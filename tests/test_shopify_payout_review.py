"""Exercise settlement review decisions with Odoo 18's checked field."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


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
