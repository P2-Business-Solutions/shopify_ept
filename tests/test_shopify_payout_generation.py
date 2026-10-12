"""Accounting integration coverage for repeatable payout generation."""

import unittest
from unittest.mock import patch

try:
    from odoo import Command, fields
    from odoo.exceptions import UserError
    from odoo.tests import TransactionCase, tagged
except ModuleNotFoundError:
    TransactionCase = unittest.TestCase
    ODOO_AVAILABLE = False

    def tagged(*_tags):
        return lambda test_class: test_class
else:
    ODOO_AVAILABLE = True


@tagged('post_install', '-at_install')
@unittest.skipUnless(ODOO_AVAILABLE, 'Odoo test runtime is not available')
class PayoutTestCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        accounts = cls.env['account.account'].create([
            {
                'name': name,
                'code': code,
                'account_type': account_type,
                'reconcile': account_type == 'asset_current',
                'company_ids': [Command.set(company.ids)],
            }
            for name, code, account_type in (
                ('Payout Test Bank', 'SHPTBANK', 'asset_cash'),
                ('Payout Test Suspense', 'SHPTSUSP', 'asset_current'),
                ('Payout Test Fees', 'SHPTFEES', 'expense'),
            )
        ])
        cls.journal = cls.env['account.journal'].create({
            'name': 'Payout Generation Test',
            'code': 'SHPT',
            'type': 'bank',
            'company_id': company.id,
            'default_account_id': accounts[0].id,
            'suspense_account_id': accounts[1].id,
        })
        cls.instance = cls.env['shopify.instance.ept'].create({
            'name': 'Payout Generation Test Store',
            'shopify_company_id': company.id,
            'shopify_warehouse_id': cls.env['stock.warehouse'].search([
                ('company_id', '=', company.id),
            ], limit=1).id,
            'shopify_api_key': 'test-key',
            'shopify_password': 'test-password',
            'shopify_shared_secret': 'test-secret',
            'shopify_host': 'payout-test.example.com',
            'shopify_settlement_report_journal_id': cls.journal.id,
            'transaction_line_ids': [Command.create({
                'transaction_type': 'fees',
                'account_id': accounts[2].id,
            })],
        })
        cls.env.user.groups_id |= cls.env.ref('account.group_account_user')

    def _payout(self, reference):
        return self.env['shopify.payout.report.ept'].create({
            'instance_id': self.instance.id,
            'payout_reference_id': reference,
            'payout_date': fields.Date.today(),
            'currency_id': self.env.company.currency_id.id,
            'amount': 97,
            'payout_status': 'paid',
            'payout_transaction_ids': [
                Command.create({
                    'transaction_id': reference + '-charge',
                    'transaction_type': 'charge',
                    'source_order_id': reference + '-missing-order',
                    'amount': 100,
                    'fee': 3,
                    'is_remaining_statement': True,
                }),
                Command.create({
                    'transaction_type': 'fees',
                    'amount': -3,
                    'is_remaining_statement': True,
                }),
            ],
        })

    def _statement_lines(self, payouts):
        return self.env['account.bank.statement.line'].search([
            ('payout_id', 'in', payouts.ids),
        ])


