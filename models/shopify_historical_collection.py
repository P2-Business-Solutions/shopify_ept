"""Clear proven old-system receipts without repeating sales or customer cash."""
from odoo import Command, fields, models, _
from odoo.exceptions import UserError

from .shopify_payment_plan import cash_events, fingerprint
from .shopify_refund_plan import prove_historical_fulfillment, source_date


class ShopifyHistoricalCollectionMove(models.Model):
    _inherit = 'account.move'

    shopify_legacy_collection_transaction_id = fields.Char(
        string='Historical Shopify Collection Transaction', copy=False, index=True)

    _sql_constraints = [
        ('shopify_legacy_collection_unique',
         'unique(shopify_instance_id, shopify_legacy_collection_transaction_id)',
         'A historical Shopify collection can have only one clearing entry.'),
    ]


class ShopifyHistoricalCollectionLine(models.Model):
    _inherit = 'shopify.payout.report.line.ept'

    def _collection_snapshot(self, moves):
        return fingerprint([(move.id, move.state, str(move.write_date), str(move.date),
                             [(line.id, line.account_id.id, line.balance, line.amount_currency,
                               line.amount_residual, line.amount_residual_currency,
                               line.matched_debit_ids.ids, line.matched_credit_ids.ids)
                              for line in move.line_ids.sorted('id')]) for move in moves.sorted('id')])

    def _historical_collection_entry(self):
        """Only immutable reviews can designate a balance-sheet clearing entry."""
        self.ensure_one()
        audits = self.env['shopify.payout.repair.audit.ept'].search([('transaction_ids', 'in', self.ids)])
        moves = self.env['account.move'].search([
            ('shopify_instance_id', '=', self.payout_id.instance_id.id),
            ('shopify_legacy_collection_transaction_id', '=', self.source_order_transaction_id),
        ]) if self.source_order_transaction_id else self.env['account.move']
        if not audits and not moves:
            return moves
        if (len(audits) != 1 or len(moves) != 1 or audits.collection_move_ids != moves
                or audits.transaction_ids != self or audits.payout_ids != self.payout_id):
            raise UserError(_('The historical collection has missing or conflicting clearing/audit links. Review its accounting.'))
        plan = audits.plan
        payout, instance = self.payout_id, self.payout_id.instance_id
        currency, company = payout.currency_id, instance.shopify_company_id
        event = plan.get('event', {})
        if (plan.get('kind') != 'collection' or plan.get('line_id') != self.id
                or self.transaction_type != 'charge' or self.shop_cash_kind
                or event.get('id') != self.source_order_transaction_id
                or plan.get('source_order_id') != self.source_order_id
                or currency.compare_amounts(plan.get('amount', 0), self.amount)
                or audits.company_id != company or moves.company_id != company
                or moves.shopify_source_order_id != self.source_order_id
                or moves.currency_id != currency or moves.state != 'posted'
                or moves.journal_id.id != plan['values']['journal_id']
                or str(moves.date) != plan['values']['date'] or moves.date != payout.payout_date
                or moves.reversal_move_ids.filtered(lambda move: move.state == 'posted')):
            raise UserError(_('The historical collection no longer agrees with its reviewed source or clearing entry.'))
        if self.env['account.payment'].search_count([
                ('shopify_instance_id', '=', instance.id),
                ('shopify_order_transaction_id', '=', self.source_order_transaction_id),
                ('state', 'not in', ('canceled', 'rejected'))]):
            raise UserError(_('This collection also has an Odoo customer payment. Review the duplicate cash before matching.'))
        expected = {command[2]['account_id']: command[2]['debit'] - command[2]['credit']
                    for command in plan['values']['line_ids']}
        if (len(moves.line_ids) != 2 or moves.line_ids.tax_ids or moves.line_ids.tax_line_id
                or set(moves.line_ids.account_id.ids) != set(expected)
                or any(currency.compare_amounts(line.balance, expected[line.account_id.id])
                       or line.currency_id != currency
                       or currency.compare_amounts(line.amount_currency, expected[line.account_id.id])
                       for line in moves.line_ids)):
            raise UserError(_('The historical clearing entry has different accounts, amounts or taxes. Review it before matching.'))
        outstanding = moves.line_ids.filtered(lambda line: line.account_id.id == plan['outstanding_id'])
        matches = outstanding.matched_debit_ids | outstanding.matched_credit_ids
        others = (matches.debit_move_id | matches.credit_move_id) - outstanding
        if others.filtered(lambda line: line.move_id.statement_line_id.payout_line_id != self):
            raise UserError(_('The historical clearing receipt is already allocated outside its exact payout transaction.'))
        return moves

    def _historical_collection_plan(self, cutover, timezone, account, confirmed, source=None):
        self.ensure_one()
        payout, instance = self.payout_id, self.payout_id.instance_id
        currency, company = payout.currency_id, instance.shopify_company_id
        payout._check_payout_reimport_period()
        if self.transaction_type != 'charge' or self.shop_cash_kind or self.amount <= 0 or not cutover:
            raise UserError(_('Historical collection repair needs an individual positive charge and a cutover date.'))
        if not self.source_order_id or not self.source_order_transaction_id:
            raise UserError(_('The payout collection is missing its Shopify order or exact transaction ID. Reimport the payout to refresh its source links, then preview again.'))
        if self._repair_order():
            raise UserError(_('An Odoo order now exists. Refresh and use its normal invoice/payment repair.'))
        payload, transactions = source or self._repair_source()
        name = payload.get('name') or self.source_order_id
        if str(payload.get('id')) != self.source_order_id:
            raise UserError(_('The collection source identifies another Shopify order.'))
        try:
            events = cash_events(transactions, self.source_order_id, currency.name)
            event = next((row for row in events if row['id'] == self.source_order_transaction_id), None)
            if (not event or event['kind'] == 'refund' or event['gateway'] != 'shopify_payments'
                    or currency.compare_amounts(float(event['amount']), self.amount)):
                raise UserError(_('%s: the payout charge must identify its exact successful Shopify Payments collection. Reimport the payout if its source transaction ID is missing.', name))
            if source_date(event['timestamp'], timezone) >= cutover:
                raise UserError(_('%s: this collection is on/after cutover. Import the current-period order using Import/Update Specific Order(s), then refresh this preview.', name))
            prove_historical_fulfillment(payload, cutover, timezone)
            if any(source_date(row['timestamp'], timezone) >= cutover for row in events if row['kind'] != 'refund'):
                raise UserError(_('%s: collections span the cutover. Review the opening-balance allocation separately.', name))
        except ValueError as error:
            raise UserError(_('%(order)s: %(reason)s Import a current-period order only if its sales/fulfillment were not included in migration balances.',
                              order=name, reason=str(error))) from error
        if payout.payout_date < cutover:
            raise UserError(_('%s: the payout predates cutover and needs review of the opening balances.', name))
        if not confirmed or not account:
            raise UserError(_('%s: paid and fulfilled before cutover; this is an opening-balance collection. Select its Opening-Balance Collection Account and enable Clear Collections Included in Opening Balances, then refresh.', name))
        journal = instance.shopify_settlement_report_journal_id
        methods = journal.inbound_payment_method_line_ids.filtered(lambda method: method.code == 'manual')
        outstanding = methods.payment_account_id
        if (journal.company_id != company or journal.type != 'bank' or currency != company.currency_id
                or (journal.currency_id and journal.currency_id != currency) or len(methods) != 1
                or len(outstanding) != 1 or not outstanding.reconcile or outstanding.deprecated
                or outstanding.account_type != 'asset_current'
                or company not in outstanding.company_ids
                or outstanding in (journal.default_account_id, journal.suspense_account_id)
                or (outstanding.currency_id and outstanding.currency_id != currency)):
            raise UserError(_('Configure a same-company Shopify bank journal and one Manual receipt method with a dedicated outstanding receipts account. Historical foreign-currency conversion requires review.'))
        if (account.deprecated or company not in account.company_ids
                or account.account_type not in ('asset_current', 'asset_cash')
                or account in (outstanding, journal.suspense_account_id)
                or (account.currency_id and account.currency_id != currency)):
            raise UserError(_('Select the same-company balance-sheet account holding these original collections, separate from outstanding receipts and bank suspense.'))
        duplicates = self.search([
            ('payout_id.instance_id', '=', instance.id), ('transaction_type', '=', 'charge'),
            ('source_order_transaction_id', '=', event['id']), ('id', '!=', self.id),
        ])
        if duplicates:
            raise UserError(_('Several payout rows identify the same collection. Review their source identity before clearing it.'))
        payments = self.env['account.payment'].search([
            ('shopify_instance_id', '=', instance.id), ('payment_type', '=', 'inbound'),
            ('state', 'not in', ('canceled', 'rejected')), '|',
            ('shopify_order_transaction_id', '=', event['id']), ('shopify_source_order_id', '=', self.source_order_id),
        ])
        partners = self.env['shopify.res.partner.ept'].search([
            ('shopify_instance_id', '=', instance.id),
            ('shopify_customer_id', '=', str((payload.get('customer') or {}).get('id') or '')),
        ]).partner_id.commercial_partner_id
        if partners:
            payments |= self.env['account.payment'].search([
                ('company_id', '=', company.id), ('journal_id', '=', journal.id),
                ('partner_id.commercial_partner_id', 'in', partners.ids), ('payment_type', '=', 'inbound'),
                ('currency_id', '=', currency.id), ('amount', '=', self.amount),
                ('date', 'in', sorted({event['date'], str(source_date(event['timestamp'], timezone)),
                                      str(payout.payout_date)})),
                ('state', 'not in', ('canceled', 'rejected')),
            ])
        if payments:
            raise UserError(_('An Odoo payment may already record this collection. Verify its source reference and use payment matching instead of opening-balance clearing.'))
        statements = payout.payout_statement_line_ids.filtered(lambda row: row.payout_line_id == self)
        if len(statements) > 1:
            raise UserError(_('The collection has several bank statement lines. Review the duplicates.'))
        for statement in statements:
            if (currency.compare_amounts(statement.amount, self.amount) or statement.currency_id != currency
                    or statement.journal_id != journal or statement.date != payout.payout_date
                    or statement.shopify_transaction_id != self.transaction_id
                    or (statement.shopify_order_transaction_id
                        and statement.shopify_order_transaction_id != event['id'])):
                raise UserError(_('The existing collection statement disagrees with its payout source. Reimport/review it first.'))
        move = self._historical_collection_entry()
        if statements:
            liquidity, suspense, counterpart = statements._seek_for_lines()
            if (len(statements.line_ids) != 2 or len(liquidity) != 1 or statements.payment_ids
                    or statements.line_ids.tax_ids or statements.line_ids.tax_line_id
                    or (statements.is_reconciled and (not move or len(counterpart) != 1
                        or counterpart.account_id != outstanding or not counterpart.reconciled))
                    or (not statements.is_reconciled and (len(suspense) != 1 or counterpart
                        or statements.line_ids.matched_debit_ids or statements.line_ids.matched_credit_ids))):
                raise UserError(_('The collection statement has existing or nonstandard accounting. Review its match before clearing historical cash.'))
        amount = self.amount
        values = dict(move_type='entry', company_id=company.id, journal_id=journal.id, currency_id=currency.id,
                      date=str(payout.payout_date), shopify_instance_id=instance.id,
                      shopify_source_order_id=self.source_order_id, shopify_legacy_collection_transaction_id=event['id'],
                      ref='Shopify historical collection %s / %s' % (name, event['id']), line_ids=[
                          Command.create(dict(name='Historical collection %s' % event['id'], account_id=outstanding.id,
                                              debit=amount, credit=0, currency_id=currency.id, amount_currency=amount)),
                          Command.create(dict(name='Clear opening balance %s' % name, account_id=account.id,
                                              debit=0, credit=amount, currency_id=currency.id, amount_currency=-amount)),
                      ])
        if move and (set(move.line_ids.account_id.ids) != set((outstanding | account).ids)
                     or move.journal_id != journal):
            raise UserError(_('The existing historical clearing entry uses different accounts or journal. Retain it and review the allocation.'))
        return dict(kind='collection', line_id=self.id, source_order_id=self.source_order_id,
                    source_name=name, cutover=str(cutover), timezone=timezone, amount=amount,
                    account_id=account.id, outstanding_id=outstanding.id, event=event, values=values,
                    collection_move_id=move.id, entry_snapshot=self._collection_snapshot(move),
                    source=fingerprint({'order': payload, 'transactions': transactions}),
                    fulfillment_evidence=[dict(status=row.get('status'), created_at=row.get('created_at'),
                                               items=[dict(id=item.get('id'), quantity=item.get('quantity'))
                                                      for item in row.get('line_items', [])])
                                          for row in payload.get('fulfillments', [])],
                    payout_snapshot=fingerprint([(row.id, str(row.write_date), row.amount, row.is_reconciled,
                                                 str(row.move_id.write_date)) for row in statements]))

    def _apply_historical_collection_plan(self, plan):
        self.ensure_one()
        move = self.env['account.move'].browse(plan['collection_move_id'])
        if move:
            return move
        company = self.payout_id.instance_id.shopify_company_id
        move = self.env['account.move'].with_company(company).with_context(
            tracking_disable=True, mail_create_nosubscribe=True).create(plan['values'])
        move.action_post()
        if str(move.date) != plan['values']['date']:
            raise UserError(_('Odoo changed the historical clearing entry date. Review the accounting period.'))
        self.env['shopify.payout.repair.audit.ept']._record_repair({
            'payout_ids': [Command.set(self.payout_id.ids)], 'company_id': company.id,
            'source_order_id': self.source_order_id, 'transaction_ids': [Command.set(self.ids)],
            'plan': plan, 'collection_move_ids': [Command.set(move.ids)],
        })
        self._historical_collection_entry()
        return move


