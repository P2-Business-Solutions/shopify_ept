"""Review and apply independent payout exceptions without blocking the batch."""
import json
import pytz
from markupsafe import Markup, escape

from odoo import api, Command, fields, models, _
from odoo.exceptions import UserError
from psycopg2 import OperationalError
from ..models.shopify_payment_plan import cash_events, fingerprint
from ..models.shopify_order_utils import is_cash_only_shopify_order
from ..models.shopify_refund_plan import source_date
from ..models.shopify_repair_evidence import source_review


REPAIR_ROUTES = [
    ('review', 'Accounting Review'), ('customer', 'Customer Mapping'),
    ('documents', 'Invoices / Credits'), ('credit', 'Refund Allocation'),
    ('source', 'Source Evidence'), ('payments', 'Payment Identity'),
    ('invoice_receipt', 'Invoice Receipt Correction'), ('shop_cash', 'Shop Cash Order')]


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
    queue_filter = fields.Selection([('pending', 'Unmatched'), ('ready', 'Ready'),
        ('review', 'Needs Review'), ('done', 'Applied / Awaiting Match'), ('matched', 'Matched'),
        ('all', 'All Cases')], string='Show Cases', default='pending', required=True)
    issue_filter = fields.Selection([('all', 'All Issues')] + REPAIR_ROUTES,
                                   string='Issue', default='all', required=True)
    visible_line_ids = fields.One2many('shopify.payout.repair.line.ept', 'wizard_id',
        compute='_compute_visible_line_ids', inverse='_inverse_visible_line_ids', readonly=False)
    selection_summary = fields.Char(compute='_compute_selection_summary', string='Selected for Apply')

    @api.depends('line_ids.status', 'line_ids.reconciliation_status', 'line_ids.resolution',
                 'line_ids.active', 'queue_filter', 'issue_filter')
    def _compute_visible_line_ids(self):
        for workspace in self:
            workspace.visible_line_ids = workspace.line_ids.filtered(workspace._case_is_visible)

    def _inverse_visible_line_ids(self):
        # This is a filtered view of the same cases. Only child selection is editable.
        for workspace in self:
            if workspace.visible_line_ids - workspace.line_ids:
                raise UserError(_('Cases cannot be moved between repair workspaces.'))

    def _case_is_visible(self, line):
        self.ensure_one()
        matched = line.reconciliation_status == 'matched'
        stage = self.queue_filter
        return ((stage == 'all' or (stage == 'matched' and matched)
                 or (stage == 'pending' and not matched)
                 or (stage in ('ready', 'review', 'done') and not matched and line.status == stage))
                and (self.issue_filter == 'all' or line.resolution == self.issue_filter))

    @api.depends('line_ids.selected', 'line_ids.status', 'line_ids.reconciliation_status')
    def _compute_selection_summary(self):
        for workspace in self:
            selected = workspace.line_ids.filtered(lambda line: line.selected and line.status == 'ready'
                                                    and line.reconciliation_status != 'matched')
            workspace.selection_summary = _('%s ready unmatched case(s) selected across all filters.', len(selected))

    def _select_ready(self, limit=None):
        self._check_manager()
        self.env.cr.execute('SELECT id FROM shopify_payout_repair_ept WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        self.line_ids.write({'selected': False})
        ready = self.line_ids.filtered(lambda line: self._case_is_visible(line)
            and line.status == 'ready' and line.reconciliation_status != 'matched').sorted('id')
        (ready[:limit] if limit else ready).write({'selected': True})
        return self._reopen()

    def action_select_ready(self):
        return self._select_ready()

    def action_select_pilot(self):
        return self._select_ready(limit=10)

    def action_clear_selection(self):
        self._check_manager()
        self.env.cr.execute('SELECT id FROM shopify_payout_repair_ept WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        self.line_ids.write({'selected': False})
        return self._reopen()

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

    def _with_opening_evidence(self, transactions, opening):
        """Validate and attach identical cash evidence during preview and apply."""
        if opening['kind'] not in ('collection', 'opening_refund'):
            return opening
        transactions.ensure_one()
        evidence_row = self.env['shopify.opening.evidence.ept'].search([
            ('instance_id', '=', transactions.payout_id.instance_id.id),
            ('transaction_id', '=', transactions.source_order_transaction_id)])
        if evidence_row:
            evidence_row._check_file_integrity()
            if (evidence_row.source_order_id != transactions.source_order_id
                    or evidence_row.currency_id != transactions.payout_id.currency_id
                    or evidence_row.currency_id.compare_amounts(evidence_row.allocated_amount, abs(transactions.amount))
                    or evidence_row.source_hash != fingerprint(dict(event=opening['event'], amount=opening['amount'],
                                                                   currency=transactions.payout_id.currency_id.id))):
                raise UserError(_('The attached A2000 allocation conflicts with this source cash event.'))
            opening['a2000_evidence'] = dict(id=evidence_row.id, document=evidence_row.document_number,
                kind=evidence_row.document_type, allocation=evidence_row.allocation_reference,
                attachment=evidence_row.attachment_id.id, original_source_hash=evidence_row.source_hash)
        return opening

    def _plan(self, transactions, order, historical=False, collection=False, evidence=None):
        if transactions.payout_id - self.payout_ids:
            raise UserError(_('The repair contains transactions outside the selected payouts.'))
        for payout in transactions.payout_id:
            payout._check_payout_reimport_period()
        return_plan = transactions.sorted('id')[:1]._refund_return_plan(self, evidence=evidence)
        if return_plan:
            return return_plan
        references = self.env['shopify.refund.reference.ept']
        for transaction in transactions:
            references |= transaction._refund_credit_reference()
        if references and len(references) == len(transactions):
            return dict(kind='credit_link', credit_ids=references.credit_note_id.ids, transaction_ids=transactions.ids)
        source = None
        if evidence is not None:
            if (historical or collection) and (not transactions[:1].source_order_id
                    or not transactions[:1].source_order_transaction_id):
                raise UserError(_('The payout cash transaction is missing its Shopify order or exact transaction ID. Reimport the payout to refresh its source links, then preview again.'))
            if order and not historical and not collection:
                order.check_access('write')
                source = order._shopify_cash_source()
            else:
                source = transactions.sorted('id')[:1]._repair_source()
            source_order_id = order.shopify_order_id if order and not historical and not collection else transactions[:1].source_order_id
            evidence.update(source_review(*source, source_order_id,
                transactions[:1].payout_id.currency_id.name, self.cutover_date, self.timezone))
            if order:
                documents = order.invoice_ids | self.env['account.move'].search([
                    ('shopify_refund_order_id', '=', order.id), ('move_type', '=', 'out_refund')])
                documents = documents.filtered(lambda move: move.state != 'cancel')
                payments = order._shopify_payment_candidates(documents, evidence.get('events', []))
                evidence['documents'] = [dict(id=move.id, name=move.display_name, kind=move.move_type,
                    state=move.state, amount=move.amount_total, residual=move.amount_residual,
                    currency=move.currency_id.name, company=move.company_id.display_name,
                    customer=move.commercial_partner_id.display_name,
                    compatible=move.company_id == order.company_id and move.currency_id == order.currency_id
                        and move.commercial_partner_id == order.partner_id.commercial_partner_id)
                    for move in documents]
                evidence['payments'] = [dict(id=pay.id, name=pay.display_name, amount=pay.amount,
                    direction=pay.payment_type, state=pay.state, date=str(pay.date),
                    currency=pay.currency_id.name, transaction=pay.shopify_order_transaction_id or '',
                    posted=pay.move_id.state == 'posted', compatible=pay.company_id == order.company_id
                        and pay.currency_id == order.currency_id
                        and pay.partner_id.commercial_partner_id == order.partner_id.commercial_partner_id)
                    for pay in payments]
        if historical:
            plan = transactions.sorted('id')[:1]._historical_refund_plan(
                self.cutover_date, self.timezone, self.sales_journal_id, self.post_historical,
                opening_account=self.historical_collection_account_id,
                opening_confirmed=self.clear_historical_collections, source=source)
            return self._with_opening_evidence(transactions, plan)
        if collection:
            if len(transactions) != 1:
                raise UserError(_('Review each historical collection against its exact payout transaction.'))
            opening = transactions._historical_collection_plan(
                self.cutover_date, self.timezone, self.historical_collection_account_id,
                self.clear_historical_collections, source=source)
            return self._with_opening_evidence(transactions, opening)
        if len(order) != 1:
            raise UserError(_('Import or correct the order before repairing its current-period accounting.'))
        order.check_access('write')
        plan = order._build_shopify_cash_plan(repair=True, source=source)
        if (plan['mode'] == 'archived_refund_cash' and any(
                source_date(event['timestamp'], self.timezone) < self.cutover_date for event in plan['events'])):
            raise UserError(_('This archived order has cash before cutover. Review its opening-balance treatment before recording customer payments.'))
        return dict(kind='order', order_id=order.id, cash=plan)

    def action_preview(self):
        return self._refresh_preview()

    def _refresh_preview(self, case=None):
        self._check_manager()
        self.env.cr.execute('SELECT id FROM shopify_payout_repair_ept WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        groups, exceptions, cash_only_sources = {}, [], {}
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
            evidence = {}
            try:
                orders = (transaction.payout_id._shop_cash_orders_for_preview(transaction)
                          if transaction.shop_cash_kind else transaction._repair_order())
                cash_only_order = False
                if (orders and not transaction.shop_cash_kind and orders.state == 'cancel'
                        and not orders.invoice_ids.filtered(lambda move: move.state != 'cancel')):
                    if orders.id not in cash_only_sources:
                        cash_only_sources[orders.id] = orders._shopify_cash_source()
                    payload, source_transactions = cash_only_sources[orders.id]
                    if is_cash_only_shopify_order(payload):
                        try:
                            source_events = cash_events(source_transactions, orders.shopify_order_id, orders.currency_id.name)
                            cash_only_order = all(source_date(row['timestamp'], self.timezone) >= self.cutover_date
                                                  for row in source_events)
                        except ValueError as error:
                            raise UserError(str(error)) from error
                return_plan = transaction._refund_return_plan(self, evidence=evidence)
                if return_plan:
                    key = ('return_refund', transaction.payout_id.instance_id.id, return_plan['refund_id'])
                    if key not in groups:
                        groups[key] = [self.env['shopify.payout.report.line.ept'], orders, return_plan, evidence]
                    groups[key][0] |= transaction
                elif transaction._refund_credit_reference():
                    groups[('credit_link', transaction.id)] = [transaction, orders, self._plan(transaction, orders), evidence]
                elif transaction.shop_cash_kind:
                    for order in orders:
                        key = ('order', order.id)
                        if key not in groups:
                            groups[key] = [self.env['shopify.payout.report.line.ept'], order, False, {}]
                        groups[key][0] |= transaction
                elif orders and (cash_only_order or orders.invoice_ids.filtered(lambda move: move.state != 'cancel')):
                    key = ('order', orders.id)
                    if key not in groups:
                        groups[key] = [self.env['shopify.payout.report.line.ept'], orders, False, {}]
                    groups[key][0] |= transaction
                elif transaction.transaction_type in ('refund', 'payment_refund'):
                    # Multiple captures/refunds in one document produce one credit.
                    plan = self._plan(transaction, orders, historical=True, evidence=evidence)
                    if plan['kind'] == 'opening_refund':
                        groups[('opening_refund', transaction.id)] = [transaction, orders, plan, evidence]
                        continue
                    key = ('historical', transaction.payout_id.instance_id.id, plan['values']['shopify_refund_id'])
                    if key not in groups:
                        groups[key] = [self.env['shopify.payout.report.line.ept'], orders, plan, evidence]
                    groups[key][0] |= transaction
                elif orders:
                    key = ('order', orders.id)
                    if key not in groups:
                        groups[key] = [self.env['shopify.payout.report.line.ept'], orders, False, {}]
                    groups[key][0] |= transaction
                else:
                    plan = self._plan(transaction, orders, collection=True, evidence=evidence)
                    groups[('collection', transaction.id)] = [transaction, orders, plan, evidence]
            except UserError as error:
                exceptions.append((transaction, orders if len(orders) == 1 else self.env['sale.order'],
                                   str(error), getattr(error, 'repair_route', 'review'), evidence,
                                   getattr(error, 'dependency_order_id', False)))
        values = []
        for transactions, order, historic_plan, evidence in groups.values():
            plan, error, route = None, '', 'review'
            try:
                plan = historic_plan or self._plan(transactions, order, evidence=evidence)
            except UserError as problem:
                error = str(problem)
                route = getattr(problem, 'repair_route', 'review')
            summary = error or self._summary(plan)
            values.append(dict(wizard_id=self.id, transaction_ids=[Command.set(transactions.ids)],
                               order_id=order.id, kind=historic_plan['kind'] if historic_plan else 'order',
                               source_reference=order.display_name if order else (historic_plan or {}).get(
                                   'source_name', transactions[:1].source_order_id),
                               status='review' if error else 'ready', selected=not error,
                               plan=json.loads(json.dumps(plan, default=str)) if plan else False,
                               detail=summary, resolution=route, review_evidence=evidence,
                               dependency_order_id=False))
        values.extend(dict(wizard_id=self.id, transaction_ids=[Command.set(transaction.ids)],
                           order_id=order.id,
                           source_reference=transaction.source_order_id or transaction.transaction_id,
                           kind='review', status='review', selected=False, detail=error, resolution=route,
                           review_evidence=evidence, dependency_order_id=dependency)
                      for transaction, order, error, route, evidence, dependency in exceptions)
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
        if plan['kind'] == 'return_refund':
            return _('Loop return %(return_name)s: warehouse receipt %(receipts)s completed; %(action)s credit %(credit)s and record/reuse %(count)s refund payment(s) for %(amount)s. Match the payout to finish settlement.',
                     return_name=plan['return_name'], receipts=', '.join(plan['receipt_names']),
                     action=_('reuse') if plan['accounting']['credit_id'] else _('create'),
                     credit=plan['credit_name'] or _('new'), count=len(plan['accounting']['events']),
                     amount=plan['accounting']['amount'])
        if plan['kind'] == 'credit_link':
            return _('Reuse the reviewed refund credit %s and match its existing payout statement.',
                     ', '.join(self.env['account.move'].browse(plan['credit_ids']).mapped('display_name')))
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
        if cash['mode'] == 'prepayment':
            return _('%(count)s captured payment(s): record customer credit now for the payout and apply the same payment(s) when the shipment invoice posts.',
                     count=len(cash['events']))
        if cash['mode'] == 'archived_refund_cash':
            return _('Fully refunded archived order: record/reuse %(count)s original cash movements and match their payouts. '
                     'Customer balance clears; no invoice or credit note is created.', count=len(cash['events']))
        return _('%(mode)s: %(count)s cash transactions; %(replace)s net payment(s) to reverse; %(credits)s credit(s) to create. The invoice total is retained.',
                 mode=cash['mode'], count=len(cash['events']), replace=len(cash['replace_ids']), credits=len(cash['credits']))

    def action_apply(self):
        self._check_manager()
        self.env.cr.execute('SELECT id FROM shopify_payout_repair_ept WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if self.preview_settings != self._settings():
            raise UserError(_('Settings changed. Refresh the preview before applying.'))
        selected = self.line_ids.filtered(lambda row: row.selected and row.status == 'ready'
                                         and row.reconciliation_status != 'matched')
        if not selected:
            if self.line_ids and all(line.reconciliation_status == 'matched' for line in self.line_ids):
                self.result = _('All cases are already matched. No accounting or matching was repeated.')
                return self._reopen()
            raise UserError(_('Select at least one ready repair.'))
        if selected.filtered(lambda line: not self._case_is_visible(line)):
            raise UserError(_('Some selected cases are hidden by the current filters. Clear Selection and select the visible Ready cases before applying.'))
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
                    if fresh['kind'] == 'return_refund':
                        credit, payments = line.transaction_ids.sorted('id')[:1]._apply_refund_return_plan(
                            fresh, line.transaction_ids.payout_id)
                        line.write({'credit_ids': [Command.set(credit.ids)], 'payment_ids': [Command.set(payments.ids)]})
                    elif fresh['kind'] == 'credit_link':
                        line.write({'credit_ids': [Command.set(fresh['credit_ids'])]})
                    elif fresh['kind'] == 'historical':
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
                line.write({'status': 'review', 'selected': False, 'detail': str(error), 'last_error': str(error),
                            'resolution': getattr(error, 'repair_route', 'review')})
                line._record_history('failure')
                failed += 1
        matching_issues = []
        if self.rematch_payouts:
            for payout in selected.filtered(lambda row: row.status == 'done').transaction_ids.payout_id:
                try:
                    with self.env.cr.savepoint():
                        payout._check_payout_reimport_period()
                        reviewed = selected.filtered(lambda row: row.status == 'done' and (
                            row.payment_ids or row.collection_move_ids or row.kind == 'credit_link')).transaction_ids.filtered(
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
                             ('return_refund', 'Received Return Refund'),
                             ('credit_link', 'Existing Refund Credit'),
                             ('review', 'Needs Review')], readonly=True)
    status = fields.Selection([('ready', 'Ready'), ('review', 'Needs Review'), ('done', 'Applied')], readonly=True)
    detail = fields.Text(readonly=True)
    resolution = fields.Selection(REPAIR_ROUTES, default='review', readonly=True, string='Issue')
    summary = fields.Char(compute='_compute_summary', string='Summary')
    review_evidence = fields.Json(readonly=True)
    evidence_html = fields.Html(compute='_compute_evidence_html', string='Source and Accounting Evidence')
    dependency_order_id = fields.Char(string='Missing Shop Cash Order', readonly=True)
    can_import_customer = fields.Boolean(compute='_compute_can_import_customer')
    can_link_credit = fields.Boolean(compute='_compute_can_import_customer')
    next_step = fields.Char(compute='_compute_next_step', string='Next Step')
    plan = fields.Json(readonly=True)
    settings_fingerprint = fields.Char(readonly=True)
    last_preview_at = fields.Datetime(string='Last Preview', readonly=True)
    last_apply_at = fields.Datetime(string='Last Applied', readonly=True)
    last_error = fields.Text(string='Latest Processing Issue', readonly=True)
    history_ids = fields.One2many('shopify.payout.repair.history.ept', 'case_id', readonly=True)
    review_html = fields.Html(compute='_compute_review_html', string='Accounting Preview')

    @api.depends('detail')
    def _compute_summary(self):
        for line in self:
            value = ' '.join((line.detail or '').split())
            line.summary = value[:117] + '…' if len(value) > 120 else value

    @api.depends('review_evidence')
    def _compute_evidence_html(self):
        def table(headers, rows):
            return (Markup('<table class="table table-sm"><thead><tr>')
                + Markup('').join(Markup('<th>%s</th>') % escape(header) for header in headers)
                + Markup('</tr></thead><tbody>')
                + Markup('').join(Markup('<tr>') + Markup('').join(Markup('<td>%s</td>') % escape(str(value))
                    for value in row) + Markup('</tr>') for row in rows)
                + Markup('</tbody></table>'))
        for line in self:
            evidence = line.review_evidence or {}
            if not evidence:
                line.evidence_html = False
                continue
            sections = [Markup('<p>Source order %s · %s · Cutover %s (%s).</p>') %
                tuple(escape(evidence.get(key, '')) for key in ('order_id', 'currency', 'cutover', 'timezone'))]
            if evidence.get('refund_origin'):
                sections.append(Markup('<p>Refund origin: %s.</p>') % escape(evidence['refund_origin']))
            if evidence.get('loop_return'):
                record = evidence['loop_return']
                sections.append(table([_('Loop return'), _('Warehouse status'), _('Receipts')],
                                      [[record['name'], record['state'], record['receipts']]]))
            if evidence.get('source_issue'):
                sections.append(Markup('<p>Source history needs review: %s</p>') % escape(evidence['source_issue']))
            documents = evidence.get('documents', [])
            payments = evidence.get('payments', [])
            posted = [row for row in documents if row['state'] == 'posted' and row['compatible']]
            invoices = sum(row['amount'] for row in posted if row['kind'] == 'out_invoice')
            credits = sum(row['amount'] for row in posted if row['kind'] == 'out_refund')
            if 'net' in evidence:
                totals = [[_('Shopify collections'), evidence['gross']], [_('Shopify refunds'), evidence['refunds']],
                          [_('Shopify net cash'), evidence['net']]]
                if 'documents' in evidence:
                    totals.extend([[_('Posted invoices'), invoices], [_('Posted credit notes'), credits],
                        [_('Invoice less credits'), invoices - credits],
                        [_('Documents minus Shopify net cash'), round(invoices - credits - float(evidence['net']), 6)],
                        [_('Posted receipts less refund payments'), sum(row['amount'] * (
                            -1 if row['direction'] == 'outbound' else 1) for row in payments
                            if row['posted'] and row['compatible'])]])
                sections.append(table([_('Comparison'), evidence['currency']], totals))
            if 'documents' in evidence:
                sections.append(Markup('<p>Totals include posted documents for this customer, company and currency. '
                    'A difference needs review; it does not authorize a write-off or new sales invoice. '
                    'For migrated sales, confirm the opening balances before creating any invoice.</p>'))
                sections.append(table([_('Document'), _('Status'), _('Total'), _('Open'), _('Currency'),
                    _('Customer'), _('Company'), _('Same scope')], [[row[key] for key in
                    ('name', 'state', 'amount', 'residual', 'currency', 'customer', 'company', 'compatible')]
                    for row in documents]))
                if not documents:
                    sections.append(Markup('<p>No active invoice or credit note is linked to this order.</p>'))
            if payments:
                sections.append(table([_('Payment'), _('Direction'), _('Status'), _('Date'), _('Amount'),
                    _('Currency'), _('Source transaction')], [[row[key] for key in
                    ('name', 'direction', 'state', 'date', 'amount', 'currency', 'transaction')] for row in payments]))
            if evidence.get('events'):
                sections.append(table([_('Shopify transaction'), _('Type'), _('Processor'), _('Source time'), _('Amount')],
                    [[row[key] for key in ('id', 'kind', 'gateway', 'timestamp', 'amount')] for row in evidence['events']]))
            sections.append(Markup('<p>Historical fulfillment: %s</p>') % escape(evidence.get('fulfillment_result', '')))
            sections.append(Markup('<p>Shopify payment status: %s. Fulfillment status: %s. Cancelled at: %s. Archived at: %s.</p>') %
                tuple(escape(evidence.get(key) or '—') for key in ('financial_status', 'fulfillment_status', 'cancelled_at', 'archived_at')))
            if evidence.get('fulfillment'):
                sections.append(table([_('Fulfillment'), _('Status'), _('Source time'), _('Before cutover'), _('Issue')],
                    [[row.get(key, '') for key in ('id', 'status', 'timestamp', 'before_cutover', 'issue')]
                    for row in evidence['fulfillment']]))
            if evidence.get('items'):
                sections.append(table([_('Source item'), _('Item'), _('SKU'), _('Required'), _('Proven before cutover'), _('Unproven'), _('Issue')],
                    [[row.get(key, '') for key in ('id', 'name', 'sku', 'required', 'fulfilled', 'missing', 'issue')]
                    for row in evidence['items']]))
            line.evidence_html = Markup('').join(sections)

    def _customer_import_eligible(self):
        self.ensure_one()
        refunds = self.transaction_ids.filtered(lambda tx:
            tx.transaction_type in ('refund', 'payment_refund') and not tx.shop_cash_kind)
        return (((self.resolution == 'customer' and self.status == 'review') or self.status == 'ready') and bool(refunds)
            and refunds == self.transaction_ids and len(set(refunds.mapped('source_order_id'))) == 1
            and bool(refunds[:1].source_order_id) and len(refunds.payout_id.instance_id) == 1
            and not self.current_order_id and self.reconciliation_status != 'matched')

    @api.depends('resolution', 'status', 'transaction_ids', 'transaction_ids.transaction_type',
                 'transaction_ids.shop_cash_kind', 'transaction_ids.source_order_id',
                 'current_order_id', 'reconciliation_status')
    def _compute_can_import_customer(self):
        for line in self:
            line.can_import_customer = (line.resolution == 'customer' and line.status == 'review'
                                       and line._customer_import_eligible())
            refunds = line.transaction_ids.filtered(lambda tx:
                tx.transaction_type in ('refund', 'payment_refund') and not tx.shop_cash_kind)
            line.can_link_credit = len(refunds) == 1 and refunds == line.transaction_ids and line.reconciliation_status != 'matched'

    def _dependency(self):
        self.ensure_one()
        self.check_access('read')
        transactions = self.transaction_ids.filtered('shop_cash_kind')
        if (self.resolution != 'shop_cash' or not self.dependency_order_id
                or len(transactions.payout_id.instance_id) != 1
                or transactions.payout_id - self.wizard_id.payout_ids
                or self.dependency_order_id not in {str(item.get('order_id'))
                    for transaction in transactions for item in transaction.shop_cash_allocations or []}):
            raise UserError(_('Refresh this case to identify its exact Shop Cash order dependency.'))
        return transactions[:1].payout_id.instance_id

    def action_open_dependency_order(self):
        from urllib.parse import quote
        instance = self._dependency()
        host = (instance.shopify_host or '').removeprefix('https://').removeprefix('http://').strip('/')
        if not host or '/' in host:
            raise UserError(_('Configure the store hostname first.'))
        return {'type': 'ir.actions.act_url', 'url': 'https://%s/admin/orders/%s' %
            (host, quote(self.dependency_order_id, safe='')), 'target': 'new'}

    def action_review_dependency_import(self):
        self.wizard_id._check_manager()
        instance = self._dependency()
        if self.env['sale.order'].search_count([('shopify_order_id', '=', self.dependency_order_id),
                ('shopify_instance_id', '=', instance.id), ('company_id', '=', self.company_id.id)]):
            raise UserError(_('The dependency order is now imported. Refresh this case to continue.'))
        action = self.env['ir.actions.act_window']._for_xml_id(
            'shopify_ept.action_wizard_shopify_instance_import_export_operations')
        action['context'] = dict(self.env.context, default_shopify_instance_id=instance.id,
            default_shopify_operation='import_orders_by_remote_ids', default_shopify_order_ids=self.dependency_order_id)
        return action

    def action_link_refund_credit_note(self):
        self.ensure_one()
        refunds = self.transaction_ids.filtered(lambda tx: tx.transaction_type in ('refund', 'payment_refund') and not tx.shop_cash_kind)
        if len(refunds) != 1:
            raise UserError(_('Open the individual payout refund to link its credit note.'))
        return refunds.action_link_refund_credit_note()
    credit_ids = fields.Many2many('account.move', readonly=True)
    payment_ids = fields.Many2many('account.payment', readonly=True)
    collection_move_ids = fields.Many2many('account.move', 'shopify_collection_repair_move_rel',
                                          'case_id', 'move_id', readonly=True, string='Opening-Balance Clearing Entries')
    reconciliation_status = fields.Selection([('matched', 'Matched'), ('pending', 'Awaiting Match')],
        compute='_compute_reconciliation_status', string='Payout Match')

    @api.depends('resolution', 'status', 'reconciliation_status', 'review_evidence')
    def _compute_next_step(self):
        routes = {
            'customer': _('Import Customer, then review the refreshed refund plan.'),
            'documents': _('Open Invoices / Credits or Open Order, correct the documents, then Refresh Case.'),
            'credit': _('Review the refund allocation and prepare/reuse its credit; Link Credit Note checks the source before matching.'),
            'source': _('Open Shopify and verify fulfillment, cancellation and processor history, then Refresh Case.'),
            'payments': _('Review Payments to inspect the exact source transactions and existing receipts.'),
            'invoice_receipt': _('Review Payments, then Preview Invoice Receipt Correction; inspect the reversal before applying.'),
            'shop_cash': _('Open Dependency in Shopify, verify cutover treatment, then Review Order Import and Refresh Case.'),
            'review': _('Review Details and the recorded issue, correct the evidence/accounting, then Refresh Case.'),
        }
        for case in self:
            if case.reconciliation_status == 'matched':
                case.next_step = _('Matched. Continue with the next unmatched case.')
            elif case.status == 'ready':
                case.next_step = _('Review Details, then Apply Selected Ready Cases.')
            elif case.status == 'done':
                case.next_step = _('Accounting applied. Review the matching issue and Refresh Case to retry.')
            elif (case.review_evidence or {}).get('loop_return') and case.resolution == 'documents':
                case.next_step = _('Open the Loop return, complete all WH/IN receipts and backorders, review its credit notes, then Refresh Case.')
            else:
                case.next_step = routes.get(case.resolution, routes['review'])

    def action_review_payments(self):
        self.ensure_one()
        self.check_access('write')
        self.wizard_id._check_manager()
        order = self.current_order_id
        if (not order or self.reconciliation_status == 'matched'
                or self.transaction_ids.payout_id - self.wizard_id.payout_ids
                or order.company_id != self.company_id
                or self.transaction_ids.payout_id.instance_id != order.shopify_instance_id
                or any(tx.source_order_id != order.shopify_order_id for tx in self.transaction_ids if not tx.shop_cash_kind)):
            raise UserError(_('Review payments only for an unmatched case belonging to one current Shopify order.'))
        preview = self.env['shopify.payment.repair.ept'].create({
            'order_ids': [Command.set(order.ids)], 'repair_case_id': self.id})
        return preview.action_preview()

    def action_open_accounting_documents(self):
        self.ensure_one()
        self.check_access('read')
        order = self.current_order_id
        order.check_access('read')
        documents = order.invoice_ids | self.credit_ids
        if order:
            documents |= self.env['account.move'].search([
                ('shopify_refund_order_id', '=', order.id), ('move_type', '=', 'out_refund')])
        documents.check_access('read')
        if not documents:
            return self.action_open_order()
        action = {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                  'name': _('Case Invoices / Credits'), 'view_mode': 'list,form',
                  'domain': [('id', 'in', documents.ids)], 'target': 'current',
                  'context': {'create': False}}
        if len(documents) == 1:
            action.update(view_mode='form', res_id=documents.id)
        return action

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

    def _record_history(self, operation, detail=None):
        self.ensure_one()
        return self.env['shopify.payout.repair.history.ept']._record_event({
            'workspace_id': self.wizard_id.id, 'case_id': self.id, 'company_id': self.company_id.id,
            'payout_ids': [Command.set(self.payout_ids.ids)], 'operation': operation,
            'status': self.status, 'reconciliation_status': self.reconciliation_status,
            'source_reference': self.source_reference, 'source_order_id': self.transaction_ids[:1].source_order_id,
            'order_id': self.current_order_id.id, 'detail': detail if detail is not None else self.last_error or self.detail,
            'plan': self.plan, 'payment_ids': [Command.set(self.payment_ids.ids)],
            'review_evidence': self.review_evidence,
            'credit_ids': [Command.set(self.credit_ids.ids)],
            'collection_move_ids': [Command.set(self.collection_move_ids.ids)],
        })

    def action_refresh_case(self):
        self.ensure_one()
        return self.wizard_id._refresh_preview(case=self)

    def action_import_customer(self):
        self.ensure_one()
        self.check_access('write')
        self.wizard_id._check_manager()
        if self.transaction_ids.payout_id - self.wizard_id.payout_ids:
            raise UserError(_('The repair contains transactions outside the selected payouts.'))
        refunds = self.transaction_ids.filtered(lambda tx:
            tx.transaction_type in ('refund', 'payment_refund') and not tx.shop_cash_kind)
        if not self._customer_import_eligible() or refunds[:1]._repair_order():
            raise UserError(_('Import a customer only for an unmatched refund case with one missing source order.'))
        customer_id, partner = refunds.sorted('id')[:1]._import_repair_customer()
        self._record_history('customer', detail=_('Shopify customer %s mapped to %s (customer %s). Customer import only; review the refreshed accounting preview before applying.',
            customer_id, partner.display_name, partner.id))
        return self.action_refresh_case()

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
            if plan.get('kind') == 'return_refund':
                sections.append(Markup('<p>Loop return %s. Completed warehouse receipts: %s.</p>') %
                    (escape(plan['return_name']), escape(', '.join(plan['receipt_names']))))
                plan = dict(plan['accounting'], kind='historical' if plan['accounting']['values'] else 'return_credit')
            if plan.get('kind') == 'return_credit':
                credit = self.env['account.move'].browse(plan['credit_id'])
                sections.append(Markup('<p>Reuse credit %s: %s %s. Retain its posted accounts and taxes.</p>') %
                    (escape(credit.display_name), escape(str(credit.amount_total)), escape(credit.currency_id.name)))
                events = plan['events']
            elif plan.get('kind') == 'credit_link':
                notes = self.env['account.move'].browse(plan['credit_ids'])
                sections.append(Markup('<p>Reuse reviewed credit: %s. Match the exact payout refund; retain its posted accounts and taxes.</p>') %
                                escape(', '.join(notes.mapped('display_name'))))
                events = []
            elif plan.get('kind') == 'historical':
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
                if cash.get('mode') == 'prepayment':
                    sections.append(Markup('<p>Record customer prepayment for payout reconciliation. Leave customer credit open '
                                           'until the shipment invoice posts, then apply the same payment. '
                                           'No invoice is created; the payment date and payout match are retained.</p>'))
                if cash.get('mode') == 'archived_refund_cash':
                    sections.append(Markup('<p>Fully refunded archived order with zero current value, all items removed and no fulfillment. '
                                           'Record/reuse the original receipt and refund separately, clear their customer balance, '
                                           'and match each exact payout line. No invoice, credit note or Shopify cancellation is created.</p>'))
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
