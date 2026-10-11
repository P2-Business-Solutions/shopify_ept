"""Review and apply independent payout exceptions without blocking the batch."""
import json
import pytz
from markupsafe import Markup, escape

from odoo import api, Command, fields, models, _
from odoo.exceptions import UserError
from psycopg2 import OperationalError
from ..models.shopify_payment_plan import fingerprint


class ShopifyPayoutRepair(models.Model):
    _name = 'shopify.payout.repair.ept'
    _description = 'Bulk Shopify Payout Repairs'

    payout_ids = fields.Many2many('shopify.payout.report.ept', required=True, readonly=True)
    company_id = fields.Many2one('res.company', compute='_compute_company_id', store=True, precompute=True,
                                required=True, readonly=True, index=True)
    cutover_date = fields.Date(string='Fulfillment Cutover', default='2026-08-01', required=True)
    timezone = fields.Selection(selection=lambda self: [(tz, tz) for tz in pytz.all_timezones],
                                default=lambda self: self.env.user.tz or 'America/New_York', required=True)
    sales_journal_id = fields.Many2one('account.journal', string='Historical Refund Credit Journal',
                                      domain="[('type', '=', 'sale')]",
                                      default=lambda self: self.env['account.journal'].search([
                                          ('company_id', '=', self.env.company.id), ('type', '=', 'sale')], limit=1))
    post_historical = fields.Boolean(string='Post Proven Historical Credits and Refund Payments', default=True)
    clear_historical_collections = fields.Boolean(
        string='Clear Cash Included in Opening Balances',
        help='Confirm that the selected pre-cutover collections and refunds, including their original sales/returns '
             'and taxes, are already included in migration balances. Their unsettled net cash is held in the account '
             'below. Only missing balance-sheet clearing entries are created; no sales or returns are repeated.')
    historical_collection_account_id = fields.Many2one(
        'account.account', string='Opening-Balance Cash Account', check_company=True,
        domain="[('account_type', 'in', ('asset_current', 'asset_cash')), ('deprecated', '=', False)]",
        default=lambda self: self.env['account.account'].search([
            ('company_ids', 'in', self.env.company.ids), ('code', '=', '110010'), ('deprecated', '=', False)], limit=1))
    open_only = fields.Boolean(string='Unmatched Transactions Only', default=True)
    rematch_payouts = fields.Boolean(string='Reconcile Payout Statements in This Run', default=True,
        help='Create/reuse reviewed accounting and match it to the payout in the same run. '
             'Disable only to post accounting without matching; those cases remain awaiting reconciliation.')
    line_ids = fields.One2many('shopify.payout.repair.line.ept', 'wizard_id', readonly=True)
    preview_settings = fields.Char(readonly=True)
    result = fields.Text(string='Last Run Result', readonly=True)
    progress_summary = fields.Char(compute='_compute_progress_summary', string='Current Progress')
    history_ids = fields.One2many('shopify.payout.repair.history.ept', 'workspace_id', readonly=True)

    @api.depends('payout_ids.instance_id.shopify_company_id')
    def _compute_company_id(self):
        for workspace in self:
            workspace.company_id = workspace.payout_ids[:1].instance_id.shopify_company_id

    @api.constrains('payout_ids', 'company_id')
    def _check_payout_company(self):
        for workspace in self:
            if workspace.payout_ids.instance_id.shopify_company_id != workspace.company_id:
                raise UserError(_('A saved repair workspace must contain payouts from one company.'))

    @api.depends('line_ids.status', 'line_ids.reconciliation_status', 'line_ids.active')
    def _compute_progress_summary(self):
        for workspace in self:
            matched = workspace.line_ids.filtered(lambda line: line.reconciliation_status == 'matched')
            pending = workspace.line_ids - matched
            workspace.progress_summary = _('%s matched; %s ready; %s need review; %s applied and awaiting match.',
                len(matched), len(pending.filtered(lambda line: line.status == 'ready')),
                len(pending.filtered(lambda line: line.status == 'review')),
                len(pending.filtered(lambda line: line.status == 'done')))

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
                                collections=self.clear_historical_collections,
                                collection_account=self.historical_collection_account_id.id,
                                open_only=self.open_only, rematch=self.rematch_payouts))

    def _plan(self, transactions, order, historical=False, collection=False):
        if transactions.payout_id - self.payout_ids:
            raise UserError(_('The repair contains transactions outside the selected payouts.'))
        for payout in transactions.payout_id:
            payout._check_payout_reimport_period()
        if historical:
            return transactions.sorted('id')[:1]._historical_refund_plan(
                self.cutover_date, self.timezone, self.sales_journal_id, self.post_historical,
                opening_account=self.historical_collection_account_id,
                opening_confirmed=self.clear_historical_collections)
        if collection:
            if len(transactions) != 1:
                raise UserError(_('Review each historical collection against its exact payout transaction.'))
            return transactions._historical_collection_plan(
                self.cutover_date, self.timezone, self.historical_collection_account_id,
                self.clear_historical_collections)
        if len(order) != 1:
            raise UserError(_('Import or correct the order before repairing its current-period accounting.'))
        order.check_access('write')
        plan = order._build_shopify_cash_plan(repair=True)
        return dict(kind='order', order_id=order.id, cash=plan)

    def action_preview(self):
        return self._refresh_preview()

    def _refresh_preview(self, case=None):
        self._check_manager()
        self.env.cr.execute('SELECT id FROM shopify_payout_repair_ept WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        groups, exceptions = {}, []
        transactions = (case.transaction_ids if case else self.payout_ids.payout_transaction_ids).filtered(
            lambda row: row.transaction_type in ('charge', 'refund', 'payment_refund') or row.shop_cash_kind)
        refreshed = self.env['shopify.payout.report.line.ept']
        for transaction in transactions.sorted('id'):
            statements = transaction.payout_id.payout_statement_line_ids.filtered(
                lambda row: row.payout_line_id == transaction)
            if self.open_only and statements and all(statements.mapped('is_reconciled')):
                continue
            refreshed |= transaction
            orders = self.env['sale.order']
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
                    if plan['kind'] == 'opening_refund':
                        groups[('opening_refund', transaction.id)] = [transaction, orders, plan]
                        continue
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
                    plan = self._plan(transaction, orders, collection=True)
                    groups[('collection', transaction.id)] = [transaction, orders, plan]
            except UserError as error:
                exceptions.append((transaction, orders if len(orders) == 1 else self.env['sale.order'], str(error)))
        values = []
        for transactions, order, historic_plan in groups.values():
            plan, error = None, ''
            try:
                plan = historic_plan or self._plan(transactions, order)
            except UserError as problem:
                error = str(problem)
            summary = error or self._summary(plan)
            values.append(dict(wizard_id=self.id, transaction_ids=[Command.set(transactions.ids)],
                               order_id=order.id, kind=historic_plan['kind'] if historic_plan else 'order',
                               source_reference=order.display_name if order else (historic_plan or {}).get(
                                   'source_name', transactions[:1].source_order_id),
                               status='review' if error else 'ready', selected=not error,
                               plan=json.loads(json.dumps(plan, default=str)) if plan else False,
                               detail=summary))
        values.extend(dict(wizard_id=self.id, transaction_ids=[Command.set(transaction.ids)],
                           order_id=order.id,
                           source_reference=transaction.source_order_id or transaction.transaction_id,
                           kind='review', status='review', selected=False, detail=error)
                      for transaction, order, error in exceptions)
        retained = self.env['shopify.payout.repair.line.ept']
        previous = self.line_ids.filtered(lambda line: bool(line.transaction_ids & refreshed))
        for value in values:
            ids = set(value['transaction_ids'][0][2])
            existing = previous.filtered(lambda line: set(line.transaction_ids.ids) == ids)[:1]
            value.update(settings_fingerprint=self._settings(), last_preview_at=fields.Datetime.now(),
                         last_error=False, plan=value.get('plan') or False)
            if existing:
                existing.write(value)
                line = existing
            else:
                line = self.env['shopify.payout.repair.line.ept'].create(value)
            retained |= line
            line._record_history('preview')
        # Keep completed results and archive superseded groupings with their history.
        (previous - retained).write({'active': False, 'selected': False})
        self.invalidate_recordset(['line_ids'])
        self.payout_ids.invalidate_recordset(['repair_case_ids'])
        self.write({'preview_settings': self._settings(),
                    'result': _('%s ready; %s need review. Select the ready cases to apply.',
                                len(self.line_ids.filtered(lambda row: row.status == 'ready')),
                                len(self.line_ids.filtered(lambda row: row.status == 'review')))})
        return self._reopen()

    def _summary(self, plan):
        if plan['kind'] in ('collection', 'opening_refund'):
            account = self.env['account.account'].browse(plan['account_id'])
            if plan['kind'] == 'opening_refund':
                return _('%(order)s: clear the pre-cutover refund %(amount)s through %(account)s on %(date)s and match its payout deduction. '
                         'The original return and tax remain in migration balances; no new credit note or customer refund payment.',
                         order=plan['source_name'], amount=abs(plan['amount']), account=account.display_name,
                         date=plan['values']['date'])
            return _('%(order)s: clear %(amount)s from %(account)s on %(date)s and match the payout receipt. '
                     'Original sales and fulfillment remain in the old system.',
                     order=plan['source_name'], amount=plan['amount'], account=account.display_name,
                     date=plan['values']['date'])
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
                    fresh = self._plan(line.transaction_ids, line.order_id, line.kind == 'historical',
                                       line.kind in ('collection', 'opening_refund'))
                    if line.settings_fingerprint != self._settings():
                        raise UserError(_('Settings changed for this case. Refresh it before applying.'))
                    if fingerprint(fresh) != fingerprint(line.plan):
                        raise UserError(_('Source data or accounting changed after preview. Refresh this case before applying.'))
                    if fresh['kind'] == 'historical':
                        credit, payments = line.transaction_ids.sorted('id')[:1]._apply_historical_refund_plan(
                            fresh, payouts=line.transaction_ids.payout_id)
                        line.write({'credit_ids': [Command.set(credit.ids)], 'payment_ids': [Command.set(payments.ids)]})
                    elif fresh['kind'] in ('collection', 'opening_refund'):
                        move = line.transaction_ids._apply_historical_collection_plan(fresh)
                        line.write({'collection_move_ids': [Command.set(move.ids)]})
                    else:
                        payments = line.order_id.with_context(tracking_disable=True, mail_create_nosubscribe=True,
                            shopify_silent_cancelled_import=True)._apply_shopify_cash_plan(fresh['cash'])
                        payment_audit = line.order_id.shopify_payment_audit_ids.sorted('id', reverse=True)[:1]
                        line.write({'payment_ids': [Command.set(payments.ids)],
                                    'credit_ids': [Command.set(payment_audit.credit_note_ids.ids)]})
                        self.env['shopify.payout.repair.audit.ept']._record_repair({
                            'payout_ids': [Command.set(line.transaction_ids.payout_id.ids)],
                            'company_id': line.order_id.company_id.id, 'order_id': line.order_id.id,
                            'source_order_id': line.order_id.shopify_order_id, 'plan': fresh,
                            'transaction_ids': [Command.set(line.transaction_ids.ids)],
                            'payment_audit_id': payment_audit.id,
                            'credit_ids': [Command.set(line.credit_ids.ids)],
                            'payment_ids': [Command.set(payments.ids)],
                        })
                    for payout in line.transaction_ids.payout_id:
                        payout._message_log(body=_('Reviewed bulk repair: %s. Source/ledger fingerprint: %s.',
                                                  line.detail, fingerprint(fresh)))
                    line.write({'status': 'done', 'selected': False, 'last_error': False,
                                'last_apply_at': fields.Datetime.now()})
                    line._record_history('apply')
                applied += 1
            except OperationalError:
                raise
            except UserError as error:
                line.write({'status': 'review', 'selected': False, 'detail': str(error), 'last_error': str(error)})
                line._record_history('failure')
                failed += 1
        matching_issues = []
        if self.rematch_payouts:
            for payout in selected.filtered(lambda row: row.status == 'done').transaction_ids.payout_id:
                try:
                    with self.env.cr.savepoint():
                        payout._check_payout_reimport_period()
                        reviewed = selected.filtered(lambda row: row.status == 'done' and (
                            row.payment_ids or row.collection_move_ids)).transaction_ids.filtered(
                            lambda transaction: transaction.payout_id == payout)
                        scoped_payout = payout.with_context(shopify_bulk_repaired_line_ids=reviewed.ids, cron_process=True)
                        if payout.state in ('draft', 'partially_generated'):
                            scoped_payout.generate_bank_statement()
                        scoped_payout.process_bank_statement()
                        if payout.reimport_reconciliation_issue:
                            matching_issues.append('%s: %s' % (payout.display_name, payout.reimport_reconciliation_issue))
                except OperationalError:
                    raise
                except UserError as error:
                    payout.reimport_reconciliation_issue = str(error)
                    matching_issues.append('%s: %s' % (payout.display_name, error))
        done = selected.filtered(lambda row: row.status == 'done')
        matched = done.filtered(lambda row: row.reconciliation_status == 'matched')
        pending = done - matched
        for line in done:
            issue = '\n'.join(filter(None, line.transaction_ids.payout_id.mapped('reimport_reconciliation_issue')))
            line.last_error = issue if self.rematch_payouts and line.reconciliation_status == 'pending' else False
            line._record_history('match' if self.rematch_payouts else 'post_only')
        result = [_('%s case(s) applied; %s case(s) changed or failed and remain for review.', applied, failed),
                  _('%s applied case(s) fully matched to their payout; %s still awaiting reconciliation.', len(matched), len(pending))]
        if pending and not self.rematch_payouts:
            result.append(_('Payout matching was disabled for this run. Enable Reconcile Payout Statements in This Run and apply a refreshed preview to reuse the existing accounting and finish matching.'))
        if matching_issues:
            result.append(_('Accounting was retained. Payout reconciliation/validation needs review: %s', '\n'.join(matching_issues)))
        self.result = '\n'.join(result)
        return self._reopen()

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': _('Bulk Repair Payout Transactions')}


class ShopifyPayoutRepairLine(models.Model):
    _name = 'shopify.payout.repair.line.ept'
    _description = 'Reviewed Shopify Payout Repair Case'

    wizard_id = fields.Many2one('shopify.payout.repair.ept', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='wizard_id.company_id', store=True, index=True)
    active = fields.Boolean(default=True, readonly=True)
    payout_ids = fields.Many2many('shopify.payout.report.ept', 'shopify_payout_repair_case_rel',
                                 'case_id', 'payout_id', compute='_compute_payout_ids', store=True)
    transaction_ids = fields.Many2many('shopify.payout.report.line.ept', 'shopify_bulk_repair_transaction_rel',
                                      'case_id', 'transaction_id', readonly=True)
    order_id = fields.Many2one('sale.order', readonly=True)
    current_order_id = fields.Many2one('sale.order', compute='_compute_current_order_id', string='Order')
    source_reference = fields.Char(string='Source Order', readonly=True)
    selected = fields.Boolean()
    kind = fields.Selection([('order', 'Order Cash Repair'), ('historical', 'Historical Refund'),
                             ('collection', 'Historical Collection'), ('opening_refund', 'Opening-Balance Refund'),
                             ('review', 'Needs Review')], readonly=True)
    status = fields.Selection([('ready', 'Ready'), ('review', 'Needs Review'), ('done', 'Applied')], readonly=True)
    detail = fields.Text(readonly=True)
    plan = fields.Json(readonly=True)
    settings_fingerprint = fields.Char(readonly=True)
    last_preview_at = fields.Datetime(string='Last Preview', readonly=True)
    last_apply_at = fields.Datetime(string='Last Applied', readonly=True)
    last_error = fields.Text(string='Latest Processing Issue', readonly=True)
    history_ids = fields.One2many('shopify.payout.repair.history.ept', 'case_id', readonly=True)
    review_html = fields.Html(compute='_compute_review_html', string='Accounting Preview')
    credit_ids = fields.Many2many('account.move', readonly=True)
    payment_ids = fields.Many2many('account.payment', readonly=True)
    collection_move_ids = fields.Many2many('account.move', 'shopify_collection_repair_move_rel',
                                          'case_id', 'move_id', readonly=True, string='Opening-Balance Clearing Entries')
    reconciliation_status = fields.Selection([('matched', 'Matched'), ('pending', 'Awaiting Match')],
        compute='_compute_reconciliation_status', string='Payout Match')

    @api.depends('transaction_ids.payout_id')
    def _compute_payout_ids(self):
        for line in self:
            line.payout_ids = line.transaction_ids.payout_id

    @api.depends('order_id', 'transaction_ids.source_order_id', 'transaction_ids.order_id')
    def _compute_current_order_id(self):
        for line in self:
            order = line.order_id.exists()
            if not order:
                try:
                    orders = line.transaction_ids._repair_orders_for_progress()
                    order = orders if len(orders) == 1 else self.env['sale.order']
                except UserError:
                    order = self.env['sale.order']
            line.current_order_id = order

    def _record_history(self, operation):
        self.ensure_one()
        return self.env['shopify.payout.repair.history.ept']._record_event({
            'workspace_id': self.wizard_id.id, 'case_id': self.id, 'company_id': self.company_id.id,
            'payout_ids': [Command.set(self.payout_ids.ids)], 'operation': operation,
            'status': self.status, 'reconciliation_status': self.reconciliation_status,
            'source_reference': self.source_reference, 'source_order_id': self.transaction_ids[:1].source_order_id,
            'order_id': self.current_order_id.id, 'detail': self.last_error or self.detail,
            'plan': self.plan, 'payment_ids': [Command.set(self.payment_ids.ids)],
            'credit_ids': [Command.set(self.credit_ids.ids)],
            'collection_move_ids': [Command.set(self.collection_move_ids.ids)],
        })

    def action_refresh_case(self):
        self.ensure_one()
        return self.wizard_id._refresh_preview(case=self)

    @api.depends('transaction_ids', 'transaction_ids.payout_id.payout_statement_line_ids.is_reconciled',
                 'transaction_ids.payout_id.payout_statement_line_ids.move_id.checked',
                 'transaction_ids.payout_id.payout_statement_line_ids.move_id.state')
    def _compute_reconciliation_status(self):
        for line in self:
            transactions = line.transaction_ids.filtered(lambda transaction:
                not transaction.payout_id.currency_id.is_zero(transaction.amount))
            matched = bool(transactions)
            for transaction in transactions:
                statements = transaction.payout_id.payout_statement_line_ids.filtered(
                    lambda statement: statement.payout_line_id == transaction)
                if (len(statements) != 1 or not statements.is_reconciled
                        or statements.move_id.state != 'posted' or not statements.move_id.checked):
                    matched = False
                    break
            line.reconciliation_status = 'matched' if matched else 'pending'

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
            elif plan.get('kind') in ('collection', 'opening_refund'):
                currency = self.env['res.currency'].browse(plan['values']['currency_id'])
                journal = self.env['account.journal'].browse(plan['values']['journal_id'])
                sections.append(Markup('<p>Original Shopify %s: %s on %s. Clearing entry: %s in %s.</p>') %
                                (escape('refund' if plan['kind'] == 'opening_refund' else 'collection'),
                                 escape(plan['event']['id']), escape(plan['event']['timestamp']),
                                 escape(plan['values']['date']), escape(journal.display_name)))
                sections.append(Markup('<table class="table"><thead><tr><th>Account</th><th>Debit</th><th>Credit</th></tr></thead><tbody>'))
                for command in plan['values']['line_ids']:
                    item = command[2]
                    account = self.env['account.account'].browse(item['account_id'])
                    sections.append(Markup('<tr><td>%s</td><td>%s %s</td><td>%s %s</td></tr>') %
                                    (escape(account.display_name), escape(str(item['debit'])), escape(currency.name),
                                     escape(str(item['credit'])), escape(currency.name)))
                sections.append(Markup('</tbody></table><p>The outstanding entry matches this exact payout receipt/refund. '
                                       'Sales, returns and taxes already included in migration balances are retained. '
                                       'No invoice, credit note, customer payment, delivery or manufacturing order is created.</p>'))
                events = []
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
        self.invalidate_recordset(['current_order_id'])
        if self.current_order_id:
            return {'type': 'ir.actions.act_window', 'res_model': 'sale.order', 'res_id': self.current_order_id.id,
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
