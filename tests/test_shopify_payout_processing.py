"""Check retry propagation and currency precision without an Odoo database."""

import ast
from decimal import Decimal
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock


class OperationalError(Exception):
    """Stand-in for the database driver's retryable exception in standalone tests."""


class Records(list):
    def filtered(self, predicate):
        return Records(row for row in self if predicate(row))


source = ast.parse((Path(__file__).resolve().parents[1] / 'models' / 'shopify_payout_report_ept.py').read_text())
model = next(node for node in source.body if isinstance(node, ast.ClassDef))
names = {'process_bank_statement', 'reconcile_invoice_refund', 'check_process_statement'}
methods = [node for node in model.body if isinstance(node, ast.FunctionDef) and node.name in names]
namespace = {'OperationalError': OperationalError, 'UserError': ValueError,
             '_logger': logging.getLogger(__name__)}
exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
             '<payout processing>', 'exec'), namespace)
Processing = type('Processing', (), {name: namespace[name] for name in names})


class TestShopifyPayoutProcessing(unittest.TestCase):
    def setUp(self):
        self.payout = Processing()
        self.payout.id = 1
        self.payout.name = 'Test payout'
        self.payout.ensure_one = Mock()
        self.payout._lock_settlement_payouts = Mock()
        self.payout._context = {'cron_process': True}
        self.savepoint = MagicMock()
        self.env = MagicMock()
        self.env.cr.savepoint.return_value = self.savepoint
        self.payout.env = self.env
        self.statement = SimpleNamespace(id=2, is_reconciled=False, amount=97,
                                         shopify_transaction_type='adjustment')
        self.env.__getitem__.return_value.search.return_value = Records([self.statement])
        self.payout.currency_id = SimpleNamespace(compare_amounts=lambda a, b: a - b)

    def test_processing_propagates_database_conflict_for_request_retry(self):
        self.payout.reconcile_other_transactions = Mock(side_effect=OperationalError('concurrent update'))
        with self.assertRaises(OperationalError):
            self.payout.process_bank_statement()
        self.payout._lock_settlement_payouts.assert_called_once()
        self.savepoint.__enter__.assert_called_once()
        self.assertIs(self.savepoint.__exit__.call_args.args[0], OperationalError)

    def test_refund_created_during_processing_is_matched_without_second_click(self):
        self.statement.shopify_transaction_type = 'refund'
        self.statement.payout_line_id = object()
        self.statement.amount = -45
        payment, move_line = SimpleNamespace(move_id=True), object()
        self.payout.find_payment_for_payout_transaction = Mock(side_effect=[False, payment])
        self.payout.get_invoices_for_reconcile = Mock(return_value=Records())
        self.payout.get_payment_move_line_amount = Mock(return_value=(-45, [], [move_line]))
        self.payout.reconcile_invoice_refund = Mock(return_value=[])
        self.payout.write = Mock()
        self.payout.process_bank_statement()
        self.payout.reconcile_invoice_refund.assert_called_once_with(
            self.statement, -45, [], [], [move_line], [])

    def test_invoice_matching_propagates_database_conflict(self):
        self.payout.shopify_reconcile_bank_statement_line_ept = Mock(
            side_effect=OperationalError('concurrent update'))
        with self.assertRaises(OperationalError):
            self.payout.reconcile_invoice_refund(self.statement, 97, [], [{'id': 3}], [], [])
        self.assertIs(self.savepoint.__exit__.call_args.args[0], OperationalError)

    def test_generation_completion_uses_currency_precision(self):
        self.payout.currency_id = SimpleNamespace(
            is_zero=lambda amount: Decimal(str(amount)).quantize(Decimal('.001')) == 0)
        transaction = SimpleNamespace(amount=.004, is_remaining_statement=True)
        self.payout.payout_transaction_ids = Records([transaction])
        self.assertFalse(self.payout.check_process_statement())
        transaction.is_remaining_statement = False
        self.assertTrue(self.payout.check_process_statement())
        transaction.is_remaining_statement = True
        transaction.amount = .0001
        self.assertTrue(self.payout.check_process_statement())
