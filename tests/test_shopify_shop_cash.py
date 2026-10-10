"""Shop Cash parsing and real-ledger settlement coverage."""
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE

spec = importlib.util.spec_from_file_location('shop_cash_utils', Path(__file__).resolve().parents[1] / 'models' / 'shopify_shop_cash_utils.py')
utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(utils)

if ODOO_AVAILABLE:
    from odoo import Command, fields
    from odoo.exceptions import UserError
    from .. import shopify


class TestShopCashEvidence(unittest.TestCase):
    def test_only_payment_activity_is_classified(self):
        self.assertEqual(utils.shop_cash_kind('credit', 'shop_cash'), 'shop_cash_credit')
        self.assertEqual(utils.shop_cash_kind('debit', 'shop_cash_refund'), 'shop_cash_refund_debit')
        self.assertEqual(utils.shop_cash_kind('SHOP_CASH_CREDIT', None), 'shop_cash_credit')
        self.assertFalse(utils.shop_cash_kind('credit', 'shop_cash_campaign_billing'))
        self.assertFalse(utils.shop_cash_kind('credit', 'tax_adjustment'))

    def test_rest_adjustment_id_is_not_used_as_payment_id(self):
        result = utils.shop_cash_allocations({'adjustment_order_transactions': [
            {'id': 999, 'order': {'id': 123}, 'amount': '40.00', 'fees': '.90', 'net': '39.10'}]})
        self.assertEqual(result, [{'order_id': '123', 'transaction_id': '', 'amount': '40.00',
                                   'fee': '0.90', 'net': '39.10'}])

    def test_duplicate_or_unidentified_allocations_are_rejected(self):
        row = {'order_transaction_id': 123, 'amount': '40.00'}
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            utils.shop_cash_allocations({'adjustment_order_transactions': [row, row]})
        with self.assertRaisesRegex(ValueError, 'lacks'):
            utils.shop_cash_allocations({'adjustment_order_transactions': [{'id': 123, 'amount': '40'}]})

    def test_nonfinite_zero_and_invalid_amounts_are_rejected(self):
        for amount in ('NaN', 'Infinity', 'oops', '0'):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                utils.shop_cash_allocations({'adjustment_order_transactions': [
                    {'order_transaction_id': 123, 'amount': amount}]})


