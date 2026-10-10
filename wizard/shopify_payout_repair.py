"""Review and apply independent payout exceptions without blocking the batch."""
import json
import pytz
from markupsafe import Markup, escape

from odoo import api, Command, fields, models, _
from odoo.exceptions import UserError
from psycopg2 import OperationalError
from ..models.shopify_payment_plan import fingerprint


class ShopifyPayoutRepair(models.TransientModel):
    _name = 'shopify.payout.repair.ept'
    _description = 'Bulk Shopify Payout Repairs'

    payout_ids = fields.Many2many('shopify.payout.report.ept', required=True, readonly=True)
    cutover_date = fields.Date(string='Fulfillment Cutover', default='2026-08-01', required=True)
    timezone = fields.Selection(selection=lambda self: [(tz, tz) for tz in pytz.all_timezones],
                                default=lambda self: self.env.user.tz or 'America/New_York', required=True)
    sales_journal_id = fields.Many2one('account.journal', string='Historical Refund Credit Journal',
                                      domain="[('type', '=', 'sale')]",
                                      default=lambda self: self.env['account.journal'].search([
                                          ('company_id', '=', self.env.company.id), ('type', '=', 'sale')], limit=1))
    post_historical = fields.Boolean(string='Post Proven Historical Credits and Refund Payments', default=True)
    open_only = fields.Boolean(string='Unmatched Transactions Only', default=True)
    rematch_payouts = fields.Boolean(string='Reconcile Existing Payout Statements After Repair', default=True)
    line_ids = fields.One2many('shopify.payout.repair.line.ept', 'wizard_id', readonly=True)
    preview_settings = fields.Char(readonly=True)
    result = fields.Text(readonly=True)

    def _check_manager(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_('Only accounting managers can apply payout repairs.'))
        self.payout_ids.check_access('write')
        if not self.payout_ids:
            raise UserError(_('Select at least one payout.'))

    def _settings(self):
        return fingerprint(dict(payouts=sorted(self.payout_ids.ids), cutover=str(self.cutover_date), timezone=self.timezone,
                                journal=self.sales_journal_id.id, post=self.post_historical,
                                open_only=self.open_only, rematch=self.rematch_payouts))

    def _plan(self, transactions, order, historical=False):
        if transactions.payout_id - self.payout_ids:
            raise UserError(_('The repair contains transactions outside the selected payouts.'))
        for payout in transactions.payout_id:
            payout._check_payout_reimport_period()
        if historical:
            return transactions.sorted('id')[:1]._historical_refund_plan(
                self.cutover_date, self.timezone, self.sales_journal_id, self.post_historical)
        if len(order) != 1:
            raise UserError(_('Import or correct the order before repairing its current-period accounting.'))
        order.check_access('write')
        plan = order._build_shopify_cash_plan(repair=True)
        return dict(kind='order', order_id=order.id, cash=plan)

    def action_preview(self):
        self._check_manager()
        self.line_ids.unlink()
        groups, exceptions = {}, []
        transactions = self.payout_ids.payout_transaction_ids.filtered(
            lambda row: row.transaction_type in ('charge', 'refund', 'payment_refund') or row.shop_cash_kind)
        for transaction in transactions.sorted('id'):
            statements = transaction.payout_id.payout_statement_line_ids.filtered(
                lambda row: row.payout_line_id == transaction)
            if self.open_only and statements and all(statements.mapped('is_reconciled')):
                continue
            try:
                orders = (transaction.payout_id._shop_cash_orders_for_preview(transaction)
                          if transaction.shop_cash_kind else transaction._repair_order())
                if transaction.shop_cash_kind:
                    for order in orders:
                        key = ('order', order.id)
                        if key not in groups:
                            groups[key] = [self.env['shopify.payout.report.line.ept'], order, False]
                        groups[key][0] |= transaction
                elif orders and orders.invoice_ids.filtered(lambda move: move.state != 'cancel'):
                    key = ('order', orders.id)
                    if key not in groups:
                        groups[key] = [self.env['shopify.payout.report.line.ept'], orders, False]
                    groups[key][0] |= transaction
                elif transaction.transaction_type in ('refund', 'payment_refund'):
                    # Multiple captures/refunds in one document produce one credit.
                    plan = self._plan(transaction, orders, historical=True)
                    key = ('historical', transaction.payout_id.instance_id.id, plan['values']['shopify_refund_id'])
                    if key not in groups:
                        groups[key] = [self.env['shopify.payout.report.line.ept'], orders, plan]
                    groups[key][0] |= transaction
                elif orders:
                    key = ('order', orders.id)
                    if key not in groups:
                        groups[key] = [self.env['shopify.payout.report.line.ept'], orders, False]
                    groups[key][0] |= transaction
                else:
                    raise UserError(_('No Odoo order exists for this collection. Review its opening-balance treatment or import the current-period order.'))
            except UserError as error:
                exceptions.append((transaction, str(error)))
        values = []
        for transactions, order, historic_plan in groups.values():
            plan, error = None, ''
            try:
                plan = historic_plan or self._plan(transactions, order)
            except UserError as problem:
                error = str(problem)
            summary = error or self._summary(plan)
            values.append(dict(wizard_id=self.id, transaction_ids=[Command.set(transactions.ids)],
                               order_id=order.id, kind='historical' if historic_plan else 'order',
                               source_reference=order.display_name if order else (historic_plan or {}).get(
                                   'source_name', transactions[:1].source_order_id),
                               status='review' if error else 'ready', selected=not error,
                               plan=json.loads(json.dumps(plan, default=str)) if plan else False,
                               detail=summary))
        values.extend(dict(wizard_id=self.id, transaction_ids=[Command.set(transaction.ids)],
                           source_reference=transaction.source_order_id or transaction.transaction_id,
                           kind='review', status='review', selected=False, detail=error)
                      for transaction, error in exceptions)
        if values:
            self.env['shopify.payout.repair.line.ept'].create(values)
        self.write({'preview_settings': self._settings(),
                    'result': _('%s ready; %s need review. Select the ready cases to apply.',
                                len(self.line_ids.filtered(lambda row: row.status == 'ready')),
                                len(self.line_ids.filtered(lambda row: row.status == 'review')))})
        return self._reopen()

    def _summary(self, plan):
        if plan['kind'] == 'historical':
            lines = plan['values']['invoice_line_ids']
            accounts = self.env['account.account'].browse(sorted({command[2]['account_id'] for command in lines}))
            return _('%(order)s: %(action)s refund credit %(amount)s; %(count)s exact cash refund(s). Accounts: %(accounts)s. Original receipt remains in opening balances.',
                     order=plan['source_name'], action='Post/reuse' if plan['post'] else 'Prepare/reuse',
                     amount=plan['amount'], count=len(plan['events']), accounts=', '.join(accounts.mapped('display_name')))
        cash = plan['cash']
        return _('%(mode)s: %(count)s cash transactions; %(replace)s net payment(s) to reverse; %(credits)s credit(s) to create. The invoice total is retained.',
                 mode=cash['mode'], count=len(cash['events']), replace=len(cash['replace_ids']), credits=len(cash['credits']))

    def action_apply(self):
        self._check_manager()
        self.env.cr.execute('SELECT id FROM shopify_payout_repair_ept WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if self.preview_settings != self._settings():
            raise UserError(_('Settings changed. Refresh the preview before applying.'))
        selected = self.line_ids.filtered(lambda row: row.selected and row.status == 'ready')
        if not selected:
            raise UserError(_('Select at least one ready repair.'))
        self.payout_ids._lock_settlement_payouts()
        keys = sorted({'shopify-cash:%s:%s' % (transaction.payout_id.instance_id.id, transaction.source_order_id)
                       for transaction in selected.transaction_ids})
        for key in keys:
            self.env.cr.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [key])
        for order in selected.order_id.sorted('id'):
            order._lock_shopify_cash()
        applied, failed = 0, 0
        for line in selected.sorted('id'):
            try:
                with self.env.cr.savepoint():
                    fresh = self._plan(line.transaction_ids, line.order_id, line.kind == 'historical')
                    if fingerprint(fresh) != fingerprint(line.plan):
                        raise UserError(_('Source data or accounting changed after preview. Refresh this case before applying.'))
                    if fresh['kind'] == 'historical':
                        credit, payments = line.transaction_ids.sorted('id')[:1]._apply_historical_refund_plan(
                            fresh, payouts=line.transaction_ids.payout_id)
                        line.write({'credit_ids': [Command.set(credit.ids)], 'payment_ids': [Command.set(payments.ids)]})
                    else:
                        payments = line.order_id.with_context(tracking_disable=True, mail_create_nosubscribe=True,
                            shopify_silent_cancelled_import=True)._apply_shopify_cash_plan(fresh['cash'])
                        line.write({'payment_ids': [Command.set(payments.ids)]})
                    for payout in line.transaction_ids.payout_id:
                        payout._message_log(body=_('Reviewed bulk repair: %s. Source/ledger fingerprint: %s.',
                                                  line.detail, fingerprint(fresh)))
                    line.status = 'done'
                applied += 1
            except OperationalError:
                raise
            except UserError as error:
                line.write({'status': 'review', 'selected': False, 'detail': str(error)})
                failed += 1
        if self.rematch_payouts:
            for payout in selected.filtered(lambda row: row.status == 'done').transaction_ids.payout_id:
                try:
                    with self.env.cr.savepoint():
                        payout._check_payout_reimport_period()
                        reviewed = selected.filtered(lambda row: row.status == 'done' and row.payment_ids).transaction_ids.filtered(
                            lambda transaction: transaction.payout_id == payout)
                        scoped_payout = payout.with_context(shopify_bulk_repaired_line_ids=reviewed.ids)
                        if payout.state in ('draft', 'partially_generated'):
                            scoped_payout.generate_bank_statement()
                        scoped_payout.process_bank_statement()
                except OperationalError:
                    raise
                except UserError as error:
                    payout.reimport_reconciliation_issue = str(error)
        self.result = _('%s case(s) applied; %s case(s) changed or failed and remain for review. Existing payout matches are preserved.', applied, failed)
        return self._reopen()

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Bulk Repair Payout Transactions')}


