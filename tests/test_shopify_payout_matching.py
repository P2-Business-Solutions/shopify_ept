"""Execute payout matching arithmetic without an Odoo database.

Full ledger/widget behavior is covered separately by the Odoo integration tests.
"""

import ast
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import unittest


source = ast.parse((Path(__file__).resolve().parents[1] / 'models' / 'shopify_payout_report_ept.py').read_text())
model = next(node for node in source.body if isinstance(node, ast.ClassDef))
names = {'get_paid_move_line_amount', 'get_payment_move_line_amount', '_payout_move_line_residual'}
methods = [node for node in model.body if isinstance(node, ast.FunctionDef) and node.name in names]
namespace = {}
exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), '<payout matching>', 'exec'), namespace)
Matching = type('Matching', (), {name: namespace[name] for name in names})


class Records(list):
    def filtered(self, predicate):
        return Records(item for item in self if predicate(item))


class TestShopifyPayoutMatching(unittest.TestCase):
    def setUp(self):
        def compare(a, b):
            a, b = (Decimal(str(value)).quantize(Decimal('.01')) for value in (a, b))
            return (a > b) - (a < b)
        self.currency = SimpleNamespace(id=1, compare_amounts=compare)
        self.company = object()
        self.model = Matching()
        self.model.currency_id = self.currency
        self.model.instance_id = SimpleNamespace(shopify_company_id=self.company)
        self.model.env = {'account.move.line': Records()}

    def _payment(self, direction, amount, transaction_id, residual=None, reconciled=False):
        line = SimpleNamespace(
            move_id=SimpleNamespace(state='posted'), account_id=SimpleNamespace(reconcile=True),
            reconciled=reconciled, balance=amount, currency_id=self.currency,
            amount_residual_currency=amount if residual is None else residual,
        )
        payment = SimpleNamespace(
            payment_type=direction, company_id=self.company, shopify_instance_id=self.model.instance_id,
            shopify_order_transaction_id=transaction_id,
            _seek_for_lines=lambda: (Records([line]), Records(), Records()),
        )
        return payment, line

    def _match(self, amount, payments, transaction_id=False):
        statement = SimpleNamespace(amount=amount, currency_id=self.currency,
                                    shopify_order_transaction_id=transaction_id)
        invoices = SimpleNamespace(payment_state='reversed', reconciled_payment_ids=Records(payments))
        return self.model.get_paid_move_line_amount(statement, invoices)

    def test_original_charge_and_later_refund_are_not_netted(self):
        charge, charge_line = self._payment('inbound', 100, 'charge')
        refund, refund_line = self._payment('outbound', -100, 'refund')
        self.assertEqual(self._match(100, [charge, refund])[2], [charge_line])
        self.assertEqual(self._match(-100, [charge, refund])[2], [refund_line])

    def test_consumed_charge_does_not_prevent_later_refund_match(self):
        charge, _ = self._payment('inbound', 100, 'charge', residual=0, reconciled=True)
        refund, refund_line = self._payment('outbound', -100, 'refund')
        self.assertFalse(self._match(100, [charge, refund])[2])
        self.assertEqual(self._match(-100, [charge, refund])[2], [refund_line])

    def test_partial_payment_uses_remaining_balance(self):
        payment, line = self._payment('inbound', 100, 'charge', residual=30)
        self.assertFalse(self._match(100, [payment])[2])
        self.assertEqual(self._match(30, [payment])[2], [line])

    def test_ambiguous_same_amount_payments_require_transaction_identity(self):
        first, _ = self._payment('inbound', 100, 'first')
        second, second_line = self._payment('inbound', 100, 'second')
        self.assertFalse(self._match(100, [first, second])[2])
        self.assertEqual(self._match(100, [first, second], 'second')[2], [second_line])
