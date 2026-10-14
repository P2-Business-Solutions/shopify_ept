"""Live report coverage: completeness, ledger changes, breadcrumbs and access."""

from datetime import timedelta
from unittest.mock import patch

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from .test_shopify_payout_settlement import TestShopifyPayoutSettlement as SettlementFixtures
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from test_shopify_payout_settlement import TestShopifyPayoutSettlement as SettlementFixtures

if ODOO_AVAILABLE:
    from odoo import Command, fields
    from odoo.exceptions import AccessError, UserError
    from odoo.tests import new_test_user


class TestShopifyPayoutExceptionReport(PayoutTestCase):
    _book_activity = SettlementFixtures._book_activity
    _bank_match = SettlementFixtures._bank_match

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.transit = cls.env['account.account'].create({
            'name': 'Report Transit', 'code': 'RPTTRANS', 'account_type': 'asset_current',
            'reconcile': True, 'company_ids': [Command.set(cls.env.company.ids)]})
        bank_account = cls.journal.default_account_id.copy({'code': 'RPTBANK'})
        cls.bank = cls.journal.copy({'name': 'Report Receiving Bank', 'code': 'RPTB',
                                    'default_account_id': bank_account.id})
        (cls.bank.inbound_payment_method_line_ids | cls.bank.outbound_payment_method_line_ids).write({
            'payment_account_id': cls.transit.id})
        cls.transfer_journal = cls.env['account.journal'].create({
            'name': 'Report Transfers', 'code': 'RPTT', 'type': 'general', 'company_id': cls.env.company.id})
        cls.instance.write({'shopify_payout_bank_journal_id': cls.bank.id,
                            'shopify_payout_transit_account_id': cls.transit.id,
                            'shopify_payout_transfer_journal_id': cls.transfer_journal.id})
        cls.counterpart = cls.instance.transaction_line_ids.account_id
        (cls.journal | cls.bank).autocheck_on_post = True

    def _rows(self, payout, **conditions):
        report = self.env['shopify.payout.exception.report.ept']
        report.invalidate_model()
        return report.search([('payout_id', '=', payout.id)] +
                             [(key, '=', value) for key, value in conditions.items()])

    def _case(self, payout, transaction, **values):
        workspace = self.env['shopify.payout.repair.ept'].create({'payout_ids': [Command.set(payout.ids)]})
        values = dict(wizard_id=workspace.id, transaction_ids=[Command.set(transaction.ids)],
                      source_reference='#saved-order', status='review', detail='Customer identity needs review',
                      resolution='customer', last_preview_at=fields.Datetime.now(), **values)
        return self.env['shopify.payout.repair.line.ept'].create(values)

    def test_missing_statements_and_cases_are_included_without_source_reads_or_accounting(self):
        payout = self._payout('report-no-case')
        before_moves = self.env['account.move'].search_count([])
        before_cases = self.env['shopify.payout.repair.line.ept'].search_count([])
        with patch.object(type(self.instance), 'connect_in_shopify', side_effect=AssertionError('source read')):
            rows = self._rows(payout, is_open=True)
            self.assertEqual(len(rows), 2)
            self.assertEqual(set(rows.mapped('stage')), {'missing_statement'})
            self.assertFalse(rows.case_id)
            self.assertEqual(sum(rows.mapped('remaining_amount')), 97)
            self.assertTrue(all(rows.mapped('next_step')))
        self.assertEqual(self.env['account.move'].search_count([]), before_moves)
        self.assertEqual(self.env['shopify.payout.repair.line.ept'].search_count([]), before_cases)

    def test_offsets_do_not_hide_open_transactions_and_zero_rows_are_excluded(self):
        payout = self._payout('report-offset')
        payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'fees').amount = 0
        payout.payout_transaction_ids = [Command.create({'transaction_type': 'refund', 'amount': -100}),
                                         Command.create({'transaction_type': 'charge', 'amount': 0.001})]
        rows = self._rows(payout, is_open=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(rows.mapped('remaining_amount')), 0)

    def test_matching_and_statement_review_update_same_rows_without_erasing_notes(self):
        payout = self._payout('report-live')
        original_ids = self._rows(payout, scope='transaction').ids
        payout.generate_bank_statement()
        rows = self._rows(payout, scope='transaction')
        self.assertEqual(rows.ids, original_ids)
        self.assertIn('unmatched', rows.mapped('stage'))
        for statement in payout.payout_statement_line_ids:
            _liquidity, suspense, _other = statement._seek_for_lines()
            suspense.account_id = self.counterpart
        payout.reimport_reconciliation_issue = 'Old missing payment warning'
        self.assertEqual(set(self._rows(payout, scope='transaction').mapped('stage')), {'resolved'})
        self.assertEqual(self._rows(payout, is_open=True).stage, 'validation')
        payout.payout_statement_line_ids[:1].move_id.checked = False
        row = self._rows(payout, stage='statement_review')
        self.assertEqual(len(row), 1)
        self.assertEqual(row.remaining_amount, 0)
        self.assertEqual(payout.reimport_reconciliation_issue, 'Old missing payment warning')

    def test_validation_settlement_partial_bank_and_resolution_are_live(self):
        payout = self._payout('report-settlement')
        self._book_activity(payout)
        self.assertEqual(self._rows(payout, is_open=True).stage, 'validation')
        payout.validate_statement()
        self.assertEqual(self._rows(payout, is_open=True).stage, 'settlement')
        payout.action_create_settlement_transfer()
        self.assertEqual(self._rows(payout, is_open=True).stage, 'bank')
        first = self._bank_match(payout, 40)
        row = self._rows(payout, is_open=True)
        self.assertEqual(row.stage, 'bank')
        self.assertEqual(row.settlement_status, 'partial')
        self.assertEqual(row.remaining_amount, 0)
        second = self._bank_match(payout, 57)
        self.assertFalse(self._rows(payout, is_open=True))
        row = self._rows(payout, scope='payout')
        self.assertEqual(row.stage, 'resolved')
        self.assertEqual(row.settlement_bank_line_ids, first | second)
        payout.settlement_line_id.remove_move_reconcile()
        self.assertEqual(self._rows(payout, is_open=True).stage, 'bank')

    def test_partial_cash_residual_preserves_sign_and_booked_exchange_rate(self):
        foreign = self.env.ref('base.EUR') if self.env.company.currency_id != self.env.ref('base.EUR') else self.env.ref('base.USD')
        foreign.active = True
        for use_foreign in (False, True):
            payout = self._payout('report-partial-' + str(use_foreign))
            charge = payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'charge')
            values = {'journal_id': self.journal.id, 'date': payout.payout_date, 'payment_ref': 'Partial cash',
                      'amount': 100, 'payout_id': payout.id, 'payout_line_id': charge.id,
                      'shopify_transaction_type': 'charge'}
            if use_foreign:
                values.update(foreign_currency_id=foreign.id, amount_currency=200)
            statement = self.env['account.bank.statement.line'].create(values)
            _liquidity, suspense, _other = statement._seek_for_lines()
            move = self.env['account.move'].create({
                'journal_id': self.transfer_journal.id, 'date': payout.payout_date,
                'line_ids': [Command.create({'account_id': suspense.account_id.id, 'debit': 40,
                    'currency_id': suspense.currency_id.id, 'amount_currency': 80 if use_foreign else 40}),
                    Command.create({'account_id': self.counterpart.id, 'credit': 40})]})
            move.action_post()
            (suspense | move.line_ids.filtered(lambda line: line.account_id == suspense.account_id)).reconcile()
            row = self._rows(payout, transaction_id=charge.id)
            self.assertEqual(row.stage, 'unmatched')
            self.assertEqual(row.amount, 100)
            self.assertEqual(row.remaining_amount, 60)
    def test_duplicate_statements_are_one_transaction_exception_with_all_links(self):
        payout = self._payout('report-duplicate')
        self._book_activity(payout)
        charge = payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'charge')
        original = payout.payout_statement_line_ids.filtered(lambda row: row.payout_line_id == charge)
        duplicate = self.env['account.bank.statement.line'].create({
            'journal_id': self.journal.id, 'date': payout.payout_date, 'payment_ref': 'Duplicate',
            'amount': charge.amount, 'payout_id': payout.id, 'payout_line_id': charge.id,
            'shopify_transaction_type': 'charge', 'counterpart_account_id': self.counterpart.id})
        row = self._rows(payout, is_open=True)
        self.assertEqual(len(row), 1)
        self.assertEqual(row.stage, 'statement_structure')
        self.assertEqual(row.statement_count, 2)
        action = row.action_open_statements()
        self.assertEqual(self.env['account.bank.statement.line'].search(action['domain']), original | duplicate)

    def test_unlinked_statement_is_visible_even_when_matched(self):
        payout = self._payout('report-orphan')
        self._book_activity(payout)
        statement = self.env['account.bank.statement.line'].create({
            'journal_id': self.journal.id, 'date': payout.payout_date, 'payment_ref': 'Unlinked',
            'amount': 5, 'payout_id': payout.id, 'counterpart_account_id': self.counterpart.id})
        row = self._rows(payout, is_open=True)
        self.assertEqual(row.scope, 'statement')
        self.assertEqual(row.statement_id, statement)
        self.assertEqual(row.remaining_amount, 0)

    def test_amount_mismatch_is_visible_after_matching(self):
        payout = self._payout('report-amount')
        charge = payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'charge')
        # Simulate an already imported, incorrectly linked entry without editing
        # a protected posted transaction to manufacture the fixture.
        self.env['account.bank.statement.line'].create({
            'journal_id': self.journal.id, 'date': payout.payout_date, 'payment_ref': 'Wrong amount',
            'amount': 90, 'payout_id': payout.id, 'payout_line_id': charge.id,
            'shopify_transaction_type': 'charge', 'counterpart_account_id': self.counterpart.id})
        fee = payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'fees')
        self.env['account.bank.statement.line'].create({
            'journal_id': self.journal.id, 'date': payout.payout_date, 'payment_ref': 'Fee',
            'amount': fee.amount, 'payout_id': payout.id, 'payout_line_id': fee.id,
            'shopify_transaction_type': 'fees', 'counterpart_account_id': self.counterpart.id})
        self.assertEqual(self._rows(payout, is_open=True).stage, 'statement_structure')

    def test_empty_payout_has_a_payout_level_exception(self):
        payout = self._payout('report-empty')
        payout.payout_transaction_ids.unlink()
        row = self._rows(payout, is_open=True)
        self.assertEqual(row.stage, 'missing_transactions')
        self.assertEqual(row.scope, 'payout')

    def test_newest_case_is_unique_and_archived_history_and_audit_remain_accessible(self):
        payout = self._payout('report-breadcrumbs')
        charge = payout.payout_transaction_ids[:1]
        old = self._case(payout, charge)
        old._record_history('preview')
        old._record_history('failure', detail='Previous apply failed')
        old.active = False
        newer = self._case(payout, charge)
        newer._record_history('preview')
        audit = self.env['shopify.payout.repair.audit.ept']._record_repair({
            'company_id': self.env.company.id, 'payout_ids': [Command.set(payout.ids)],
            'transaction_ids': [Command.set(charge.ids)], 'source_order_id': charge.source_order_id})
        row = self._rows(payout, transaction_id=charge.id)
        self.assertEqual(len(row), 1)
        self.assertEqual(row.case_id, newer)
        self.assertEqual(row.case_ids, old | newer)
        self.assertEqual(row.history_ids, old.history_ids | newer.history_ids)
        self.assertEqual(row.audit_ids, audit)
        self.assertEqual(row.action_open_case()['res_id'], newer.id)
        self.assertEqual(row.action_open_workspace()['res_id'], newer.wizard_id.id)
        newer.active = False
        row = self._rows(payout, transaction_id=charge.id)
        self.assertFalse(row.case_id)
        self.assertEqual(len(row.history_ids), 3)

    def test_multi_transaction_case_does_not_hide_individual_matches(self):
        payout = self._payout('report-grouped-case')
        case = self._case(payout, payout.payout_transaction_ids)
        payout.generate_bank_statement()
        charge = payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'charge')
        statement = payout.payout_statement_line_ids.filtered(lambda row: row.payout_line_id == charge)
        _liquidity, suspense, _other = statement._seek_for_lines()
        suspense.account_id = self.counterpart
        row = self._rows(payout, transaction_id=charge.id)
        self.assertEqual(row.case_id, case)
        self.assertEqual(row.stage, 'resolved')
        self.assertFalse(row.is_open)

    def test_report_groups_by_issue_and_payout_without_duplicate_case_rows(self):
        payout = self._payout('report-grouping')
        self._case(payout, payout.payout_transaction_ids[:1])
        report = self.env['shopify.payout.exception.report.ept']
        groups = report._read_group([('payout_id', '=', payout.id), ('is_open', '=', True)],
                                    ['resolution'], ['__count'])
        self.assertEqual(dict(groups), {False: 1, 'customer': 1})
        groups = report._read_group([('payout_id', '=', payout.id), ('is_open', '=', True)],
                                    ['payout_id'], ['__count'])
        self.assertEqual(groups, [(payout, 2)])

    def test_ordering_company_access_and_read_only_actions(self):
        payout = self._payout('report-security')
        earlier = self._payout('report-earlier')
        earlier.payout_date -= timedelta(days=1)
        other_company = self.env['res.company'].create({'name': 'Exception report other company'})
        other_instance = self.instance.copy({'name': 'Other company store', 'shopify_company_id': other_company.id,
                                             'shopify_host': 'report-other-company.example.com'})
        hidden = self._payout('report-hidden')
        hidden.instance_id = other_instance
        user = new_test_user(self.env, login='payout_report_reader',
            groups='account.group_account_user,shopify_ept.group_shopify_ept', company_id=self.env.company.id,
            company_ids=[Command.set(self.env.company.ids)])
        report = self.env['shopify.payout.exception.report.ept'].with_user(user).with_context(
            allowed_company_ids=self.env.company.ids)
        rows = report.search([('payout_id', 'in', (payout | earlier | hidden).ids), ('is_open', '=', True)])
        self.assertEqual(rows[:1].payout_id, earlier)
        self.assertNotIn(hidden, rows.payout_id)
        row = rows.filtered(lambda item: item.payout_id == payout)[:1]
        self.assertEqual(row.action_open_payout()['res_id'], payout.id)
        with self.assertRaises(AccessError):
            report.browse(hidden.payout_transaction_ids[:1].id * 4).read(['payout_id'])
        with self.assertRaises(UserError):
            row.action_open_workspace()
        for operation in (lambda: row.write({'stage': 'resolved'}), lambda: row.unlink(),
                          lambda: report.create({'payout_id': payout.id})):
            with self.assertRaises(UserError):
                operation()

    def test_payout_button_retains_open_filter_and_exact_payout_scope(self):
        payouts = self._payout('report-nav-1') | self._payout('report-nav-2')
        action = payouts.action_open_exception_report()
        self.assertEqual(action['res_model'], 'shopify.payout.exception.report.ept')
        self.assertEqual(action['domain'], [('payout_id', 'in', payouts.ids)])
        self.assertIn('search_default_open', action['context'])
