"""Posting and reconciliation tests, run inside an Odoo accounting database."""

from datetime import timedelta
from unittest.mock import patch

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE

if ODOO_AVAILABLE:
    from odoo import Command, fields
    from odoo.exceptions import UserError


class TestShopifyPayoutSettlement(PayoutTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.transit = cls.env['account.account'].create({
            'name': 'Shopify Transit Test', 'code': 'SHPTTRANS',
            'account_type': 'asset_current', 'reconcile': True,
            'company_ids': [Command.set(cls.env.company.ids)],
        })
        bank_account = cls.journal.default_account_id.copy({'code': 'SHPTREAL'})
        cls.bank = cls.journal.copy({'name': 'Real Bank Test', 'code': 'REAL',
                                    'default_account_id': bank_account.id})
        (cls.bank.inbound_payment_method_line_ids | cls.bank.outbound_payment_method_line_ids).write({
            'payment_account_id': cls.transit.id,
        })
        cls.transfer_journal = cls.env['account.journal'].create({
            'name': 'Settlement Transfers Test', 'code': 'STLT', 'type': 'general',
            'company_id': cls.env.company.id,
        })
        cls.instance.write({
            'shopify_payout_bank_journal_id': cls.bank.id,
            'shopify_payout_transit_account_id': cls.transit.id,
            'shopify_payout_transfer_journal_id': cls.transfer_journal.id,
        })
        cls.counterpart = cls.instance.transaction_line_ids.account_id
        (cls.journal | cls.bank).autocheck_on_post = True

    def _book_activity(self, payout):
        """Book payout activity to non-suspense accounts without a live Shopify API."""
        for row in payout.payout_transaction_ids.filtered('amount'):
            self.env['account.bank.statement.line'].create({
                'journal_id': self.journal.id, 'date': payout.payout_date,
                'payment_ref': row.transaction_id or row.transaction_type,
                'amount': row.amount, 'payout_id': payout.id, 'payout_line_id': row.id,
                'shopify_transaction_type': row.transaction_type,
                'counterpart_account_id': self.counterpart.id,
            })
            row.is_remaining_statement = False
        payout.state = 'processed'

    def _bank_match(self, payout, amount=None):
        bank_line = self.env['account.bank.statement.line'].create({
            'journal_id': self.bank.id, 'date': fields.Date.today(),
            'payment_ref': 'Shopify payout ' + payout.payout_reference_id,
            'amount': payout.amount if amount is None else amount,
            'counterpart_account_id': self.transit.id,
        })
        counterpart = bank_line.move_id.line_ids.filtered(lambda line: line.account_id == self.transit)
        (payout.settlement_line_id | counterpart).reconcile()
        return bank_line

    def test_transfer_is_balanced_does_not_rebook_activity_and_is_repeatable(self):
        payout = self._payout('settlement')
        self._book_activity(payout)
        activity = self._statement_lines(payout).move_id.line_ids
        original_balances = activity.mapped('balance')
        payout.action_create_settlement_transfer()
        move = payout.settlement_move_id
        self.assertEqual(move.state, 'posted')
        self.assertEqual(len(move.line_ids), 2)
        self.assertEqual(sum(move.line_ids.mapped('balance')), 0)
        self.assertEqual(payout.settlement_line_id.balance, 97)
        self.assertEqual(move.line_ids.filtered(lambda line: line.account_id == self.journal.default_account_id).balance, -97)
        self.assertEqual(activity.mapped('balance'), original_balances)
        self.assertEqual(payout.settlement_status, 'pending')
        payout.action_create_settlement_transfer()
        self.assertEqual(payout.settlement_move_id, move)
        self.assertEqual(self.env['account.move'].search_count([
            ('shopify_settlement_payout_id', '=', payout.id)]), 1)

    def test_actual_bank_match_partial_match_and_unmatch_update_status(self):
        payout = self._payout('bank-match')
        self._book_activity(payout)
        payout.action_create_settlement_transfer()
        first = self._bank_match(payout, 40)
        self.assertEqual(payout.settlement_status, 'partial')
        second = self._bank_match(payout, 57)
        self.assertEqual(payout.settlement_status, 'matched')
        self.assertEqual(payout.settlement_bank_line_ids, first | second)
        payout.settlement_line_id.remove_move_reconcile()
        self.assertEqual(payout.settlement_status, 'pending')

    def test_duplicate_report_for_same_shopify_reference_cannot_transfer_twice(self):
        original = self._payout('same-reference')
        self._book_activity(original)
        original.action_create_settlement_transfer()
        duplicate = self._payout('same-reference')
        self._book_activity(duplicate)
        with self.assertRaises(UserError):
            duplicate.action_create_settlement_transfer()
        self.assertFalse(duplicate.settlement_move_id)

    def test_unchecked_source_blocks_transfer_and_updates_existing_status(self):
        payout = self._payout('source-review')
        self._book_activity(payout)
        statement = self._statement_lines(payout)[:1]
        statement.move_id.checked = False
        with self.assertRaises(UserError):
            payout.action_create_settlement_transfer()
        self.assertFalse(payout.settlement_move_id)
        statement.move_id.checked = True
        payout.action_create_settlement_transfer()
        self.assertEqual(payout.settlement_status, 'pending')
        statement.move_id.checked = False
        self.assertEqual(payout.settlement_status, 'review')
        statement.move_id.checked = True
        self.assertEqual(payout.settlement_status, 'pending')

    def test_unchecked_bank_receipt_requires_review(self):
        payout = self._payout('bank-review')
        self._book_activity(payout)
        payout.action_create_settlement_transfer()
        bank_line = self._bank_match(payout)
        self.assertEqual(payout.settlement_status, 'matched')
        bank_line.move_id.checked = False
        self.assertEqual(payout.settlement_status, 'review')
        bank_line.move_id.checked = True
        self.assertEqual(payout.settlement_status, 'matched')

    def test_review_filter_tracks_checked_changes(self):
        payout = self._payout('review-search')
        self._book_activity(payout)
        payout.action_create_settlement_transfer()
        bank_line = self._bank_match(payout)
        domain = [('id', '=', payout.id), ('settlement_status', '=', 'review')]
        self.assertFalse(payout.search(domain))
        bank_line.move_id.checked = False
        self.assertEqual(payout.search(domain), payout)
        bank_line.move_id.checked = True
        self.assertFalse(payout.search(domain))

    def test_bulk_statement_review_reopens_each_linked_payout(self):
        payouts = self._payout('bulk-review-1') | self._payout('bulk-review-2')
        for payout in payouts:
            self._book_activity(payout)
            payout.validate_statement()
        self._statement_lines(payouts).write({'checked': False})
        self.assertEqual(payouts.mapped('state'), ['partially_processed', 'partially_processed'])

    def test_failed_adjustment_processing_rolls_back_line_changes(self):
        payout = self._payout('adjustment-rollback')
        payout.payout_transaction_ids.unlink()
        payout.write({'amount': -3, 'payout_transaction_ids': [Command.create({
            'transaction_type': 'adjustment', 'transaction_id': 'adjustment', 'amount': -3,
        })]})
        payout.generate_bank_statement()
        statement = self._statement_lines(payout)
        original_ref = statement.payment_ref

        def fail_after_edit(_payout, line, *_args):
            line.payment_ref = 'partial edit that must roll back'
            raise UserError('Adjustment could not be reconciled')

        with patch.object(type(payout), 'reconcile_other_transactions', fail_after_edit):
            payout.with_context(cron_process=True).process_bank_statement()
        self.assertEqual(statement.payment_ref, original_ref)
        self.assertFalse(statement.is_reconciled)
        self.assertEqual(payout.state, 'partially_processed')

    def test_nonbank_writeoff_is_not_reported_as_bank_matched(self):
        payout = self._payout('writeoff')
        self._book_activity(payout)
        payout.action_create_settlement_transfer()
        writeoff = self.env['account.move'].create({
            'journal_id': self.transfer_journal.id,
            'line_ids': [Command.create({'account_id': account.id, 'debit': debit, 'credit': credit})
                         for account, debit, credit in ((self.transit, 0, 97), (self.counterpart, 97, 0))],
        })
        writeoff.action_post()
        (payout.settlement_line_id | writeoff.line_ids.filtered(lambda line: line.account_id == self.transit)).reconcile()
        self.assertEqual(payout.settlement_status, 'review')

    def test_negative_payout_matches_bank_withdrawal(self):
        payout = self._payout('negative')
        payout.payout_transaction_ids.unlink()
        payout.write({'amount': -100, 'payout_transaction_ids': [Command.create({
            'transaction_type': 'refund', 'transaction_id': 'refund-next-day', 'amount': -100,
        })]})
        self._book_activity(payout)
        payout.action_create_settlement_transfer()
        self.assertEqual(payout.settlement_line_id.balance, -100)
        self._bank_match(payout)
        self.assertEqual(payout.settlement_status, 'matched')

    def test_unbalanced_or_missing_activity_never_creates_transfer(self):
        payout = self._payout('mismatch')
        self._book_activity(payout)
        payout.amount = 98
        with self.assertRaises(UserError):
            payout.action_create_settlement_transfer()
        self.assertFalse(payout.settlement_move_id)
        payout.amount = 97
        payout.payout_transaction_ids = [Command.create({
            'transaction_id': 'missing', 'transaction_type': 'charge', 'amount': 10,
        })]
        with self.assertRaises(UserError):
            payout.action_create_settlement_transfer()
        self.assertFalse(payout.settlement_move_id)

    def test_unreconciled_activity_never_validates_or_transfers(self):
        payout = self._payout('unreconciled')
        payout.generate_bank_statement()
        with self.assertRaises(UserError):
            payout.validate_statement()
        self.assertFalse(payout.settlement_move_id)

    def test_existing_imported_payout_outflow_is_reused(self):
        payout = self._payout('reuse')
        self._book_activity(payout)
        row = self.env['shopify.payout.report.line.ept'].create({
            'payout_id': payout.id, 'transaction_id': 'payout-outflow',
            'transaction_type': 'payout', 'amount': -97,
        })
        outflow = self.env['account.bank.statement.line'].create({
            'journal_id': self.journal.id, 'date': payout.payout_date,
            'payment_ref': 'payout-outflow', 'amount': -97,
            'payout_id': payout.id, 'payout_line_id': row.id,
            'shopify_transaction_type': 'payout', 'counterpart_account_id': self.transit.id,
        })
        payout.action_create_settlement_transfer()
        self.assertEqual(payout.settlement_move_id, outflow.move_id)
        self._bank_match(payout)
        self.assertEqual(payout.settlement_status, 'matched')

    def test_wrong_outflow_account_blocks_duplicate_transfer(self):
        payout = self._payout('wrong-outflow')
        payout.payout_transaction_ids = [Command.create({
            'transaction_type': 'payout', 'transaction_id': 'outflow', 'amount': -97,
        })]
        self._book_activity(payout)
        with self.assertRaises(UserError):
            payout.action_create_settlement_transfer()
        self.assertFalse(payout.settlement_move_id)

    def test_automatic_transfer_after_validation(self):
        payout = self._payout('automatic')
        self._book_activity(payout)
        self.instance.shopify_auto_settlement_transfer = True
        payout.validate_statement()
        self.assertEqual(payout.state, 'validated')
        self.assertEqual(payout.settlement_status, 'pending')

    def test_failed_payout_and_wrong_bank_configuration_block_posting(self):
        payout = self._payout('failed')
        self._book_activity(payout)
        payout.payout_status = 'failed'
        with self.assertRaises(UserError):
            payout.action_create_settlement_transfer()
        payout.payout_status = 'paid'
        self.instance.shopify_payout_bank_journal_id = self.journal
        with self.assertRaises(UserError):
            payout.action_create_settlement_transfer()
        self.assertFalse(payout.settlement_move_id)

    def test_charge_and_next_day_refund_reconcile_to_separate_payouts(self):
        self._check_split_day_refund(use_transaction_ids=True)

    def test_legacy_payments_on_refunded_order_remain_independently_matchable(self):
        self._check_split_day_refund(use_transaction_ids=False)

    def _check_split_day_refund(self, use_transaction_ids):
        if 'bank.rec.widget' not in self.env.registry.models:
            self.skipTest('The Odoo Enterprise bank reconciliation widget is required')
        receivable = self.env['account.account'].create({
            'name': 'Test Receivable', 'code': 'SHPTREC',
            'account_type': 'asset_receivable', 'reconcile': True,
            'company_ids': [Command.set(self.env.company.ids)],
        })
        partner = self.env['res.partner'].create({
            'name': 'Refund Customer', 'property_account_receivable_id': receivable.id,
        })
        order = self.env['sale.order'].create({'partner_id': partner.id,
                                             'shopify_instance_id': self.instance.id,
                                             'shopify_order_id': 'split-day-order'})
        product = self.env['product.product'].create({'name': 'Refund Test Service', 'type': 'service'})
        sale_line = self.env['sale.order.line'].create({
            'order_id': order.id, 'product_id': product.id, 'product_uom_qty': 1, 'price_unit': 100,
            'tax_id': [Command.clear()],
        })
        sales_journal = self.env['account.journal'].create({
            'name': 'Payout Test Sales', 'code': 'PTS', 'type': 'sale',
        })
        outstanding = self.transit.copy({'code': 'SHPTPAY', 'name': 'Shopify Outstanding Test'})
        (self.journal.inbound_payment_method_line_ids | self.journal.outbound_payment_method_line_ids).write({
            'payment_account_id': outstanding.id,
        })
        payouts = self.env['shopify.payout.report.ept']
        payments = self.env['account.payment']
        invoices = self.env['account.move']
        for kind, direction, amount, transaction_id, days in (
                ('charge', 'inbound', 100, 'original-charge', 1),
                ('refund', 'outbound', -100, 'later-refund', 0)):
            methods = (self.journal.inbound_payment_method_line_ids if direction == 'inbound'
                       else self.journal.outbound_payment_method_line_ids)
            invoice = self.env['account.move'].create({
                'journal_id': sales_journal.id, 'partner_id': partner.id,
                'move_type': 'out_invoice' if direction == 'inbound' else 'out_refund',
                'invoice_date': fields.Date.today() - timedelta(days=days),
                'invoice_line_ids': [Command.create({
                    'name': kind, 'quantity': 1, 'price_unit': 100, 'account_id': self.counterpart.id,
                    'sale_line_ids': [Command.link(sale_line.id)], 'tax_ids': [Command.clear()],
                })],
            })
            invoice.action_post()
            invoices |= invoice
            payment = self.env['account.payment'].create({
                'partner_id': partner.id, 'partner_type': 'customer', 'payment_type': direction,
                'amount': abs(amount), 'journal_id': self.journal.id,
                'payment_method_line_id': methods[:1].id,
                'currency_id': self.env.company.currency_id.id,
                'shopify_instance_id': self.instance.id,
                'shopify_order_transaction_id': transaction_id if use_transaction_ids else False,
                'invoice_ids': [Command.set(invoice.ids)],
            })
            payment.action_post()
            (invoice.line_ids.filtered(lambda line: line.account_id == receivable)
             | payment._seek_for_lines()[1]).reconcile()
            payments |= payment
            payouts |= self.env['shopify.payout.report.ept'].create({
                'instance_id': self.instance.id, 'payout_reference_id': transaction_id,
                'payout_date': fields.Date.today() - timedelta(days=days),
                'currency_id': self.env.company.currency_id.id, 'amount': amount,
                'payout_status': 'paid',
                'payout_transaction_ids': [Command.create({
                    'transaction_type': kind, 'amount': amount, 'transaction_id': transaction_id,
                    'source_order_transaction_id': transaction_id if use_transaction_ids else False,
                    'order_id': order.id,
                })],
            })
        # Both payments already exist when the earlier payout is processed.
        if not use_transaction_ids:
            # Reproduce the stored invoice status that broke the old fallback.
            # The source of truth for settlement is the payment's open item.
            invoices.filtered(lambda invoice: invoice.move_type == 'out_invoice').payment_state = 'reversed'
        self.instance.shopify_auto_settlement_transfer = True
        payouts.generate_bank_statement()
        for payout in payouts:
            payout.process_bank_statement()
            self.assertEqual(payout.state, 'validated')
            self.assertEqual(payout.settlement_line_id.amount_currency, payout.amount)
        self.assertTrue(all(self._statement_lines(payouts).mapped('is_reconciled')))
        self.assertTrue(all(payment._seek_for_lines()[0].reconciled for payment in payments))

    def test_foreign_currency_transfer_uses_booked_liquidity_balance(self):
        foreign = self.env['res.currency'].search([
            ('id', '!=', self.env.company.currency_id.id),
        ], limit=1)
        if not foreign:
            self.skipTest('A second currency is required')
        self.env['res.currency.rate'].create({
            'currency_id': foreign.id, 'company_id': self.env.company.id,
            'name': fields.Date.today(), 'rate': 0.8,
        })
        (self.journal | self.bank).currency_id = foreign
        payout = self._payout('foreign')
        payout.currency_id = foreign
        self._book_activity(payout)
        expected = sum(self._statement_lines(payout).move_id.line_ids.filtered(
            lambda line: line.account_id == self.journal.default_account_id).mapped('balance'))
        payout.action_create_settlement_transfer()
        self.assertEqual(payout.settlement_line_id.currency_id, foreign)
        self.assertEqual(payout.settlement_line_id.amount_currency, 97)
        self.assertEqual(payout.settlement_line_id.balance, expected)