class TestShopifyPayoutGeneration(PayoutTestCase):
    def test_linked_transactions_cannot_delete_or_change_accounting(self):
        payout = self._payout('protected-links')
        payout.generate_bank_statement()
        transaction = payout.payout_transaction_ids[:1]
        with self.assertRaises(UserError):
            transaction.unlink()
        with self.assertRaises(UserError):
            transaction.amount = 1
        with self.assertRaises(UserError):
            self._statement_lines(payout)[:1].payout_line_id = False
        payout.state = 'draft'
        with self.assertRaises(UserError):
            payout.unlink()
        self.assertEqual(len(self._statement_lines(payout)), 2)

    def test_zero_transactions_do_not_create_statement_lines(self):
        payout = self._payout('zero-line')
        payout.payout_transaction_ids = [Command.create({
            'transaction_id': 'zero-charge', 'transaction_type': 'charge',
            'source_order_id': 'missing-order', 'amount': 0,
        })]
        payout.generate_bank_statement()
        self.assertEqual(len(self._statement_lines(payout)), 2)
        self.assertFalse(payout.payout_transaction_ids.filtered(
            lambda row: row.transaction_id == 'zero-charge').is_remaining_statement)

    def test_wrong_company_journal_cannot_generate_statements(self):
        payout = self._payout('wrong-company')
        other_company = self.env['res.company'].create({'name': 'Payout Other Company'})
        self.instance.shopify_company_id = other_company
        with self.assertRaises(UserError):
            payout.generate_bank_statement()
        self.assertFalse(self._statement_lines(payout))

    def test_bulk_action_generates_multiple_payouts_and_is_repeatable(self):
        self.env.user.groups_id |= self.env.ref('account.group_account_manager')
        payouts = self._payout('bulk-1') | self._payout('bulk-2')
        action = self.env.ref('shopify_ept.action_generate_shopify_payout_statements')
        with patch.object(type(payouts.payout_transaction_ids), '_repair_source',
                          side_effect=UserError('Missing source order')):
            result = action.with_context(
                active_model=payouts._name, active_ids=payouts.ids,
            ).run()
        self.assertEqual(result['res_model'], 'shopify.payout.repair.ept')
        workspace = self.env[result['res_model']].browse(result['res_id'])
        self.assertEqual(workspace.payout_ids, payouts)
        self.assertEqual(len(workspace.line_ids), 2)
        lines = self._statement_lines(payouts)
        self.assertEqual(len(lines), 4)
        for payout in payouts:
            self.assertEqual(payout.state, 'partially_processed')
            self.assertEqual(sum(self._statement_lines(payout).mapped('amount')), 97)
        payouts.generate_bank_statement()
        self.assertEqual(self._statement_lines(payouts), lines)

    def test_blocked_refund_rolls_back_its_credit_and_generates_other_payout_rows(self):
        from ..models.shopify_repair_error import ShopifyRepairBlocked
        payout = self._payout('blocked-refund')
        other = self._payout('healthy-payout')
        partner = self.env['res.partner'].create({'name': 'Blocked Refund Customer'})
        order = self.env['sale.order'].create({'partner_id': partner.id,
            'shopify_instance_id': self.instance.id, 'shopify_order_id': 'blocked-order'})
        transaction = payout.payout_transaction_ids.filtered(lambda line: line.transaction_type == 'charge')
        transaction.write({'transaction_type': 'refund', 'amount': -45,
                           'order_id': order.id, 'source_order_id': order.shopify_order_id})
        payout.amount = -48
        sales = self.env['account.journal'].create({'name': 'Blocked Refund Sales', 'code': 'BLKR',
            'type': 'sale', 'company_id': self.env.company.id})
        original = type(payout).check_for_invoice_refund
        def refund(model, row, logs):
            if row == transaction:
                self.env['account.move'].create({'move_type': 'out_refund', 'journal_id': sales.id,
                    'partner_id': partner.id, 'ref': 'must-rollback-blocked-refund'})
                raise ShopifyRepairBlocked('Existing payments do not match Shopify cash history', 'payments')
            return original(model, row, logs)
        with patch.object(type(payout), 'check_for_invoice_refund', refund):
            (payout | other).generate_bank_statement()
        self.assertEqual(len(self._statement_lines(payout | other)), 4)
        statement = self._statement_lines(payout).filtered(lambda row: row.payout_line_id == transaction)
        self.assertFalse(statement.is_reconciled)
        self.assertFalse(statement.refund_invoice_id)
        self.assertFalse(self.env['account.move'].search([('ref', '=', 'must-rollback-blocked-refund')]))
        self.assertEqual(payout.state, 'generated')
        self.assertIn(transaction.transaction_id, payout.reimport_reconciliation_issue)
        self.assertIn('Existing payments', payout.common_log_line_ids.message)
        lines = self._statement_lines(payout | other)
        (payout | other).generate_bank_statement()
        self.assertEqual(self._statement_lines(payout | other), lines)

        def match_fee(model, fee_statement, _data, logs):
            _liquidity, suspense, _counterpart = fee_statement._seek_for_lines()
            suspense.account_id = model.instance_id.transaction_line_ids.filtered(
                lambda row: row.transaction_type == 'fees').account_id
            fee_statement.move_id.checked = True
            return logs
        with patch.object(type(payout), 'get_invoices_for_reconcile',
                          side_effect=ShopifyRepairBlocked('Existing payments need accounting review', 'payments')), \
                patch.object(type(payout), 'reconcile_other_transactions', match_fee):
            payout.process_bank_statement()
            fee_statement = self._statement_lines(payout) - statement
            self.assertTrue(fee_statement.is_reconciled)
            self.assertFalse(statement.is_reconciled)
            self.assertEqual(payout.state, 'partially_processed')
            fee_items = fee_statement.line_ids
            payout.process_bank_statement()
            self.assertEqual(fee_statement.line_ids, fee_items)
        self.assertIn('Existing payments need accounting review', payout.reimport_reconciliation_issue)

    def test_stale_remaining_flags_do_not_duplicate_existing_lines(self):
        payout = self._payout('stale-flags')
        payout.generate_bank_statement()
        lines = self._statement_lines(payout)
        payout.write({'state': 'partially_generated'})
        payout.payout_transaction_ids.write({'is_remaining_statement': True})
        payout.generate_bank_statement()
        self.assertEqual(self._statement_lines(payout), lines)
        self.assertFalse(any(payout.payout_transaction_ids.mapped('is_remaining_statement')))
        self.assertEqual(payout.state, 'generated')

    def test_partial_generation_uses_links_even_when_missing_line_flag_is_false(self):
        payout = self._payout('partial')
        charge = payout.payout_transaction_ids.filtered(lambda line: line.transaction_type == 'charge')
        existing = self.env['account.bank.statement.line'].create({
            'payment_ref': charge.transaction_id,
            'date': payout.payout_date,
            'amount': charge.amount,
            'journal_id': self.journal.id,
            'payout_id': payout.id,
            'payout_line_id': charge.id,
        })
        payout.write({'state': 'partially_generated'})
        payout.payout_transaction_ids.write({'is_remaining_statement': False})
        payout.generate_bank_statement()
        lines = self._statement_lines(payout)
        self.assertEqual(len(lines), 2)
        self.assertIn(existing, lines)
        self.assertEqual(sum(lines.mapped('amount')), 97)

    def test_bulk_generation_preserves_advanced_states(self):
        draft = self._payout('draft')
        payouts = draft
        advanced = []
        for state in ('generated', 'partially_processed', 'processed', 'validated'):
            payout = self._payout(state)
            payout.generate_bank_statement()
            payout.state = state
            advanced.append((payout, state, self._statement_lines(payout)))
            payouts |= payout
        payouts.generate_bank_statement()
        self.assertEqual(draft.state, 'generated')
        for payout, state, lines in advanced:
            self.assertEqual(payout.state, state)
            self.assertEqual(self._statement_lines(payout), lines)

    def test_legacy_entry_point_is_also_repeatable(self):
        payout = self._payout('legacy')
        payout.create_bank_statement_lines_for_payout_report()
        lines = self._statement_lines(payout)
        payout.create_bank_statement_lines_for_payout_report()
        self.assertEqual(self._statement_lines(payout), lines)

    def test_bulk_failure_rolls_back_new_statement_lines(self):
        first = self._payout('valid')
        second = self._payout('invalid-currency')
        second.currency_id = False
        with self.assertRaises(UserError), self.env.cr.savepoint():
            (first | second).generate_bank_statement()
        self.assertFalse(self._statement_lines(first | second))
        self.assertEqual(first.state, 'draft')