class ShopifyPayoutRepairLine(models.TransientModel):
    _name = 'shopify.payout.repair.line.ept'
    _description = 'Reviewed Shopify Payout Repair Case'

    wizard_id = fields.Many2one('shopify.payout.repair.ept', required=True, ondelete='cascade')
    transaction_ids = fields.Many2many('shopify.payout.report.line.ept', 'shopify_bulk_repair_transaction_rel',
                                      'case_id', 'transaction_id', readonly=True)
    order_id = fields.Many2one('sale.order', readonly=True)
    source_reference = fields.Char(string='Source Order', readonly=True)
    selected = fields.Boolean()
    kind = fields.Selection([('order', 'Order Cash Repair'), ('historical', 'Historical Refund'), ('review', 'Needs Review')], readonly=True)
    status = fields.Selection([('ready', 'Ready'), ('review', 'Needs Review'), ('done', 'Applied')], readonly=True)
    detail = fields.Text(readonly=True)
    plan = fields.Json(readonly=True)
    review_html = fields.Html(compute='_compute_review_html', string='Accounting Preview')
    credit_ids = fields.Many2many('account.move', readonly=True)
    payment_ids = fields.Many2many('account.payment', readonly=True)

    @api.depends('plan')
    def _compute_review_html(self):
        for line in self:
            plan = line.plan or {}
            sections = []
            if plan.get('kind') == 'historical':
                values = plan['values']
                journal = self.env['account.journal'].browse(values['journal_id'])
                partner = self.env['res.partner'].browse(values['partner_id'])
                currency = self.env['res.currency'].browse(values['currency_id'])
                sections.append(Markup('<p>Customer: %s. Credit journal: %s. Date: %s. Refund total: %s %s.</p>') %
                                (escape(partner.display_name), escape(journal.display_name), escape(values['date']),
                                 escape(str(plan['amount'])), escape(currency.name)))
                sections.append(Markup('<table class="table"><thead><tr><th>Credit line</th><th>Account</th>'
                                       '<th>Quantity</th><th>Unit amount</th></tr></thead><tbody>'))
                for command in values['invoice_line_ids']:
                    item = command[2]
                    account = self.env['account.account'].browse(item['account_id'])
                    sections.append(Markup('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>') %
                                    (escape(item['name']), escape(account.display_name), escape(str(item['quantity'])),
                                     escape(str(item['price_unit']))))
                sections.append(Markup('</tbody></table>'))
                events = plan['events']
            else:
                cash = plan.get('cash', {})
                events = cash.get('events', [])
                documents = self.env['account.move'].browse(cash.get('document_ids', []))
                for document in documents:
                    sections.append(Markup('<p>Keep %s: %s %s.</p>') %
                                    (escape(document.display_name), escape(str(document.amount_total)), escape(document.currency_id.name)))
                for payment in self.env['account.payment'].browse(cash.get('replace_ids', [])):
                    sections.append(Markup('<p>Reverse legacy payment %s: %s %s, dated %s. Retain the original entry and reversal.</p>') %
                                    (escape(payment.display_name), escape(str(payment.amount)), escape(payment.currency_id.name),
                                     escape(str(payment.date))))
                for credit in cash.get('credits', []):
                    sections.append(Markup('<p>Create refund credit: %s on %s.</p>') %
                                    (escape(str(credit['amount'])), escape(credit['values']['date'])))
            if events:
                sections.append(Markup('<table class="table"><thead><tr><th>Cash transaction</th><th>Action</th><th>Type</th>'
                                       '<th>Source date</th><th>Payment date</th><th>Amount</th><th>Journal</th></tr></thead><tbody>'))
                for event in events:
                    payment = self.env['account.payment'].browse(event.get('payment_id'))
                    journal = self.env['account.journal'].browse(event['journal_id'])
                    sections.append(Markup('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s %s</td><td>%s</td></tr>') %
                                    (escape(event['id']), escape('Reuse ' + payment.display_name if payment else 'Create payment'),
                                     escape(event['kind']), escape(event['date']), escape(event.get('payment_date') or event['date']),
                                     escape(event['amount']), escape(event['currency']), escape(journal.display_name)))
                sections.append(Markup('</tbody></table>'))
            line.review_html = Markup('').join(sections)

    def action_open_order(self):
        self.ensure_one()
        if self.order_id:
            return {'type': 'ir.actions.act_window', 'res_model': 'sale.order', 'res_id': self.order_id.id,
                    'view_mode': 'form', 'target': 'current'}
        return self.transaction_ids[:1].action_open_order()

    def action_open_shopify_order(self):
        self.ensure_one()
        return self.transaction_ids[:1].action_open_shopify_order()

    def action_open_details(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Review Repair Details')}

    def action_back_to_bulk_repair(self):
        self.ensure_one()
        return self.wizard_id._reopen()