class ShopifyHistoricalCollectionPayout(models.Model):
    _inherit = 'shopify.payout.report.ept'

    def _reconcile_historical_collection_statement(self, statement, move):
        self.ensure_one()
        self._check_payout_reimport_period()
        transaction = statement.payout_line_id
        if transaction._historical_collection_entry() != move:
            raise UserError(_('The clearing entry does not belong to this payout collection.'))
        audit = self.env['shopify.payout.repair.audit.ept'].search([('transaction_ids', 'in', transaction.ids)])
        outstanding = move.line_ids.filtered(lambda line: line.account_id.id == audit.plan['outstanding_id'])
        liquidity, suspense, counterpart = statement._seek_for_lines()
        if (statement.payout_id != self or statement.journal_id != move.journal_id
                or statement.currency_id != self.currency_id or statement.date != move.date
                or self.currency_id.compare_amounts(statement.amount, transaction.amount)
                or statement.shopify_transaction_id != transaction.transaction_id
                or (statement.shopify_order_transaction_id
                    and statement.shopify_order_transaction_id != transaction.source_order_transaction_id)
                or len(liquidity) != 1 or len(suspense) != 1 or counterpart
                or len(statement.line_ids) != 2 or statement.payment_ids
                or statement.line_ids.tax_ids or statement.line_ids.tax_line_id
                or statement.line_ids.matched_debit_ids or statement.line_ids.matched_credit_ids
                or len(outstanding) != 1 or outstanding.reconciled
                or self.currency_id.compare_amounts(outstanding.amount_residual_currency, statement.amount)):
            raise UserError(_('The historical collection has changed or already matched accounting. Review its exact statement and clearing entry.'))
        self.shopify_reconcile_bank_statement_line_ept(statement.id, outstanding.ids)
        if not statement.is_reconciled or not outstanding.reconciled:
            raise UserError(_('The historical collection was not completely reconciled.'))
