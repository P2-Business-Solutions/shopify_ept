"""Exercise settlement review decisions with Odoo 18's checked field."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from datetime import timedelta
from xml.etree import ElementTree

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
if ODOO_AVAILABLE:
    from odoo import Command, fields


source = ast.parse((Path(__file__).resolve().parents[1] / 'models' / 'shopify_payout_settlement.py').read_text())
model = next(node for node in source.body if isinstance(node, ast.ClassDef))
compute = next(node for node in model.body if isinstance(node, ast.FunctionDef)
               and node.name == '_compute_settlement_status')
compute.decorator_list = []
namespace = {}
exec(compile(ast.fix_missing_locations(ast.Module(body=[compute], type_ignores=[])),
             '<settlement review>', 'exec'), namespace)


class Records(list):
    def __or__(self, other):
        return Records([*self, *(other if isinstance(other, Records) else [other])])

    def filtered(self, predicate):
        return Records(item for item in self if predicate(item))

    def mapped(self, name):
        return [getattr(item, name) for item in self]


class TestShopifyPayoutReview(unittest.TestCase):
    def setUp(self):
        self.source = SimpleNamespace(is_reconciled=True,
                                      move_id=SimpleNamespace(state='posted', checked=True))
        self.line = SimpleNamespace(matched_debit_ids=Records(), matched_credit_ids=Records(),
                                    reconciled=False, amount_currency=97,
                                    currency_id=SimpleNamespace(compare_amounts=lambda a, b: a - b))
        self.payout = SimpleNamespace(
            settlement_line_id=self.line, settlement_bank_journal_id=object(),
            settlement_move_id=SimpleNamespace(state='posted', reversal_move_ids=Records()),
            payout_statement_line_ids=Records([self.source]),
        )
        self.payouts = Records([self.payout])
        self.payouts.env = {'account.bank.statement.line': Records()}

    def _status(self):
        namespace['_compute_settlement_status'](self.payouts)
        return self.payout.settlement_status

    def test_source_review_can_be_required_and_cleared(self):
        self.assertEqual(self._status(), 'pending')
        self.source.move_id.checked = False
        self.assertEqual(self._status(), 'review')
        self.source.move_id.checked = True
        self.assertEqual(self._status(), 'pending')

    def test_bank_match_requires_checked_receipt_for_both_payout_directions(self):
        for amount in (97, -97):
            with self.subTest(amount=amount):
                bank = SimpleNamespace(journal_id=self.payout.settlement_bank_journal_id,
                                       move_id=SimpleNamespace(state='posted', checked=True),
                                       payout_id=False, is_reconciled=True)
                counterpart = SimpleNamespace(move_id=SimpleNamespace(statement_line_id=bank))
                partial = SimpleNamespace(
                    debit_move_id=self.line if amount > 0 else counterpart,
                    credit_move_id=counterpart if amount > 0 else self.line,
                    debit_amount_currency=97, credit_amount_currency=97,
                )
                self.line.matched_debit_ids = Records([partial]) if amount < 0 else Records()
                self.line.matched_credit_ids = Records([partial]) if amount > 0 else Records()
                self.line.amount_currency = amount
                self.line.reconciled = True
                self.assertEqual(self._status(), 'matched')
                bank.move_id.checked = False
                self.assertEqual(self._status(), 'review')
                bank.move_id.checked = True
                self.assertEqual(self._status(), 'matched')

    def test_reviewed_but_unreconciled_source_still_requires_review(self):
        self.source.is_reconciled = False
        self.assertEqual(self._status(), 'review')


class TestShopifyPayoutFilters(PayoutTestCase):
    def _filter(self, name, payouts):
        view = ElementTree.fromstring(self.env.ref('shopify_ept.shopify_payout_report_search_view').arch_db)
        domain = ast.literal_eval(view.find('.//filter[@name="%s"]' % name).get('domain'))
        return self.env[payouts._name].search(domain + [('id', 'in', payouts.ids)])

    def _matched_payout(self, name):
        payout = self._payout(name)
        self.journal.autocheck_on_post = True
        payout.generate_bank_statement()
        for line in payout.payout_statement_line_ids:
            _liquidity, suspense, _other = line._seek_for_lines()
            suspense.account_id = self.instance.transaction_line_ids.account_id
        payout.validate_statement()
        return payout

    def test_remaining_and_payment_exception_filters_ignore_old_notes_and_bank_only_work(self):
        closed = self._matched_payout('matched-old-note')
        closed.reimport_reconciliation_issue = 'Old processing warning'
        open_payout = self._payout('truly-open')
        open_payout.reimport_reconciliation_issue = 'Current missing payment'
        offset = self._payout('offset-open')
        charge = offset.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'charge')
        charge.fee = 0
        offset.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'fees').amount = 0
        offset.payout_transaction_ids = [Command.create({'transaction_type': 'refund', 'amount': -100})]
        payouts = closed | open_payout | offset
        self.assertEqual(offset.unreconciled_total, 0)
        self.assertEqual(offset.unreconciled_transaction_count, 2)
        self.assertEqual(closed.settlement_status, 'not_created')
        self.assertFalse(closed.has_open_payment_exceptions)
        self.assertTrue(open_payout.has_open_payment_exceptions)
        self.assertEqual(set(self._filter('remaining_reports', payouts).ids), set((open_payout | offset).ids))
        self.assertEqual(self._filter('open_payment_exceptions', payouts), open_payout)
        self.assertEqual(self._filter('missing_settlement', payouts), closed)
        self.assertEqual(self._filter('fully_reconciled', payouts), closed)

    def test_validation_review_is_separate_from_unreconciled_payments(self):
        payout = self._matched_payout('validation-review')
        payout.write({'state': 'partially_processed', 'reimport_reconciliation_issue': 'Review payout totals'})
        self.assertFalse(self._filter('remaining_reports', payout))
        self.assertFalse(self._filter('open_payment_exceptions', payout))
        self.assertEqual(self._filter('validation_review', payout), payout)

    def test_open_exception_flag_updates_after_manual_matching_without_erasing_history(self):
        payout = self._payout('manual-match')
        self.journal.autocheck_on_post = True
        payout.generate_bank_statement()
        payout.reimport_reconciliation_issue = 'Missing payment from prior processing'
        self.assertTrue(payout.has_open_payment_exceptions)
        for line in payout.payout_statement_line_ids:
            _liquidity, suspense, _other = line._seek_for_lines()
            suspense.account_id = self.instance.transaction_line_ids.account_id
        self.assertFalse(payout.has_open_payment_exceptions)
        self.assertTrue(payout.reimport_reconciliation_issue)

    def test_payouts_default_to_oldest_date_first(self):
        later = self._payout('created-first')
        earlier = self._payout('created-second')
        earlier.payout_date = fields.Date.today() - timedelta(days=2)
        selected = later | earlier
        self.assertEqual(self.env[selected._name].search([('id', 'in', selected.ids)]), earlier | later)
        view = ElementTree.fromstring(self.env.ref('shopify_ept.shopify_payout_report_tree_view').arch_db)
        self.assertEqual(view.get('default_order'), 'payout_date asc, id asc')
