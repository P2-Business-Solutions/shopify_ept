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


class AccessError(ValueError):
    """Access failures must not become accounting exceptions."""


def translate(message, *args, **kwargs):
    return message % (kwargs or args) if args or kwargs else message


class Records(list):
    def filtered(self, predicate):
        return Records(row for row in self if predicate(row))


source = ast.parse((Path(__file__).resolve().parents[1] / 'models' / 'shopify_payout_report_ept.py').read_text())
model = next(node for node in source.body if isinstance(node, ast.ClassDef))
names = {'process_bank_statement', 'reconcile_invoice_refund', 'check_process_statement',
         '_payout_transaction_exception', '_payout_statement_metadata', 'set_payout_log_line'}
methods = [node for node in model.body if isinstance(node, ast.FunctionDef) and node.name in names]
namespace = {'OperationalError': OperationalError, 'UserError': ValueError, 'AccessError': AccessError,
             '_': translate,
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
        self.env.context = self.payout._context
        self.env.cr.savepoint.return_value = self.savepoint
        self.payout.env = self.env
        self.payout.payout_transaction_ids = Records()
        self.payout.instance_id = SimpleNamespace(id=3, is_shopify_create_schedule=False)
        self.payout.set_payout_log_line = Mock()
        self.payout.message_post = Mock()
        self.payout.write = Mock()
        self.payout.validate_statement = Mock()
        self.statement = SimpleNamespace(id=2, is_reconciled=False, amount=97,
                                         shopify_transaction_type='adjustment', payout_line_id=False)
        self.env.__getitem__.return_value.search.return_value = Records([self.statement])
        self.payout.currency_id = SimpleNamespace(compare_amounts=lambda a, b: a - b)

    def _transaction(self, identifier):
        return SimpleNamespace(id=11, transaction_id=identifier, transaction_type='refund',
                               order_id=SimpleNamespace(name='SO123'), source_order_id='123',
                               shop_cash_kind=False, _historical_collection_entry=Mock(return_value=False),
                               _refund_credit_reference=Mock(return_value=False))

    def test_manual_blocker_retains_other_matches_and_reports_exact_transaction(self):
        self.payout._context.clear()
        transaction = self._transaction('blocked-refund')
        blocked = SimpleNamespace(id=12, is_reconciled=False, amount=-45, payment_ref='SO123',
                                  shopify_transaction_type='refund', payout_line_id=transaction)
        self.env.__getitem__.return_value.search.return_value = Records([self.statement, blocked])
        self.payout.find_payment_for_payout_transaction = Mock(return_value=False)
        self.payout.get_invoices_for_reconcile = Mock(side_effect=ValueError('Existing payments do not match Shopify cash history'))
        def match(statement, _data, logs):
            statement.is_reconciled = True
            return logs
        self.payout.reconcile_other_transactions = Mock(side_effect=match)
        self.assertTrue(self.payout.process_bank_statement())
        self.assertTrue(self.statement.is_reconciled)
        self.assertFalse(blocked.is_reconciled)
        self.payout.write.assert_called_once_with({'state': 'partially_processed'})
        self.assertIn('blocked-refund', self.payout.reimport_reconciliation_issue)
        self.assertIn('Bulk Repair Transactions', self.payout.reimport_reconciliation_issue)
        self.assertIn('Existing payments', self.payout.set_payout_log_line.call_args.args[0][0]['message'])

    def test_manual_permission_and_unexpected_failures_still_propagate(self):
        self.payout._context.clear()
        for error in (AccessError('Forbidden'), RuntimeError('Programming error')):
            self.payout.reconcile_other_transactions = Mock(side_effect=error)
            with self.assertRaises(type(error)):
                self.payout.process_bank_statement()

    def test_manual_validation_blocker_retains_successful_matches(self):
        self.payout._context.clear()
        self.statement.is_reconciled = True
        self.payout.validate_statement.side_effect = ValueError('Review payout totals')
        self.payout.process_bank_statement()
        self.assertTrue(self.statement.is_reconciled)
        self.payout.write.assert_any_call({'state': 'partially_processed'})
        self.assertEqual(self.payout.reimport_reconciliation_issue, 'Review payout totals')

