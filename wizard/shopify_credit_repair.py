"""Prepare an exact source-based credit draft, reusing an unsettled credit."""
import base64
import json

from markupsafe import Markup
from odoo import api, Command, fields, models, _
from odoo.exceptions import UserError

from ..models.shopify_payment_plan import cash_events, fingerprint


class ShopifyCreditRepairLine(models.Model):
    _inherit = 'shopify.payout.report.line.ept'

    def action_prepare_credit_repair(self):
        self.ensure_one()
        notes = self.env['account.move']._shopify_refund_credit_candidates(self)
        wizard = self.env['shopify.credit.repair.ept'].create({
            'transaction_id': self.id, 'credit_note_id': notes.id if len(notes) == 1 else False})
        return wizard._reopen()


class ShopifyCreditRepair(models.TransientModel):
    _name = 'shopify.credit.repair.ept'
    _description = 'Prepare Shopify Refund Credit Repair'

    transaction_id = fields.Many2one('shopify.payout.report.line.ept', required=True)
    credit_note_id = fields.Many2one('account.move', string='Existing Credit Note')
    preview_html = fields.Html(readonly=True)
    plan_json = fields.Json(readonly=True)
    state = fields.Selection([('draft', 'Draft'), ('preview', 'Preview'), ('done', 'Prepared')], default='draft', readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        if any(set(v) - {'transaction_id', 'credit_note_id'} for v in values_list):
            raise UserError(_('Use Preview to prepare a credit repair.'))
        return super().create(values_list)

    def write(self, values):
        if self.env.context.get('_shopify_credit_repair_internal') is not CREDIT_INTERNAL:
            if set(values) - {'transaction_id', 'credit_note_id'} or self.state == 'done':
                raise UserError(_('Use Preview and Prepare Draft to update this repair.'))
            values = dict(values, plan_json=False, state='draft')
        return super().write(values)

    def _plan(self):
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_('An accounting manager must review a credit repair.'))
        line, note = self.transaction_id, self.credit_note_id
        line.check_access('write')
        line.payout_id._check_payout_reimport_period()
        if line.transaction_type not in ('refund', 'payment_refund') or line.shop_cash_kind or line.amount >= 0:
            raise UserError(_('Select one Shopify Payments refund.'))
        if line._refund_credit_reference():
            raise UserError(_('This refund already has a reviewed credit link. Use its existing closeout.'))
        order = line._repair_order()
        if not order:
            raise UserError(_('Use the historical refund workflow when the original order is absent.'))
        order.check_access('write')
        payload, raw = line._repair_source()
        events = cash_events(raw, order.shopify_order_id, order.currency_id.name)
        event = next((e for e in events if e['id'] == line.source_order_transaction_id), None)
        if (not event or event['kind'] != 'refund' or event['gateway'] != 'shopify_payments'
                or order.currency_id.compare_amounts(float(event['amount']), -line.amount)):
            raise UserError(_('The payout must identify its exact successful Shopify refund.'))
        documents = [r for r in payload.get('refunds', []) if event['id'] in {str(t.get('id')) for t in r.get('transactions', [])}]
        if len(documents) != 1:
            raise UserError(_('The source refund document is missing or ambiguous.'))
        refund = documents[0]
        invoices = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state == 'posted')
        notes = self.env['account.move'].search([
            ('company_id', '=', order.company_id.id), ('move_type', '=', 'out_refund'),
            ('state', '!=', 'cancel'), ('shopify_instance_id', '=', order.shopify_instance_id.id),
            ('shopify_refund_id', '=', str(refund['id']))])
        if notes - note:
            raise UserError(_('Another credit identifies this refund. Select and review the existing credit.'))
        if note:
            note.check_access('write')
            if (note.move_type != 'out_refund' or note.state not in ('draft', 'posted')
                    or note.company_id != order.company_id or note.currency_id != order.currency_id
                    or note.commercial_partner_id != order.partner_id.commercial_partner_id
                    or (note.reversed_entry_id and note.reversed_entry_id not in invoices)
                    or (note.shopify_refund_order_id and note.shopify_refund_order_id != order)
                    or note.invoice_line_ids.sale_line_ids.order_id - order
                    or (note.shopify_instance_id and note.shopify_instance_id != order.shopify_instance_id)
                    or (note.shopify_refund_id and note.shopify_refund_id != str(refund['id']))
                    or note.inalterable_hash or note.reversal_move_ids.filtered(lambda m: m.state == 'posted')
                    or note.line_ids.matched_debit_ids or note.line_ids.matched_credit_ids
                    or note.shopify_refund_reference_ids
                    or ('payout_refund_reference_ids' in note._fields and note.payout_refund_reference_ids)):
                raise UserError(_('The credit is allocated, locked, reversed or has conflicting source links. Review it manually.'))
            if order.company_id._get_violated_lock_dates(note.date, True, note.journal_id):
                raise UserError(_('The existing credit accounting date is locked.'))
        quantities = {}
        for doc in payload.get('refunds', []):
            for item in doc.get('refund_line_items', []):
                key = str(item.get('line_item_id'))
                quantities[key] = quantities.get(key, 0) + float(item.get('quantity') or 0)
        for key, qty in quantities.items():
            original = invoices.invoice_line_ids.filtered(lambda item: key in item.sale_line_ids.mapped('shopify_line_id'))
            if qty > sum(original.mapped('quantity')):
                raise UserError(_('Cumulative returned quantities exceed the original invoice.'))
        ids = {str(t.get('id')) for t in refund.get('transactions', [])}
        refunds = [e for e in events if e['kind'] == 'refund' and e['id'] in ids]
        if any(e['gateway'] != 'shopify_payments' for e in refunds):
            raise UserError(_('Mixed processor refunds need allocation review.'))
        amount = sum(float(e['amount']) for e in refunds)
        values = order._shopify_credit_values(refund, amount, invoices, min(e['date'] for e in refunds),
                                              payload.get('currency', order.currency_id.name), events)
        return dict(values=values, amount=amount, note_id=note.id,
            snapshot=fingerprint(dict(payload=payload, transactions=raw, order=(order.id, str(order.write_date)),
                ledger=[(m.id, str(m.write_date), m.state, m._shopify_refund_ledger_fingerprint())
                        for m in (invoices | note).sorted('id')])))

    def action_preview(self):
        plan = self._plan()
        lines = plan['values']['invoice_line_ids']
        html = Markup('<p>%s credit draft for %s. Review and post the draft, then Link Refund Credit Note and reconcile the payout.</p>') % (
            _('Correct existing') if plan['note_id'] else _('Create'), plan['amount'])
        html += Markup('<table class="table"><tr><th>Component</th><th>Qty</th><th>Unit price</th></tr>')
        html += Markup('').join(Markup('<tr><td>%s</td><td>%s</td><td>%s</td></tr>') % (
            row[2].get('name'), row[2]['quantity'], row[2]['price_unit']) for row in lines)
        self.with_context(_shopify_credit_repair_internal=CREDIT_INTERNAL).write(
            dict(plan_json=plan, preview_html=html + Markup('</table>'), state='preview'))
        return self._reopen()

    def action_apply(self):
        if self.state != 'preview' or not self.plan_json:
            raise UserError(_('Preview the credit correction first.'))
        self.transaction_id.payout_id._lock_settlement_payouts()
        self.transaction_id._repair_order()._lock_shopify_cash()
        if self.credit_note_id:
            self.env.cr.execute('SELECT id FROM account_move WHERE id = %s FOR UPDATE', [self.credit_note_id.id])
            self.credit_note_id.invalidate_recordset()
        fresh = self._plan()
        if fingerprint(fresh) != fingerprint(self.plan_json):
            raise UserError(_('Source or accounting changed. Preview the credit again.'))
        with self.env.cr.savepoint():
            values = fresh['values']
            note = self.credit_note_id
            if note:
                before = note.read(['name', 'date', 'invoice_date', 'amount_total', 'state'])[0]
                before['lines'] = note.invoice_line_ids.read(['name', 'account_id', 'product_id', 'quantity',
                                                             'price_unit', 'discount', 'tax_ids', 'analytic_distribution'])
                attachment = self.env['ir.attachment'].create(dict(name='Credit-before-repair.json',
                    datas=base64.b64encode(json.dumps(before, default=str).encode()),
                    res_model='account.move', res_id=note.id, mimetype='application/json'))
                if note.state == 'posted':
                    note.button_draft()
                note.write(dict(values, invoice_line_ids=[Command.clear()] + values['invoice_line_ids']))
                note.message_post(body=_('Prepared source-based credit correction for review; original details retained.'),
                                  attachment_ids=attachment.ids)
            else:
                note = self.env['account.move'].with_company(self.transaction_id.payout_id.instance_id.shopify_company_id).create(values)
            if note.currency_id.compare_amounts(note.amount_total, fresh['amount']):
                raise UserError(_('The draft does not equal the exact source refund.'))
            self.with_context(_shopify_credit_repair_internal=CREDIT_INTERNAL).write({'credit_note_id': note.id, 'state': 'done'})
        return dict(type='ir.actions.act_window', res_model='account.move', res_id=note.id, view_mode='form')

    def _reopen(self):
        return dict(type='ir.actions.act_window', name=_('Prepare Credit Repair'), res_model=self._name,
                    res_id=self.id, view_mode='form', target='new')


CREDIT_INTERNAL = object()
