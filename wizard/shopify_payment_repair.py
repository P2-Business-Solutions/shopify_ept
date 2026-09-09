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

    def _check_manager(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_('Only accounting managers can repair Shopify payments.'))
        self.order_ids.check_access('write')
        if not self.order_ids:
            raise UserError(_('Select at least one Shopify order.'))

    def action_preview(self):
        self._check_manager()
        if self.state == 'done':
            raise UserError(_('This preview has already been applied. Open a new preview to inspect current records.'))
        plans, sections = [], []
        blocked = False
        for order in self.order_ids.sorted('id'):
            sections.append(Markup('<h3>%s</h3>') % escape(order.display_name))
            try:
                plan = order._build_shopify_cash_plan(repair=True)
            except UserError as error:
                blocked = True
                sections.append(Markup('<p><strong>Needs review:</strong> %s</p>') % escape(str(error)))
                continue
            plans.append(plan)
            treatment = {'gross': 'Original invoice', 'net': 'Refunds already included in invoice',
                         'net_with_credits': 'Previously audited net invoice; later refunds recorded separately'}[plan['mode']]
            sections.append(Markup('<p>Invoice treatment: %s. Replace legacy payments: %s. Create credit notes: %s.</p>')
                            % (escape(treatment), escape(', '.join(map(str, plan['replace_ids'])) or 'None'),
                               len(plan['credits'])))
            for payment in self.env['account.payment'].browse(plan['replace_ids']):
                sections.append(Markup('<p>Reverse %s (%s %s), entry %s, dated %s. The original entry and reversal remain in the audit trail.</p>')
                                % (escape(payment.display_name), escape(str(payment.amount)), escape(payment.currency_id.name),
                                   escape(payment.move_id.name), escape(str(payment.move_id.date))))
            for credit in plan['credits']:
                sections.append(Markup('<p>Create credit note for refund %s: %s %s on %s.</p>')
                                % (escape(credit['values']['shopify_refund_id']), escape(str(credit['amount'])),
                                   escape(order.currency_id.name), escape(credit['values']['date'])))
            sections.append(Markup('<table class="table"><thead><tr><th>Action</th><th>Transaction</th><th>Type</th>'
                                   '<th>Date</th><th>Amount</th><th>Journal</th></tr></thead><tbody>'))
            for event in plan['events']:
                journal = self.env['account.journal'].browse(event['journal_id'])
                sections.append(Markup('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s %s</td><td>%s</td></tr>')
                                % (escape('Reuse payment %s' % event['payment_id'] if event['payment_id'] else 'Create payment'),
                                   escape(event['id']), escape(event['kind']), escape(event['date']),
                                   escape(event['amount']), escape(event['currency']), escape(journal.display_name)))
            sections.append(Markup('</tbody></table>'))
        self.write({'plan_json': json.loads(json.dumps(plans, default=str)),
                    'preview_html': Markup('').join(sections), 'state': 'blocked' if blocked else 'preview'})
        return self._reopen()

    def action_apply(self):
        self._check_manager()
        # A duplicate click cannot reuse the same approval or an old transaction snapshot.
        self.env.cr.execute('SELECT id FROM shopify_payment_repair_ept WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if self.state != 'preview' or not self.plan_json:
            raise UserError(_('Generate an unblocked preview before applying the repair.'))
        for order in self.order_ids.sorted('id'):
            order._lock_shopify_cash()
        fresh = [order._build_shopify_cash_plan(repair=True) for order in self.order_ids.sorted('id')]
        if fingerprint(fresh) != fingerprint(self.plan_json):
            raise UserError(_('Shopify transactions, configuration or accounting changed after preview. Refresh the preview before applying.'))
        with self.env.cr.savepoint():
            for order, plan in zip(self.order_ids.sorted('id'), fresh):
                order._apply_shopify_cash_plan(plan)
            self.write({'state': 'done'})
        return self._reopen()

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Preview / Repair Shopify Payments')}