class TestShopCashSettlement(PayoutTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.outstanding = cls.env['account.account'].create({
            'name': 'Shop Cash Outstanding', 'code': 'SCOUT', 'account_type': 'asset_current',
            'reconcile': True, 'company_ids': [Command.set(cls.env.company.ids)],
        })
        cls.receivable = cls.env['account.account'].create({
            'name': 'Shop Cash Receivable', 'code': 'SCREC', 'account_type': 'asset_receivable',
            'reconcile': True, 'company_ids': [Command.set(cls.env.company.ids)],
        })
        cls.revenue = cls.env['account.account'].create({
            'name': 'Shop Cash Test Sales', 'code': 'SCINC', 'account_type': 'income',
            'company_ids': [Command.set(cls.env.company.ids)],
        })
        cls.sales_journal = cls.env['account.journal'].create({'name': 'Shop Cash Sales', 'type': 'sale', 'code': 'SCS'})
        cls.product = cls.env['product.product'].create({'name': 'Shop Cash Test Item', 'type': 'service'})
        cls.journal.autocheck_on_post = True
        (cls.journal.inbound_payment_method_line_ids | cls.journal.outbound_payment_method_line_ids).write({
            'payment_account_id': cls.outstanding.id,
        })
        # A generic credit mapping must not automatically book Shop Cash.
        cls.instance.transaction_line_ids = [Command.create({
            'transaction_type': 'credit', 'account_id': cls.revenue.id,
        })]

    def _order(self, reference, amount=40):
        partner = self.env['res.partner'].create({'name': reference, 'property_account_receivable_id': self.receivable.id})
        order = self.env['sale.order'].create({'partner_id': partner.id, 'shopify_instance_id': self.instance.id,
                                              'shopify_order_id': reference})
        line = self.env['sale.order.line'].create({'order_id': order.id, 'product_id': self.product.id,
            'price_unit': amount, 'product_uom_qty': 1, 'tax_id': [Command.clear()]})
        invoice = self.env['account.move'].create({'move_type': 'out_invoice', 'journal_id': self.sales_journal.id,
            'partner_id': partner.id, 'invoice_date': fields.Date.today(), 'invoice_line_ids': [Command.create({
                'product_id': self.product.id, 'quantity': 1, 'price_unit': amount, 'account_id': self.revenue.id,
                'tax_ids': [Command.clear()], 'sale_line_ids': [Command.link(line.id)],
            })]})
        invoice.action_post()
        return order, invoice

    def _payment(self, order, amount, transaction_id, kind='capture', gateway='shop_cash', invoice=None):
        direction = 'outbound' if kind == 'refund' else 'inbound'
        methods = self.journal.outbound_payment_method_line_ids if direction == 'outbound' else self.journal.inbound_payment_method_line_ids
        payment = self.env['account.payment'].create({'payment_type': direction, 'partner_type': 'customer',
            'partner_id': order.partner_id.id, 'journal_id': self.journal.id, 'payment_method_line_id': methods[:1].id,
            'amount': amount, 'currency_id': self.env.company.currency_id.id,
            'shopify_cash_order_id': order.id, 'shopify_instance_id': self.instance.id,
            'shopify_order_transaction_id': transaction_id, 'shopify_cash_gateway': gateway, 'shopify_cash_kind': kind})
        payment.action_post()
        if invoice:
            (payment._seek_for_lines()[1] | invoice.line_ids.filtered(lambda line: line.account_type == 'asset_receivable')).reconcile()
        return payment

    def _cash_payout(self, reference, entries, amount=None, fee=0, reason='shop_cash', source_id=None):
        gross = sum(float(item['amount']) for item in entries) if amount is None else amount
        payout = self.env['shopify.payout.report.ept'].create({'instance_id': self.instance.id,
            'payout_reference_id': reference, 'payout_date': fields.Date.today(),
            'payout_status': 'paid', 'currency_id': self.env.company.currency_id.id, 'amount': gross - fee})
        values = payout.prepare_transaction_vals({'id': reference + '-balance', 'type': 'credit' if gross > 0 else 'debit',
            'currency': self.env.company.currency_id.name, 'amount': gross, 'fee': fee, 'net': gross - fee,
            'adjustment_reason': reason, 'adjustment_order_transactions': entries,
            'source_order_id': source_id}, self.instance)
        payout.payout_transaction_ids = [Command.create(values), Command.create({
            'transaction_type': 'fees', 'amount': -fee, 'net_amount': -fee,
            'currency_id': payout.currency_id.id, 'is_remaining_statement': True})]
        return payout

    def _ledger_reconcile(self, payout, statement_id, line_ids):
        """Use actual Odoo journal items when Enterprise's widget is unavailable."""
        statement = self.env['account.bank.statement.line'].browse(statement_id)
        lines = self.env['account.move.line'].browse(line_ids)
        self.assertEqual(len(lines.account_id), 1)
        self._book_counterpart(statement, lines.account_id)
        counterparts = statement.line_ids.filtered(lambda line: line.account_id == lines.account_id)
        (lines | counterparts).reconcile()

    def _book_counterpart(self, statement, account):
        _liquidity, suspense, _other = statement._seek_for_lines()
        suspense.write({'account_id': account.id})

    def _process(self, payout, operation=None):
        operation = operation or payout.process_bank_statement
        if 'bank.rec.widget' in self.env.registry.models:
            return operation()
        else:
            with patch.object(type(payout), 'shopify_reconcile_bank_statement_line_ept',
                              lambda model, statement_id, line_ids: self._ledger_reconcile(model, statement_id, line_ids)), \
                    patch.object(type(payout), 'shopify_reconcile_other_bank_statement_line_ept',
                                 lambda model, statement, _line: self._book_counterpart(statement,
                                     model.instance_id.transaction_line_ids.filtered(
                                         lambda row: row.transaction_type == statement.shopify_transaction_type).account_id)):
                return operation()

    def test_reimport_repairs_generic_credit_preserves_card_and_is_repeatable(self):
        order, invoice = self._order('reimport-mixed', 265.91)
        card = self._payment(order, 225.91, 'reimport-card', gateway='shopify_payments', invoice=invoice)
        cash = self._payment(order, 40, 'reimport-cash', invoice=invoice)
        payout = self._cash_payout('140763300066', [{'order_transaction_id': 'reimport-cash', 'amount': '40'}])
        payout.write({'amount': 265.91, 'payout_transaction_ids': [Command.create({
            'transaction_id': 'reimport-card-balance', 'transaction_type': 'charge',
            'source_order_transaction_id': 'reimport-card', 'source_order_id': order.shopify_order_id,
            'order_id': order.id, 'amount': 225.91, 'net_amount': 225.91,
            'currency_id': payout.currency_id.id, 'is_remaining_statement': True})]})
        payout.generate_bank_statement()
        statements = self._statement_lines(payout)
        cash_statement = statements.filtered(lambda row: row.payout_line_id.shop_cash_kind)
        card_statement = statements - cash_statement
        self._ledger_reconcile(payout, card_statement.id, card._seek_for_lines()[0].ids)
        self._book_counterpart(cash_statement, self.revenue)
        payout.state = 'partially_processed'
        card_items = card_statement.line_ids
        moves = statements.move_id
        report = SimpleNamespace(to_dict=lambda: {'id': 140763300066, 'status': 'paid'})
        cash_data = {'id': '140763300066-balance', 'type': 'credit', 'adjustment_reason': 'shop_cash',
            'currency': payout.currency_id.name, 'amount': 40, 'fee': 0, 'net': 40,
            'adjustment_order_transactions': [{'order_transaction_id': 'reimport-cash', 'amount': '40'}]}
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(shopify.Payouts, 'find', return_value=report), \
                patch.object(shopify.Transactions, 'find', return_value=[SimpleNamespace(to_dict=lambda: cash_data)]):
            imported = self._process(payout, lambda: self.env[payout._name].get_payout_report_by_ids('140763300066', self.instance))
        self.assertEqual(imported, payout)
        self.assertEqual(payout.state, 'validated')
        self.assertFalse(payout.reimport_reconciliation_issue)
        self.assertEqual(self._statement_lines(payout), statements)
        self.assertEqual(statements.move_id, moves)
        self.assertEqual(card_statement.line_ids, card_items)
        self.assertEqual(invoice.payment_state, 'paid')
        self.assertTrue(all(payment._seek_for_lines()[0].reconciled for payment in card | cash))
        original_items = statements.line_ids
        self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(statements.line_ids, original_items)

    def test_reimport_repairs_grouped_credits_and_refunds(self):
        captures, refunds = self.env['account.payment'], self.env['account.payment']
        credit_entries, refund_entries = [], []
        for reference, amount, refund_amount in (('reimport-first', 40, 10), ('reimport-second', 25, 5)):
            order, invoice = self._order(reference, amount)
            captures |= self._payment(order, amount, reference + '-cash', invoice=invoice)
            refunds |= self._payment(order, refund_amount, reference + '-refund', kind='refund')
            credit_entries.append({'order': {'id': reference}, 'amount': str(amount)})
            refund_entries.append({'order_transaction_id': reference + '-refund', 'amount': str(refund_amount)})
        credits = self._cash_payout('reimport-group-credit', credit_entries, fee=1.30)
        debits = self._cash_payout('reimport-group-refund', refund_entries, amount=-15, reason='shop_cash_refund')
        for payout in credits | debits:
            payout.generate_bank_statement()
            self._book_counterpart(self._statement_lines(payout).filtered(lambda row: row.payout_line_id.shop_cash_kind), self.revenue)
            payout.state = 'validated'  # Historical generic postings passed the old validation.
            self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
            self.assertEqual(payout.state, 'validated')
            self.assertFalse(payout.reimport_reconciliation_issue)
        self.assertTrue(all(payment._seek_for_lines()[0].reconciled for payment in captures | refunds))
        self.assertEqual(credits.payout_transaction_ids.filtered('shop_cash_kind').shop_cash_payment_ids, captures)
        self.assertEqual(debits.payout_transaction_ids.filtered('shop_cash_kind').shop_cash_payment_ids, refunds)

    def test_failed_rematch_restores_generic_posting_and_does_not_steal_payment(self):
        order, invoice = self._order('consumed-reimport')
        payment = self._payment(order, 40, 'consumed-reimport-cash', invoice=invoice)
        original = self._cash_payout('original-payment-owner', [{'order_transaction_id': 'consumed-reimport-cash', 'amount': '40'}])
        original.generate_bank_statement()
        self._process(original)
        payout = self._cash_payout('duplicate-payment-owner', [{'order_transaction_id': 'consumed-reimport-cash', 'amount': '40'}])
        payout.generate_bank_statement()
        statement = self._statement_lines(payout)
        self._book_counterpart(statement, self.revenue)
        payout.state = 'validated'
        original_items = statement.line_ids
        partials = payment._seek_for_lines()[0].matched_credit_ids
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(statement.line_ids, original_items)
        self.assertTrue(statement.is_reconciled)
        self.assertEqual(statement._seek_for_lines()[2].account_id, self.revenue)
        self.assertEqual(payment._seek_for_lines()[0].matched_credit_ids, partials)
        self.assertEqual(payout.state, 'partially_processed')
        self.assertTrue(payout.reimport_reconciliation_issue)

    def test_reimport_preserves_linked_manual_match_for_review(self):
        order, invoice = self._order('manual-cash')
        cash = self._payment(order, 40, 'manual-cash', invoice=invoice)
        other_order, _other_invoice = self._order('manual-card')
        wrong_payment = self._payment(other_order, 40, 'manual-card', gateway='shopify_payments')
        payout = self._cash_payout('manual-credit', [{'order_transaction_id': 'manual-cash', 'amount': '40'}])
        payout.generate_bank_statement()
        statement = self._statement_lines(payout)
        self._ledger_reconcile(payout, statement.id, wrong_payment._seek_for_lines()[0].ids)
        payout.state = 'validated'
        items = statement.line_ids
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(statement.line_ids, items)
        self.assertTrue(wrong_payment._seek_for_lines()[0].reconciled)
        self.assertFalse(cash._seek_for_lines()[0].reconciled)
        self.assertIn('linked accounting', payout.reimport_reconciliation_issue)

    def _legacy_locked_payout(self, lock_field):
        order, invoice = self._order('locked-' + lock_field)
        cash = self._payment(order, 40, 'locked-' + lock_field, invoice=invoice)
        payout = self._cash_payout('locked-payout-' + lock_field,
            [{'order_transaction_id': cash.shopify_order_transaction_id, 'amount': '40'}])
        payout.generate_bank_statement()
        self._book_counterpart(self._statement_lines(payout), self.revenue)
        payout.state = 'validated'
        self.env.company[lock_field] = fields.Date.today()
        return payout, cash

    def test_global_lock_prevents_reset_even_with_user_exception(self):
        payout, cash = self._legacy_locked_payout('fiscalyear_lock_date')
        # Even a user allowed to post through an exception must not reprocess a closed payout.
        self.env['account.lock_exception'].create({'company_id': self.env.company.id,
            'user_id': self.env.user.id, 'lock_date_field': 'fiscalyear_lock_date',
            'lock_date': fields.Date.today() - timedelta(days=1), 'reason': 'Test temporary exception'})
        self.assertLess(self.env.company._get_user_fiscal_lock_date(self.journal), payout.payout_date)
        items = self._statement_lines(payout).line_ids
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(self._statement_lines(payout).line_ids, items)
        self.assertEqual(payout.state, 'validated')
        self.assertFalse(cash._seek_for_lines()[0].reconciled)
        self.assertIn('closed accounting period', payout.reimport_reconciliation_issue)

    def test_hard_lock_prevents_reset(self):
        payout, cash = self._legacy_locked_payout('hard_lock_date')
        items = self._statement_lines(payout).line_ids
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(self._statement_lines(payout).line_ids, items)
        self.assertEqual(payout.state, 'validated')
        self.assertFalse(cash._seek_for_lines()[0].reconciled)

    def test_locked_original_statement_blocks_unlocked_payout_date(self):
        payout, cash = self._legacy_locked_payout('fiscalyear_lock_date')
        payout.payout_date = fields.Date.today() + timedelta(days=1)
        items = self._statement_lines(payout).line_ids
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(self._statement_lines(payout).line_ids, items)
        self.assertFalse(cash._seek_for_lines()[0].reconciled)
        self.assertIn('Journal entry', payout.reimport_reconciliation_issue)

    def test_reimport_retries_open_payments_without_recreating_statements(self):
        order, invoice = self._order('retry-open')
        payout = self._cash_payout('retry-open-credit', [{'order_transaction_id': 'retry-open-cash', 'amount': '40'}])
        payout.generate_bank_statement()
        payout.state = 'partially_processed'
        statements = self._statement_lines(payout)
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        cash = self._payment(order, 40, 'retry-open-cash', invoice=invoice)
        self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(payout.state, 'validated')
        self.assertFalse(payout.reimport_reconciliation_issue)
        self.assertEqual(self._statement_lines(payout), statements)
        self.assertTrue(cash._seek_for_lines()[0].reconciled)

    def test_successful_repair_survives_an_unbalanced_payout_validation(self):
        order, invoice = self._order('unbalanced-reimport')
        cash = self._payment(order, 40, 'unbalanced-cash', invoice=invoice)
        payout = self._cash_payout('unbalanced-credit', [{'order_transaction_id': 'unbalanced-cash', 'amount': '40'}])
        payout.amount = 41
        payout.generate_bank_statement()
        self._book_counterpart(self._statement_lines(payout), self.revenue)
        payout.state = 'validated'
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(payout.state, 'partially_processed')
        self.assertTrue(cash._seek_for_lines()[0].reconciled)
        payout._check_shop_cash_reconciliation(payout.payout_transaction_ids.filtered('shop_cash_kind'), self._statement_lines(payout))
        self.assertIn('does not balance', payout.reimport_reconciliation_issue)

    def _legacy_payout_with_bank_matched_transfer(self, settlement_date=None):
        transit = self.env['account.account'].create({'name': 'Reimport Transit', 'code': 'SCRTRANS',
            'account_type': 'asset_current', 'reconcile': True, 'company_ids': [Command.set(self.env.company.ids)]})
        bank_account = self.journal.default_account_id.copy({'code': 'SCRBANK'})
        bank = self.journal.copy({'name': 'Reimport Receiving Bank', 'code': 'SCRB', 'default_account_id': bank_account.id})
        bank.inbound_payment_method_line_ids.payment_account_id = transit
        transfer_journal = self.env['account.journal'].create({'name': 'Reimport Transfers', 'type': 'general', 'code': 'SCRT'})
        self.instance.write({'shopify_payout_bank_journal_id': bank.id, 'shopify_payout_transit_account_id': transit.id,
            'shopify_payout_transfer_journal_id': transfer_journal.id})
        order, invoice = self._order('bank-matched-reimport')
        cash = self._payment(order, 40, 'bank-matched-cash', invoice=invoice)
        payout = self._cash_payout('bank-matched-credit', [{'order_transaction_id': 'bank-matched-cash', 'amount': '40'}])
        payout.generate_bank_statement()
        self._process(payout)
        if settlement_date:
            payout.payout_date = settlement_date
        payout.action_create_settlement_transfer()
        payout.payout_date = fields.Date.today()
        bank_line = self.env['account.bank.statement.line'].create({'journal_id': bank.id,
            'date': fields.Date.today(), 'payment_ref': 'Reimport Bank Deposit', 'amount': payout.amount,
            'counterpart_account_id': transit.id})
        (payout.settlement_line_id | bank_line.line_ids.filtered(lambda line: line.account_id == transit)).reconcile()
        statement = self._statement_lines(payout)
        statement.action_undo_reconciliation()
        self._book_counterpart(statement, self.revenue)
        return payout, cash, bank_line

    def test_repair_preserves_existing_transfer_and_bank_match(self):
        payout, cash, bank_line = self._legacy_payout_with_bank_matched_transfer()
        move, items = payout.settlement_move_id, payout.settlement_move_id.line_ids
        partials = payout.settlement_line_id.matched_credit_ids
        self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(payout.settlement_move_id, move)
        self.assertEqual(move.line_ids, items)
        self.assertEqual(payout.settlement_line_id.matched_credit_ids, partials)
        self.assertEqual(payout.settlement_status, 'matched')
        self.assertEqual(payout.settlement_bank_line_ids, bank_line)
        self.assertTrue(cash._seek_for_lines()[0].reconciled)
        self.assertEqual(self.env['account.move'].search_count([('shopify_settlement_payout_id', '=', payout.id)]), 1)

    def test_locked_settlement_entry_blocks_open_statement_repair(self):
        yesterday = fields.Date.today() - timedelta(days=1)
        payout, cash, _bank_line = self._legacy_payout_with_bank_matched_transfer(settlement_date=yesterday)
        self.env.company.fiscalyear_lock_date = yesterday
        items = self._statement_lines(payout).line_ids
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(self._statement_lines(payout).line_ids, items)
        self.assertEqual(payout.settlement_move_id.date, yesterday)
        self.assertFalse(cash._seek_for_lines()[0].reconciled)
        self.assertIn('Journal entry', payout.reimport_reconciliation_issue)

    def test_card_and_shop_cash_clear_invoice_and_validate_payout(self):
        order, invoice = self._order('mixed', 265.91)
        card = self._payment(order, 225.91, 'card', gateway='shopify_payments', invoice=invoice)
        cash = self._payment(order, 40, 'cash', invoice=invoice)
        self.assertEqual(invoice.amount_residual, 0)
        self.assertFalse(any(payment._seek_for_lines()[0].reconciled for payment in card | cash))
        payout = self._cash_payout('mixed-payout', [{'order_transaction_id': 'cash', 'amount': '40'}], fee=.90)
        payout.write({'amount': 237.67, 'payout_transaction_ids': [Command.create({
            'transaction_id': 'card-balance', 'transaction_type': 'charge', 'source_order_id': 'mixed',
            'source_order_transaction_id': 'card', 'order_id': order.id, 'amount': 225.91,
            'fee': 5.38, 'net_amount': 220.53, 'currency_id': payout.currency_id.id,
            'is_remaining_statement': True}), Command.create({
                'transaction_id': 'tax-balance', 'transaction_type': 'tax_adjustment', 'amount': -21.96,
                'currency_id': payout.currency_id.id, 'is_remaining_statement': True})]})
        payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'fees').write({'amount': -6.28})
        self.instance.transaction_line_ids = [Command.create({'transaction_type': 'tax_adjustment',
            'account_id': self.revenue.id})]
        payout.generate_bank_statement()
        cash_statement = self._statement_lines(payout).filtered(lambda row: row.payout_line_id.shop_cash_kind)
        self.assertFalse(cash_statement.is_reconciled)
        self._process(payout)
        self.assertEqual(payout.state, 'validated')
        self.assertEqual(invoice.payment_state, 'paid')
        self.assertTrue(all(payment._seek_for_lines()[0].reconciled for payment in card | cash))
        self.assertEqual(cash_statement.payout_line_id.shop_cash_payment_ids, cash)
        original_moves = self._statement_lines(payout).move_id
        self._process(payout)
        payout.generate_bank_statement()
        self.assertEqual(self._statement_lines(payout).move_id, original_moves)

    def _legacy_adjustment_fee_payout(self, *, cash_fee=1.80, imported_cash_fee=None):
        entries, cash_payments = [], self.env['account.payment']
        for reference in ('fee-first', 'fee-second'):
            order, invoice = self._order(reference, 40)
            cash_payments |= self._payment(order, 40, reference + '-cash', invoice=invoice)
            entries.append({'order_transaction_id': reference + '-cash', 'amount': '40',
                            'fees': str(cash_fee / 2), 'net': str(40 - cash_fee / 2)})
        payout = self._cash_payout('140819824866', entries, fee=cash_fee)
        cash_line = payout.payout_transaction_ids.filtered('shop_cash_kind')
        if imported_cash_fee is not None:
            cash_line.write({'fee': imported_cash_fee, 'net_amount': 80 - imported_cash_fee})
        order, invoice = self._order('fee-card', 100)
        card = self._payment(order, 100, 'fee-card', gateway='shopify_payments', invoice=invoice)
        payout.write({'amount': 180 - cash_fee - 3, 'payout_transaction_ids': [Command.create({
            'transaction_id': 'fee-card-balance', 'transaction_type': 'charge',
            'source_order_transaction_id': 'fee-card', 'source_order_id': order.shopify_order_id,
            'order_id': order.id, 'amount': 100, 'fee': 3, 'net_amount': 97,
            'currency_id': payout.currency_id.id, 'is_remaining_statement': True})]})
        # Older imports omitted adjustment fees and also stored a positive net
        # on this synthetic negative fee deduction.
        fees = payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'fees')
        fees.write({'amount': -3, 'net_amount': 3})
        payout.generate_bank_statement()
        statements = self._statement_lines(payout)
        self._ledger_reconcile(payout, statements.filtered(lambda row: row.payout_line_id == cash_line).id,
            cash_payments.move_id.line_ids.filtered(lambda row: row.account_id == self.outstanding).ids)
        self._ledger_reconcile(payout, statements.filtered(lambda row: row.shopify_transaction_type == 'charge').id,
            card._seek_for_lines()[0].ids)
        fee_statement = statements.filtered(lambda row: row.shopify_transaction_type == 'fees')
        self._book_counterpart(fee_statement, self.instance.transaction_line_ids.filtered(
            lambda row: row.transaction_type == 'fees').account_id)
        payout.state = 'partially_processed'
        return payout, fee_statement, cash_payments | card

    def test_reimport_refreshes_adjustment_fee_and_preserves_grouped_payment_matches(self):
        payout, fee_statement, payments = self._legacy_adjustment_fee_payout(imported_cash_fee=0)
        statements = self._statement_lines(payout)
        gross_items = (statements - fee_statement).line_ids
        partials = gross_items.matched_debit_ids | gross_items.matched_credit_ids
        transactions = [{
            'id': '140819824866-balance', 'type': 'credit', 'adjustment_reason': 'shop_cash',
            'currency': payout.currency_id.name, 'amount': '80', 'fee': '1.80', 'net': '78.20',
            'adjustment_order_transactions': [
                {'order_transaction_id': ref + '-cash', 'amount': '40', 'fees': '.90', 'net': '39.10'}
                for ref in ('fee-first', 'fee-second')]}, {
            'id': 'fee-card-balance', 'type': 'charge', 'source_order_id': 'fee-card',
            'source_order_transaction_id': 'fee-card', 'currency': payout.currency_id.name,
            'amount': '100', 'fee': '3', 'net': '97'}]
        with patch.object(shopify.Transactions, 'find', return_value=[
                SimpleNamespace(to_dict=lambda data=data: data) for data in transactions]):
            payout.refresh_payout_transaction_links()
        self.assertEqual(payout.payout_transaction_ids.filtered('shop_cash_kind').fee, 1.80)
        self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(payout.state, 'validated')
        self.assertAlmostEqual(fee_statement.amount, -4.80)
        self.assertAlmostEqual(fee_statement.payout_line_id.net_amount, -4.80)
        self.assertEqual(self._statement_lines(payout), statements)
        self.assertEqual((statements - fee_statement).line_ids, gross_items)
        self.assertEqual(gross_items.matched_debit_ids | gross_items.matched_credit_ids, partials)
        self.assertTrue(all(payments.move_id.line_ids.filtered(
            lambda row: row.account_id == self.outstanding).mapped('reconciled')))
        self.assertFalse(payout.reimport_reconciliation_issue)
        items = statements.line_ids
        self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(statements.line_ids, items)

    def test_adjustment_fee_correction_respects_closed_period(self):
        payout, fees, _payments = self._legacy_adjustment_fee_payout()
        items = self._statement_lines(payout).line_ids
        self.env.company.fiscalyear_lock_date = fields.Date.today()
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(fees.amount, -3)
        self.assertEqual(fees.payout_line_id.amount, -3)
        self.assertEqual(self._statement_lines(payout).line_ids, items)
        self.assertIn('closed accounting period', payout.reimport_reconciliation_issue)

    def test_validated_fee_correction_preserves_settlement_and_bank_match(self):
        payout, fees, _payments = self._legacy_adjustment_fee_payout()
        self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
        transit = self.outstanding.copy({'name': 'Fee Repair Transit', 'code': 'SCFTRANS'})
        bank = self.journal.copy({'name': 'Fee Repair Receiving Bank', 'code': 'SCFB',
            'default_account_id': self.journal.default_account_id.copy({'code': 'SCFBANK'}).id})
        bank.inbound_payment_method_line_ids.payment_account_id = transit
        self.instance.write({'shopify_payout_bank_journal_id': bank.id,
            'shopify_payout_transit_account_id': transit.id,
            'shopify_payout_transfer_journal_id': self.env['account.journal'].create({
                'name': 'Fee Repair Transfers', 'type': 'general', 'code': 'SCFT'}).id})
        payout.action_create_settlement_transfer()
        bank_line = self.env['account.bank.statement.line'].create({'journal_id': bank.id,
            'date': fields.Date.today(), 'payment_ref': 'Fee Repair Bank Deposit', 'amount': payout.amount,
            'counterpart_account_id': transit.id})
        (payout.settlement_line_id | bank_line.line_ids.filtered(lambda row: row.account_id == transit)).reconcile()
        transfer, items = payout.settlement_move_id, payout.settlement_move_id.line_ids
        partials = payout.settlement_line_id.matched_credit_ids
        # Recreate the historical stale deduction without changing gross
        # payment matches or the already correct net settlement amount.
        fees.action_undo_reconciliation()
        fees.write({'amount': -3})
        self._book_counterpart(fees, self.instance.transaction_line_ids.filtered(
            lambda row: row.transaction_type == 'fees').account_id)
        fees.payout_line_id._set_reimported_fee_total(-3)
        self.assertEqual(payout.state, 'validated')
        self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(payout.state, 'validated')
        self.assertAlmostEqual(fees.amount, -4.80)
        self.assertEqual(payout.settlement_move_id, transfer)
        self.assertEqual(transfer.line_ids, items)
        self.assertEqual(payout.settlement_line_id.matched_credit_ids, partials)
        self.assertEqual(payout.settlement_bank_line_ids, bank_line)
        self.assertEqual(payout.settlement_status, 'matched')

    def test_fee_correction_preserves_linked_manual_accounting_for_review(self):
        payout, fees, _payments = self._legacy_adjustment_fee_payout()
        fees.action_undo_reconciliation()
        self._book_counterpart(fees, self.outstanding)
        offset = self.env['account.move'].create({'journal_id': self.sales_journal.id,
            'line_ids': [Command.create({'account_id': self.outstanding.id, 'credit': 3}),
                         Command.create({'account_id': self.revenue.id, 'debit': 3})]})
        offset.action_post()
        (fees.line_ids | offset.line_ids).filtered(lambda row: row.account_id == self.outstanding).reconcile()
        items = fees.line_ids
        partials = items.matched_debit_ids | items.matched_credit_ids
        self.assertFalse(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(fees.amount, -3)
        self.assertEqual(fees.payout_line_id.amount, -3)
        self.assertEqual(fees.line_ids, items)
        self.assertEqual(items.matched_debit_ids | items.matched_credit_ids, partials)
        self.assertIn('linked accounting', payout.reimport_reconciliation_issue)

    def test_failed_fee_rebooking_restores_original_fee_posting(self):
        payout, fees, _payments = self._legacy_adjustment_fee_payout()
        items = fees.line_ids
        # Stop after the reset and amount update, proving the savepoint covers
        # both the accounting and synthetic source row.
        with patch.object(type(payout), 'shopify_reconcile_other_bank_statement_line_ept',
                          side_effect=UserError('Fee booking failed')):
            self.assertFalse(payout._reprocess_imported_bank_statement())
        self.assertEqual(fees.amount, -3)
        self.assertEqual(fees.payout_line_id.amount, -3)
        self.assertEqual(fees.line_ids, items)
        self.assertTrue(fees.is_reconciled)
        self.assertIn('Fee booking failed', payout.reimport_reconciliation_issue)

    def test_grouped_refund_fee_credit_is_booked_once(self):
        entries = []
        for ref in ('fee-refund-first', 'fee-refund-second'):
            order, _invoice = self._order(ref, 10)
            self._payment(order, 10, ref + '-refund', kind='refund')
            entries.append({'order_transaction_id': ref + '-refund', 'amount': '10',
                            'fees': '-.30', 'net': '-9.70'})
        payout = self._cash_payout('fee-refunds', entries, amount=-20, fee=-.60, reason='shop_cash_refund')
        fees = payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'fees')
        fees.write({'amount': 0, 'net_amount': 0})
        payout.generate_bank_statement()
        self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(payout.state, 'validated')
        self.assertAlmostEqual(fees.amount, .60)
        self.assertAlmostEqual(sum(self._statement_lines(payout).mapped('amount')), -19.40)
        statements = self._statement_lines(payout)
        self.assertTrue(self._process(payout, payout._reprocess_imported_bank_statement))
        self.assertEqual(self._statement_lines(payout), statements)

    def test_grouped_credit_then_grouped_partial_refunds_are_independent(self):
        payments = self.env['account.payment']
        refund_payments = self.env['account.payment']
        credit_entries, refund_entries = [], []
        for reference, amount, refund_amount in (('first', 40, 10), ('second', 25, 5)):
            order, invoice = self._order(reference, amount)
            payments |= self._payment(order, amount, reference + '-cash', invoice=invoice)
            refund_payments |= self._payment(order, refund_amount, reference + '-refund', kind='refund')
            credit_entries.append({'order': {'id': reference}, 'amount': str(amount)})
            refund_entries.append({'order_transaction_id': reference + '-refund', 'amount': str(refund_amount)})
        credits = self._cash_payout('group-credit', credit_entries, fee=1.30)
        refunds = self._cash_payout('group-refund', refund_entries, amount=-15, reason='shop_cash_refund')
        for payout in credits | refunds:
            payout.generate_bank_statement()
            self._process(payout)
            self.assertEqual(payout.state, 'validated')
        self.assertTrue(all(payment._seek_for_lines()[0].reconciled for payment in payments | refund_payments))
        self.assertEqual(credits.payout_transaction_ids.filtered('shop_cash_kind').shop_cash_payment_ids, payments)
        self.assertEqual(refunds.payout_transaction_ids.filtered('shop_cash_kind').shop_cash_payment_ids, refund_payments)
        self._process(refunds)
        self.assertEqual(len(self._statement_lines(credits | refunds)), 3)

    def test_incomplete_group_does_not_consume_any_payment(self):
        order, invoice = self._order('present')
        payment = self._payment(order, 40, 'present-cash', invoice=invoice)
        payout = self._cash_payout('incomplete', [{'order': {'id': 'present'}, 'amount': '40'},
            {'order': {'id': 'missing'}, 'amount': '25'}])
        payout.generate_bank_statement()
        with self.assertRaisesRegex(UserError, 'missing Shop Cash order'):
            self._process(payout)
        self.assertFalse(payment._seek_for_lines()[0].reconciled)
        self.assertFalse(self._statement_lines(payout).is_reconciled)
        self.assertNotEqual(payout.state, 'validated')

    def test_generic_account_posting_cannot_validate_shop_cash(self):
        order, invoice = self._order('generic')
        payment = self._payment(order, 40, 'generic-cash', invoice=invoice)
        payout = self._cash_payout('generic-credit', [{'order_transaction_id': 'generic-cash', 'amount': '40'}])
        payout.generate_bank_statement()
        self._book_counterpart(self._statement_lines(payout), self.revenue)
        with self.assertRaisesRegex(UserError, 'actual capture/refund'):
            payout.validate_statement()
        self.assertFalse(payment._seek_for_lines()[0].reconciled)

    def test_wrong_gateway_duplicate_identity_and_amount_mismatch_block_matching(self):
        order, invoice = self._order('wrong', 40)
        payment = self._payment(order, 40, 'wrong-cash', gateway='shopify_payments', invoice=invoice)
        payout = self._cash_payout('wrong-credit', [{'order_transaction_id': 'wrong-cash', 'amount': '40'}])
        with self.assertRaisesRegex(UserError, 'inconsistent gateway'):
            payout._resolve_shop_cash_payments(payout.payout_transaction_ids.filtered('shop_cash_kind'))
        payment.shopify_cash_gateway = 'shop_cash'
        transaction = payout.payout_transaction_ids.filtered('shop_cash_kind')
        transaction.shop_cash_allocations = [{'transaction_id': 'wrong-cash', 'amount': '39'}]
        with self.assertRaisesRegex(UserError, 'gross amount'):
            payout._resolve_shop_cash_payments(transaction)
        transaction.shop_cash_allocations = [{'transaction_id': 'wrong-cash', 'amount': '20'}] * 2
        with self.assertRaisesRegex(UserError, 'duplicate'):
            payout._resolve_shop_cash_payments(transaction)

    def test_ambiguous_same_order_payments_are_not_guessed(self):
        order, invoice = self._order('ambiguous', 80)
        self._payment(order, 40, 'cash-one', invoice=invoice)
        self._payment(order, 40, 'cash-two', invoice=invoice)
        payout = self._cash_payout('ambiguous-credit', [{'order': {'id': 'ambiguous'}, 'amount': '40'}])
        with self.assertRaisesRegex(UserError, 'ambiguous'):
            payout._resolve_shop_cash_payments(payout.payout_transaction_ids.filtered('shop_cash_kind'))

    def test_already_settled_payment_cannot_be_consumed_by_another_payout(self):
        order, invoice = self._order('repeat')
        self._payment(order, 40, 'repeat-cash', invoice=invoice)
        first = self._cash_payout('repeat-first', [{'order_transaction_id': 'repeat-cash', 'amount': '40'}])
        first.generate_bank_statement()
        self._process(first)
        second = self._cash_payout('repeat-second', [{'order_transaction_id': 'repeat-cash', 'amount': '40'}])
        second.generate_bank_statement()
        with self.assertRaisesRegex(UserError, 'already settled'):
            self._process(second)
        self.assertNotEqual(second.state, 'validated')

    def test_graphql_fallback_verifies_exact_balance_and_retains_transaction_ids(self):
        payout = self._cash_payout('123', [])
        data = {'id': '456', 'type': 'credit', 'adjustment_reason': 'shop_cash',
                'currency': self.env.company.currency_id.name, 'amount': '40', 'fee': '.90', 'net': '39.10'}
        currency = payout.currency_id.name
        node = {'id': 'gid://shopify/ShopifyPaymentsBalanceTransaction/456',
            'associatedPayout': {'id': 'gid://shopify/ShopifyPaymentsPayout/123'},
            'amount': {'amount': '40', 'currencyCode': currency}, 'fee': {'amount': '.90', 'currencyCode': currency},
            'net': {'amount': '39.10', 'currencyCode': currency}, 'adjustmentsOrders': [{
                'orderTransactionId': 'cash-id', 'amount': {'amount': '40', 'currencyCode': currency},
                'fees': {'amount': '.90', 'currencyCode': currency}, 'net': {'amount': '39.10', 'currencyCode': currency}}]}
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(shopify, 'GraphQL') as graphql:
            execute = graphql.return_value.execute
            graphql.return_value.headers = {'User-Agent': 'test'}
            execute.return_value = json.dumps({'data': {'node': node}})
            result = payout._enrich_shop_cash_transaction(data)
            self.assertFalse(result['shop_cash_detail_error'])
            self.assertEqual(result['shop_cash_allocations'][0]['transaction_id'], 'cash-id')
            self.assertEqual(execute.call_args.kwargs['variables']['id'], node['id'])
            self.assertEqual(graphql.return_value.headers['X-Shopify-Access-Token'], self.instance.shopify_password)
            node['associatedPayout']['id'] = 'gid://shopify/ShopifyPaymentsPayout/999'
            execute.return_value = json.dumps({'data': {'node': node}})
            result = payout._enrich_shop_cash_transaction(data)
            self.assertFalse(result['shop_cash_allocations'])
            self.assertIn('another', result['shop_cash_detail_error'])

    def test_reimport_backfills_existing_breakdown_without_new_statement_lines(self):
        payout = self._cash_payout('refresh', [], amount=40)
        payout.generate_bank_statement()
        statements = self._statement_lines(payout)
        data = {'id': 'refresh-balance', 'type': 'credit', 'adjustment_reason': 'shop_cash',
            'currency': payout.currency_id.name, 'amount': 40, 'fee': 0, 'net': 40,
            'adjustment_order_transactions': [{'order_transaction_id': 'cash', 'amount': '40'}]}
        with patch.object(shopify.Transactions, 'find', return_value=[SimpleNamespace(to_dict=lambda: data)]):
            payout.refresh_payout_transaction_links()
        self.assertEqual(payout.payout_transaction_ids.filtered('shop_cash_kind').shop_cash_allocations[0]['transaction_id'], 'cash')
        self.assertEqual(self._statement_lines(payout), statements)

    def test_legacy_split_payment_uses_gateway_evidence_from_order(self):
        order, invoice = self._order('legacy', 265.91)
        cash = self._payment(order, 40, 'legacy-cash', invoice=invoice)
        self._payment(order, 225.91, 'legacy-card', gateway='shopify_payments', invoice=invoice)
        gateway = self.env['shopify.payment.gateway.ept'].create({'code': 'shop_cash',
            'name': 'Shop Cash', 'shopify_instance_id': self.instance.id})
        workflow = self.env['sale.workflow.process.ept'].create({'name': 'Shop Cash Legacy', 'journal_id': self.journal.id})
        order.write({'is_shopify_multi_payment': True, 'shopify_payment_ids': [Command.create({
            'payment_gateway_id': gateway.id, 'workflow_id': workflow.id, 'amount': 40,
            'payment_transaction_id': 'legacy-cash'})]})
        cash.write({'shopify_cash_gateway': False, 'shopify_cash_order_id': False, 'shopify_order_transaction_id': False})
        payout = self._cash_payout('legacy-credit', [{'order': {'id': 'legacy'}, 'amount': '40'}])
        payout.generate_bank_statement()
        self._process(payout)
        self.assertTrue(cash._seek_for_lines()[0].reconciled)
        self.assertEqual(payout.state, 'validated')

    def test_cancelled_full_refund_without_invoice_settles_both_directions(self):
        order, invoice = self._order('cancelled')
        invoice.button_draft()
        invoice.unlink()
        order.write({'state': 'cancel', 'canceled_in_shopify': True})
        capture = self._payment(order, 40, 'cancelled-cash')
        refund = self._payment(order, 40, 'cancelled-refund', kind='refund')
        (capture._seek_for_lines()[1] | refund._seek_for_lines()[1]).reconcile()
        credits = self._cash_payout('cancelled-credit', [{'order_transaction_id': 'cancelled-cash', 'amount': '40'}], fee=.90)
        refunds = self._cash_payout('cancelled-debit', [{'order_transaction_id': 'cancelled-refund', 'amount': '-40'}],
                                    amount=-40, reason='shop_cash_refund')
        for payout in credits | refunds:
            payout.generate_bank_statement()
            self._process(payout)
            self.assertEqual(payout.state, 'validated')
        self.assertTrue(capture._seek_for_lines()[0].reconciled)
        self.assertTrue(refund._seek_for_lines()[0].reconciled)
        self.assertFalse(order.invoice_ids)
        self.assertFalse(order.picking_ids)

    def test_grouped_order_allocation_can_match_multiple_captures_for_one_order(self):
        order, invoice = self._order('multi-capture', 40)
        payments = self._payment(order, 15, 'capture-first', invoice=invoice) | self._payment(order, 25, 'capture-second', invoice=invoice)
        payout = self._cash_payout('multi-capture-credit', [{'order': {'id': 'multi-capture'}, 'amount': '40'}])
        payout.generate_bank_statement()
        self._process(payout)
        self.assertEqual(payout.payout_transaction_ids.filtered('shop_cash_kind').shop_cash_payment_ids, payments)
        self.assertTrue(all(payment._seek_for_lines()[0].reconciled for payment in payments))

    def test_preview_includes_all_orders_from_grouped_credit(self):
        first, _invoice = self._order('preview-first', 40)
        second, _invoice = self._order('preview-second', 25)
        payout = self._cash_payout('preview-group', [{'order': {'id': 'preview-first'}, 'amount': '40'},
            {'order': {'id': 'preview-second'}, 'amount': '25'}])
        collected = []
        with patch.object(type(first), 'action_preview_shopify_payments', lambda orders: collected.extend(orders.ids)):
            payout.action_preview_payout_payments()
        self.assertEqual(set(collected), set((first | second).ids))

    def test_missing_details_and_reversal_require_review(self):
        payout = self._cash_payout('missing-details', [], amount=40)
        payout.generate_bank_statement()
        with self.assertRaisesRegex(UserError, 'Reimport'):
            self._process(payout)
        transaction = payout.payout_transaction_ids.filtered('shop_cash_kind')
        transaction.raw_transaction_type = 'shop_cash_credit_reversal'
        with self.assertRaisesRegex(UserError, 'reversals require'):
            self._process(payout)

    def test_graphql_permission_error_keeps_shop_cash_open_for_review(self):
        payout = self._cash_payout('permissions', [], amount=40)
        data = {'id': '1', 'type': 'credit', 'adjustment_reason': 'shop_cash',
            'currency': payout.currency_id.name, 'amount': '40', 'fee': '0', 'net': '40'}
        with patch.object(type(self.instance), 'connect_in_shopify'), patch.object(shopify, 'GraphQL') as graphql:
            graphql.return_value.headers = {}
            graphql.return_value.execute.return_value = json.dumps({'errors': [{'message': 'Access denied'}]})
            enriched = payout._enrich_shop_cash_transaction(data)
        self.assertFalse(enriched['shop_cash_allocations'])
        self.assertIn('permissions', enriched['shop_cash_detail_error'])