    def test_missing_statement_line_prevents_validation(self):
        self.payout.check_process_statement = Mock(return_value=False)
        self.statement.is_reconciled = True
        self.payout.process_bank_statement()
        self.payout.write.assert_called_once_with({'state': 'partially_generated'})
        self.payout.validate_statement.assert_not_called()

    def test_retry_clears_resolved_exceptions_and_skips_existing_matches(self):
        self.payout._context.clear()
        self.statement.is_reconciled = True
        self.payout.reimport_reconciliation_issue = 'Old exception'
        self.payout.reconcile_other_transactions = Mock()
        self.payout.process_bank_statement()
        self.payout.reconcile_other_transactions.assert_not_called()
        self.assertFalse(self.payout.reimport_reconciliation_issue)

    def test_generation_metadata_blocker_rolls_back_before_returning_unmatched_row(self):
        transaction = self._transaction('blocked-refund')
        self.payout.find_payment_for_payout_transaction = Mock(return_value=False)
        ledger = ['previous successful accounting']
        class Savepoint:
            def __enter__(self):
                self.snapshot = ledger[:]
            def __exit__(self, exception, _error, _traceback):
                if exception:
                    ledger[:] = self.snapshot
        self.env.cr.savepoint.side_effect = Savepoint
        def import_refund(_transaction, logs):
            ledger.append('partial credit')
            logs.append({'message': 'stale message from rolled back import'})
            raise ValueError('Existing payments do not match Shopify cash history')
        self.payout.check_for_invoice_refund = Mock(side_effect=import_refund)
        logs = []
        reference, _invoice, blocked = self.payout._payout_statement_metadata(transaction, logs)
        self.assertEqual(reference, 'SO123')
        self.assertTrue(blocked)
        self.assertEqual(ledger, ['previous successful accounting'])
        self.assertEqual(len(logs), 1)
        self.assertIn('blocked-refund', logs[0]['message'])

    def test_metadata_database_and_permission_failures_still_propagate(self):
        transaction = self._transaction('retry')
        for error in (OperationalError('retry'), AccessError('Forbidden'), RuntimeError('Programming error')):
            self.payout.find_payment_for_payout_transaction = Mock(side_effect=error)
            with self.assertRaises(type(error)):
                self.payout._payout_statement_metadata(transaction, [])

    def test_reviewed_bulk_generation_does_not_import_an_unselected_refund(self):
        self.payout._context['shopify_bulk_repaired_line_ids'] = []
        self.payout.find_payment_for_payout_transaction = Mock(return_value=False)
        self.payout.check_for_invoice_refund = Mock(side_effect=AssertionError('Unreviewed import'))
        self.payout._payout_statement_metadata(self._transaction('unselected'), [])
        self.payout.check_for_invoice_refund.assert_not_called()

    def test_exception_logs_link_to_payout_rather_than_transaction(self):
        self.payout._name = 'shopify.payout.report.ept'
        Processing.set_payout_log_line(self.payout, [{'message': 'Blocked', 'shopify_payout_report_line_id': 99}])
        self.env.__getitem__.return_value.create_common_log_line_ept.assert_called_once_with(
            shopify_instance_id=3, module='shopify_ept', message='Blocked',
            shopify_payout_report_line_id=self.payout.id, model_name=self.payout._name)

    def test_processing_propagates_database_conflict_for_request_retry(self):
        self.payout.reconcile_other_transactions = Mock(side_effect=OperationalError('concurrent update'))
        with self.assertRaises(OperationalError):
            self.payout.process_bank_statement()
        self.payout._lock_settlement_payouts.assert_called_once()
        self.savepoint.__enter__.assert_called_once()
        self.assertIs(self.savepoint.__exit__.call_args.args[0], OperationalError)

    def test_refund_created_during_processing_is_matched_without_second_click(self):
        self.statement.shopify_transaction_type = 'refund'
        self.statement.payout_line_id = SimpleNamespace(_historical_collection_entry=Mock(return_value=False),
                                                        _refund_credit_reference=Mock(return_value=False))
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
