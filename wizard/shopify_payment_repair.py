"""Read-only preview followed by an explicitly requested, atomic repair."""
import json
from markupsafe import Markup, escape
from odoo import fields, models, _
from odoo.exceptions import UserError
from ..models.shopify_payment_plan import fingerprint


class ShopifyPaymentRepair(models.TransientModel):
    _name = 'shopify.payment.repair.ept'
    _description = 'Preview / Repair Shopify Payments'

    order_ids = fields.Many2many('sale.order', required=True, readonly=True)
    plan_json = fields.Json(readonly=True)
    preview_html = fields.Html(readonly=True)
    state = fields.Selection([('preview', 'Preview'), ('blocked', 'Needs Review'), ('done', 'Applied')],
                             default='preview', readonly=True)
    repair_case_id = fields.Many2one('shopify.payout.repair.line.ept', readonly=True, ondelete='set null')
    invoice_receipt_available = fields.Boolean(readonly=True)
    allow_invoice_receipt = fields.Boolean(readonly=True)

    def _check_manager(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_('Only accounting managers can repair Shopify payments.'))
        self.order_ids.check_access('write')
        if not self.order_ids:
            raise UserError(_('Select at least one Shopify order.'))
        if self.repair_case_id:
            self.repair_case_id.check_access('write')
            self.repair_case_id.wizard_id._check_manager()
            if (self.order_ids != self.repair_case_id.current_order_id
                    or self.repair_case_id.transaction_ids.payout_id - self.repair_case_id.wizard_id.payout_ids):
                raise UserError(_('The payment preview must still belong to its saved payout case.'))

    def action_preview(self):
        self._check_manager()
        if self.state == 'done':
            raise UserError(_('This preview has already been applied. Open a new preview to inspect current records.'))
        plans, sections = [], []
        blocked, invoice_receipt_available = False, False
        for order in self.order_ids.sorted('id'):
            sections.append(Markup('<h3>%s</h3>') % escape(order.display_name))
            try:
                plan = order._build_shopify_cash_plan(repair=True, allow_invoice_receipt=self.allow_invoice_receipt)
            except UserError as error:
                blocked = True
                invoice_receipt_available |= getattr(error, 'repair_route', '') == 'invoice_receipt'
                sections.append(Markup('<p><strong>Needs review:</strong> %s</p>') % escape(str(error)))
                continue
            plans.append(plan)
            if plan.get('replacement_basis') == 'invoice_receipt':
                sections.append(Markup('<p><strong>Invoice receipt correction:</strong> reverse the single invoice-sized receipt below '
                                       'and record the complete successful Shopify capture. Keep both invoices and their totals. '
                                       'The existing receipt is reversed before the source receipt is created.</p>'))
                for invoice in self.env['account.move'].browse(plan['invoice_ids']):
                    sections.append(Markup('<p>Keep invoice %s: %s %s; current customer balance %s.</p>') %
                                    (escape(invoice.display_name), escape(str(invoice.amount_total)),
                                     escape(invoice.currency_id.name), escape(str(invoice.amount_residual))))
            treatment = {'gross': 'Original invoice', 'net': 'Refunds already included in invoice',
                         'prepayment': 'Customer prepayment before shipment; leave customer credit open for the shipment invoice',
                         'cancelled_cash': 'Fully refunded cancellation before fulfillment; receipt and refund only',
                         'archived_refund_cash': 'Fully refunded archived order with all items removed; receipt and refund only',
                         'net_with_credits': 'Previously audited net invoice; later refunds recorded separately'}[plan['mode']]
            sections.append(Markup('<p>Invoice treatment: %s. Replace legacy payments: %s. Create credit notes: %s.</p>')
                            % (escape(treatment), escape(', '.join(map(str, plan['replace_ids'])) or 'None'),
                               len(plan['credits'])))
            if plan['mode'] == 'prepayment':
                sections.append(Markup('<p>Record the captured payment now for payout reconciliation. '
                                       'No invoice is created. The same payment will be applied when the shipment invoice posts; '
                                       'its original date and payout match are retained.</p>'))
            for credit in self.env['account.move'].browse(plan['reused_credit_ids']):
                sections.append(Markup('<p>Reuse credit note %s for Shopify refund %s: %s %s.</p>')
                                % (escape(credit.display_name), escape(credit.shopify_refund_id),
                                   escape(str(credit.amount_total)), escape(credit.currency_id.name)))
            for payment in self.env['account.payment'].browse(plan['replace_ids']):
                sections.append(Markup('<p>Reverse %s (%s %s), entry %s, dated %s. The original entry and reversal remain in the audit trail.</p>')
                                % (escape(payment.display_name), escape(str(payment.amount)), escape(payment.currency_id.name),
                                   escape(payment.move_id.name), escape(str(payment.move_id.date))))
            for credit in plan['credits']:
                sections.append(Markup('<p>Create credit note for refund %s: %s %s on %s.</p>')
                                % (escape(credit['values']['shopify_refund_id']), escape(str(credit['amount'])),
                                   escape(order.currency_id.name), escape(credit['values']['date'])))
            sections.append(Markup('<table class="table"><thead><tr><th>Action</th><th>Transaction</th><th>Type</th>'
                                   '<th>Shopify date</th><th>Payment date</th><th>Amount</th><th>Journal</th></tr></thead><tbody>'))
            for event in plan['events']:
                journal = self.env['account.journal'].browse(event['journal_id'])
                sections.append(Markup('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s %s</td><td>%s</td></tr>')
                                % (escape('Reuse payment %s' % event['payment_id'] if event['payment_id'] else 'Create payment'),
                                   escape(event['id']), escape(event['kind']), escape(event['date']),
                                   escape(event['payment_date']),
                                   escape(event['amount']), escape(event['currency']), escape(journal.display_name)))
            sections.append(Markup('</tbody></table>'))
        self.write({'plan_json': json.loads(json.dumps(plans, default=str)),
                    'preview_html': Markup('').join(sections), 'state': 'blocked' if blocked else 'preview',
                    'invoice_receipt_available': invoice_receipt_available and len(self.order_ids) == 1})
        return self._reopen()

    def action_preview_invoice_receipt(self):
        self._check_manager()
        if self.state != 'blocked' or not self.invoice_receipt_available or len(self.order_ids) != 1:
            raise UserError(_('First preview one order with an eligible invoice-sized receipt.'))
        self.allow_invoice_receipt = True
        return self.action_preview()

    def action_return_to_payout_case(self):
        self._check_manager()
        if not self.repair_case_id:
            raise UserError(_('This preview was not opened from a saved payout case.'))
        return self.repair_case_id.action_refresh_case()

    def action_apply(self):
        self._check_manager()
        # A duplicate click cannot reuse the same approval or an old transaction snapshot.
        self.env.cr.execute('SELECT id FROM shopify_payment_repair_ept WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if self.state != 'preview' or not self.plan_json:
            raise UserError(_('Generate an unblocked preview before applying the repair.'))
        payouts = self.env['shopify.payout.report.ept']
        if self.repair_case_id:
            workspace = self.repair_case_id.wizard_id
            self.env.cr.execute('SELECT id FROM shopify_payout_repair_ept WHERE id = %s FOR UPDATE', [workspace.id])
            workspace.invalidate_recordset()
            self.repair_case_id.invalidate_recordset()
            self._check_manager()
            payouts |= workspace.payout_ids
        if self.repair_case_id or self.allow_invoice_receipt:
            for order in self.order_ids:
                payouts |= order._shopify_cash_payouts()
            payouts._lock_settlement_payouts()
        for order in self.order_ids.sorted('id'):
            order._lock_shopify_cash()
        fresh = [order._build_shopify_cash_plan(repair=True, allow_invoice_receipt=self.allow_invoice_receipt)
                 for order in self.order_ids.sorted('id')]
        if fingerprint(fresh) != fingerprint(self.plan_json):
            raise UserError(_('Shopify transactions, configuration or accounting changed after preview. Refresh the preview before applying.'))
        with self.env.cr.savepoint():
            for order, plan in zip(self.order_ids.sorted('id'), fresh):
                if self.repair_case_id:
                    order = order.with_context(tracking_disable=True, mail_create_nosubscribe=True,
                                               shopify_silent_cancelled_import=True)
                payments = order._apply_shopify_cash_plan(plan)
                if self.repair_case_id:
                    audit = order.shopify_payment_audit_ids.sorted('id', reverse=True)[:1]
                    self.repair_case_id.write({'payment_ids': [(6, 0, payments.ids)],
                                              'credit_ids': [(6, 0, audit.credit_note_ids.ids)]})
                    self.repair_case_id._record_history('payment', detail=_('Reviewed payment repair applied; order audit %s. Refresh the payout case to review and finish matching.', audit.id))
            self.write({'state': 'done'})
        return self._reopen()

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Preview / Repair Shopify Payments')}
