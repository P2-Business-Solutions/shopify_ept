"""Database integration tests for gross cash with net invoices and historical repair."""
from datetime import timedelta
from copy import deepcopy
from unittest.mock import patch
try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
if ODOO_AVAILABLE:
    from odoo import Command, fields
    from odoo.exceptions import UserError


class TestShopifyCashSync(PayoutTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance.shopify_transaction_payment_sync = True
        cls.outstanding = cls.env['account.account'].create({
            'name': 'Cash Sync Outstanding', 'code': 'CSSOUT', 'account_type': 'asset_current',
            'reconcile': True, 'company_ids': [Command.set(cls.env.company.ids)],
        })
        cls.receivable = cls.env['account.account'].create({
            'name': 'Cash Sync Receivable', 'code': 'CSSREC', 'account_type': 'asset_receivable',
            'reconcile': True, 'company_ids': [Command.set(cls.env.company.ids)],
        })
        (cls.journal.inbound_payment_method_line_ids | cls.journal.outbound_payment_method_line_ids).write({
            'payment_account_id': cls.outstanding.id,
        })
        cls.gateway = cls.env['shopify.payment.gateway.ept'].create({
            'name': 'Shopify Payments', 'code': 'shopify_payments', 'shopify_instance_id': cls.instance.id,
        })
        cls.workflow = cls.env['sale.workflow.process.ept'].create({
            'name': 'Cash Sync Test', 'journal_id': cls.journal.id, 'register_payment': True,
            'inbound_payment_method_id': cls.journal.inbound_payment_method_line_ids[:1].payment_method_id.id,
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Cash Sync Customer', 'property_account_receivable_id': cls.receivable.id,
        })
        cls.sales_journal = cls.env['account.journal'].create({
            'name': 'Cash Sync Sales', 'code': 'CSS', 'type': 'sale', 'company_id': cls.env.company.id,
        })
        cls.product = cls.env['product.product'].create({'name': 'Cash Sync Item', 'type': 'service'})
        cls.revenue = cls.env['account.account'].create({
            'name': 'Cash Sync Sales', 'code': 'CSSSALE', 'account_type': 'income',
            'company_ids': [Command.set(cls.env.company.ids)],
        })

    def _fixture(self, gross=False):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'shopify_instance_id': self.instance.id, 'shopify_order_id': '135267',
            'shopify_payment_gateway_id': self.gateway.id, 'auto_workflow_process_id': self.workflow.id,
        })
        invoice_lines = []
        for key, amount in [('retained', 197.10), ('removed', 45.0)]:
            line = self.env['sale.order.line'].create({
                'order_id': order.id, 'product_id': self.product.id, 'name': key, 'shopify_line_id': key,
                'product_uom_qty': 1, 'price_unit': amount, 'tax_id': [Command.clear()],
            })
            if gross or key == 'retained':
                invoice_lines.append(Command.create({
                    'product_id': self.product.id, 'name': key, 'account_id': self.revenue.id,
                    'quantity': 1, 'price_unit': amount, 'tax_ids': [Command.clear()],
                    'sale_line_ids': [Command.set(line.ids)],
                }))
        invoice = self.env['account.move'].create({
            'journal_id': self.sales_journal.id, 'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': fields.Date.today() - timedelta(days=1),
            'date': fields.Date.today() - timedelta(days=1), 'invoice_line_ids': invoice_lines,
            'shopify_instance_id': self.instance.id,
        })
        invoice.action_post()
        date = invoice.invoice_date.isoformat()
        common = dict(order_id='135267', currency=order.currency_id.name, gateway='shopify_payments',
                      status='success', processed_at=date + 'T10:00:00-04:00')
        events = [dict(common, id='charge', kind='sale', amount='242.10'),
                  dict(common, id='refund', kind='refund', amount='45.00', parent_id='charge',
                       processed_at=fields.Date.today().isoformat() + 'T10:00:00-04:00')]
        payload = {'id': '135267', 'line_items': [dict(id='removed', quantity=1, current_quantity=0)],
                   'refunds': [dict(id='refund-doc', transactions=[dict(id='refund')],
                                   refund_line_items=[dict(line_item_id='removed', quantity=1,
                                                          subtotal='45.00', total_tax='0.00')])]}
        return order, invoice, payload, events

    def _legacy(self, invoice, amount):
        payment = self.env['account.payment'].create({
            'partner_id': self.partner.id, 'partner_type': 'customer', 'payment_type': 'inbound',
            'amount': amount, 'currency_id': invoice.currency_id.id, 'journal_id': self.journal.id,
            'payment_method_line_id': self.journal.inbound_payment_method_line_ids[:1].id,
            'date': invoice.invoice_date, 'invoice_ids': [Command.set(invoice.ids)],
        })
        payment.action_post()
        (payment._seek_for_lines()[1] | invoice.line_ids.filtered(
            lambda row: row.account_id == self.receivable and not row.reconciled)).reconcile()
        invoice.matched_payment_ids |= payment
        return payment

    def test_net_invoice_creates_gross_receipt_and_independent_refund(self):
        order, invoice, payload, events = self._fixture()
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order.paid_invoice_ept(invoice)
            payments = self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)])
            self.assertEqual(sorted(payments.mapped('amount')), [45, 242.10])
            self.assertEqual(payments.filtered(lambda pay: pay.payment_type == 'inbound').date, invoice.invoice_date)
            self.assertEqual(payments.filtered(lambda pay: pay.payment_type == 'outbound').date, fields.Date.today())
            self.assertEqual(invoice.amount_total, 197.10)
            self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))
            self.assertFalse(order.invoice_ids.filtered(lambda move: move.move_type == 'out_refund'))
            for payment in payments:
                liquidity, counterpart, _ = payment._seek_for_lines()
                self.assertFalse(liquidity.reconciled)
                self.assertEqual(abs(liquidity.amount_residual_currency), payment.amount)
                self.assertTrue(counterpart.reconciled)
            audit_count = len(order.shopify_payment_audit_ids)
            order.paid_invoice_ept(invoice)
            self.assertEqual(len(order.shopify_payment_audit_ids), audit_count)
            self.assertEqual(self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)]), payments)

    def test_gross_invoice_gets_one_credit_note_and_reuses_receipt(self):
        order, invoice, payload, events = self._fixture(gross=True)
        receipt = self._legacy(invoice, 242.10)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order._sync_shopify_cash()
            credit = order.invoice_ids.filtered(lambda row: row.move_type == 'out_refund')
            self.assertEqual(len(credit), 1)
            self.assertEqual(credit.amount_total, 45)
            self.assertEqual(receipt.shopify_order_transaction_id, 'charge')
            order.create_shopify_partially_refund(payload['refunds'], order.name)
            self.assertEqual(order.invoice_ids.filtered(lambda row: row.move_type == 'out_refund'), credit)

    def test_preview_does_not_post_and_repair_reverses_only_net_payment(self):
        order, invoice, payload, events = self._fixture()
        legacy = self._legacy(invoice, 197.10)
        original_move = legacy.move_id
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaises(UserError):
                order._sync_shopify_cash()
            count = self.env['account.move'].search_count([])
            action = order.action_preview_shopify_payments()
            wizard = self.env['shopify.payment.repair.ept'].browse(action['res_id'])
            self.assertEqual(wizard.state, 'preview')
            self.assertEqual(self.env['account.move'].search_count([]), count)
            wizard.action_apply()
            self.assertEqual(legacy.state, 'canceled')
            self.assertEqual(original_move.state, 'posted')
            self.assertEqual(len(original_move.reversal_move_ids), 1)
            self.assertEqual(invoice.amount_total, 197.10)
            self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))
            self.assertEqual(order.shopify_payment_audit_ids.replaced_payment_ids, legacy)
            count = self.env['account.move'].search_count([])
            with self.assertRaises(UserError):
                wizard.action_apply()
            self.assertEqual(self.env['account.move'].search_count([]), count)

    def test_stale_preview_blocks_repair(self):
        order, invoice, payload, events = self._fixture()
        self._legacy(invoice, 197.10)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            order.shopify_payment_sync_revision += 1
            with self.assertRaises(UserError):
                wizard.action_apply()
            self.assertFalse(order.shopify_payment_audit_ids)

    def test_bank_matched_legacy_payment_is_not_replaced(self):
        order, invoice, payload, events = self._fixture()
        legacy = self._legacy(invoice, 197.10)
        bank_line = self.env['account.bank.statement.line'].create({
            'journal_id': self.journal.id, 'date': fields.Date.today(), 'payment_ref': 'already matched',
            'amount': 197.10, 'counterpart_account_id': self.outstanding.id,
        })
        (legacy._seek_for_lines()[0] | bank_line.line_ids.filtered(lambda row: row.account_id == self.outstanding)).reconcile()
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'blocked')
            self.assertFalse(legacy.move_id.reversal_move_ids)

    def test_atomic_failure_rolls_back_replacement(self):
        order, invoice, payload, events = self._fixture()
        legacy = self._legacy(invoice, 197.10)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            with patch.object(type(self.env['account.payment']), 'action_post', side_effect=UserError('Posting failed')):
                with self.assertRaises(UserError):
                    wizard.action_apply()
            self.assertNotEqual(legacy.state, 'canceled')
            self.assertFalse(legacy.move_id.reversal_move_ids)
            self.assertFalse(order.shopify_payment_audit_ids)
            self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))

    def test_ambiguous_legacy_payments_are_blocked(self):
        order, invoice, payload, events = self._fixture()
        self._legacy(invoice, 197.10)
        extra = self._legacy(invoice, 197.10)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'blocked')
            self.assertFalse(extra.move_id.reversal_move_ids)

    def test_unexplained_refund_is_blocked_without_credit_or_payment(self):
        order, invoice, payload, events = self._fixture()
        payload['refunds'][0]['refund_line_items'][0]['subtotal'] = '40'
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'blocked')
            self.assertFalse(order.shopify_payment_audit_ids)

    def test_net_refund_payment_matches_payout_without_credit_note(self):
        order, invoice, payload, events = self._fixture()
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order._sync_shopify_cash()
        payout = self._payout('refund-cash-link')
        payout.payout_transaction_ids.unlink()
        payout.write({'amount': -45, 'payout_transaction_ids': [Command.create({
            'transaction_id': 'balance-refund', 'transaction_type': 'refund',
            'amount': -45, 'order_id': order.id,
        })]})
        payment = payout.find_payment_for_payout_transaction(payout.payout_transaction_ids)
        self.assertEqual(payment.shopify_order_transaction_id, 'refund')
        self.assertEqual(payment.amount, 45)
        if 'bank.rec.widget' not in self.env.registry.models:
            self.skipTest('Enterprise bank reconciliation widget is required for the payout match')
        payout.generate_bank_statement()
        payout.process_bank_statement()
        self.assertEqual(payout.state, 'validated')
        self.assertTrue(payment._seek_for_lines()[0].reconciled)

    def test_locked_transaction_date_blocks_preview(self):
        order, invoice, payload, events = self._fixture()
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)), \
                patch.object(type(order.company_id), '_get_violated_lock_dates', return_value=[(fields.Date.today(), 'Test lock')]):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'blocked')
            self.assertFalse(order.shopify_payment_audit_ids)

    def test_repeated_refund_webhook_does_not_subtract_twice(self):
        order, invoice, payload, events = self._fixture()
        split = self.env['shopify.order.payment.ept'].create({
            'order_id': order.id, 'payment_gateway_id': self.gateway.id, 'workflow_id': self.workflow.id,
            'payment_transaction_id': 'charge', 'amount': 242.10, 'remaining_refund_amount': 242.10,
        })
        refunds = [{'transactions': [events[1], events[1]]}]
        order.prepare_vals_shopify_multi_payment_refund(refunds, order)
        self.assertEqual(split.remaining_refund_amount, 197.10)
        order.prepare_vals_shopify_multi_payment_refund(refunds, order)
        self.assertEqual(split.remaining_refund_amount, 197.10)

    def test_changed_shopify_history_invalidates_preview(self):
        order, invoice, payload, events = self._fixture()
        self._legacy(invoice, 197.10)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            events[0]['processed_at'] = fields.Date.today().isoformat() + 'T11:00:00-04:00'
            with self.assertRaises(UserError):
                wizard.action_apply()
            self.assertFalse(order.shopify_payment_audit_ids)

    def test_refunded_charge_is_retained_in_gateway_setup(self):
        order, invoice, payload, events = self._fixture()
        self.assertEqual(order.prepare_final_list_of_transactions(events + events), events[:1])

    def test_stale_refund_payload_cannot_restore_refundable_amount(self):
        order, invoice, payload, events = self._fixture()
        split = self.env['shopify.order.payment.ept'].create({
            'order_id': order.id, 'payment_gateway_id': self.gateway.id, 'workflow_id': self.workflow.id,
            'payment_transaction_id': 'charge', 'amount': 242.10, 'remaining_refund_amount': 242.10,
        })
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order._sync_shopify_cash()
        self.assertEqual(split.remaining_refund_amount, 197.10)
        order.prepare_vals_shopify_multi_payment_refund([], order)
        self.assertEqual(split.remaining_refund_amount, 197.10)

    def test_changed_refund_evidence_invalidates_preview(self):
        order, invoice, payload, events = self._fixture()
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            # Both versions explain the same net amount, but only the first was reviewed.
            payload['line_items'][0]['quantity'] = 2
            payload['refunds'][0]['refund_line_items'][0]['quantity'] = 2
            with self.assertRaises(UserError):
                wizard.action_apply()
            self.assertFalse(order.shopify_payment_audit_ids)

    def test_accounting_manager_can_apply_and_read_audit_without_sudo(self):
        order, invoice, payload, events = self._fixture()
        manager = self.env['res.users'].create({
            'name': 'Payment Review Manager', 'login': 'payment-review-manager',
            'email': 'payment-review-manager@example.invalid',
            'company_id': self.env.company.id, 'company_ids': [Command.set(self.env.company.ids)],
            'groups_id': [Command.set([self.env.ref('shopify_ept.group_shopify_manager_ept').id])],
        })
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order = order.with_user(manager)
            action = self.env.ref('shopify_ept.action_preview_shopify_payment_repairs').with_user(manager).with_context(
                active_model='sale.order', active_ids=order.ids).run()
            wizard = self.env['shopify.payment.repair.ept'].with_user(manager).browse(action['res_id'])
            self.assertEqual(wizard.state, 'preview', wizard.preview_html)
            wizard.action_apply()
            self.assertEqual(len(order.shopify_payment_audit_ids.read(['plan_text', 'payment_ids'])), 1)
            self.assertEqual(order.shopify_payment_audit_ids.create_uid, manager)
            with self.assertRaises(UserError):
                self.env['shopify.payment.audit.ept'].with_user(manager).create({'order_id': order.id, 'plan': {}})

    def test_connector_user_can_run_normal_payment_workflow(self):
        order, invoice, payload, events = self._fixture()
        user = self.env['res.users'].create({
            'name': 'Payment Workflow User', 'login': 'payment-workflow-user',
            'email': 'payment-workflow-user@example.invalid',
            'company_id': self.env.company.id, 'company_ids': [Command.set(self.env.company.ids)],
            'groups_id': [Command.set([self.env.ref('shopify_ept.group_shopify_ept').id])],
        })
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order = order.with_user(user)
            order.paid_invoice_ept(invoice.with_user(user))
            self.assertEqual(order.shopify_payment_audit_ids.create_uid, user)
            with self.assertRaises(UserError):
                order.action_preview_shopify_payments()

    def test_later_refund_of_a_repaired_net_invoice_credits_only_new_refund(self):
        order, invoice, payload, events = self._fixture()
        self._legacy(invoice, 197.10)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            wizard.action_apply()
            later = dict(events[1], id='later-refund', amount='197.10')
            later['processed_at'] = fields.Date.today().isoformat() + 'T11:00:00-04:00'
            events.append(later)
            payload['line_items'].append(dict(id='retained', quantity=1, current_quantity=0))
            payload['refunds'].append(dict(id='later-document', transactions=[dict(id='later-refund')],
                refund_line_items=[dict(line_item_id='retained', quantity=1, subtotal='197.10', total_tax='0')]))
            order._sync_shopify_cash()
            credits = order.invoice_ids.filtered(lambda move: move.move_type == 'out_refund')
            self.assertEqual(credits.mapped('shopify_refund_id'), ['later-document'])
            self.assertEqual(credits.amount_total, 197.10)
            self.assertEqual(invoice.amount_total, 197.10)
            self.assertTrue(order.currency_id.is_zero(invoice.amount_residual + credits.amount_residual))
            count = self.env['account.payment'].search_count([('shopify_cash_order_id', '=', order.id)])
            order._sync_shopify_cash()
            self.assertEqual(count, 3)
            self.assertEqual(self.env['account.payment'].search_count([('shopify_cash_order_id', '=', order.id)]), count)

    def test_ordinary_order_records_one_original_receipt(self):
        order, invoice, payload, events = self._fixture(gross=True)
        payload['refunds'] = []
        events = events[:1]
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order.paid_invoice_ept(invoice)
            order.paid_invoice_ept(invoice)
        payments = self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)])
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments.amount, 242.10)
        self.assertTrue(order.currency_id.is_zero(invoice.amount_residual))
        self.assertFalse(payments._seek_for_lines()[0].reconciled)

    def test_transaction_payment_recording_is_opt_in(self):
        order, invoice, payload, events = self._fixture(gross=True)
        self.instance.shopify_transaction_payment_sync = False
        try:
            with patch.object(type(order), '_shopify_cash_source',
                              side_effect=AssertionError('Shopify must not be read when the setting is off')):
                order.paid_invoice_ept(invoice)
        finally:
            self.instance.shopify_transaction_payment_sync = True
        self.assertFalse(self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)]))
        self.assertTrue(order.currency_id.is_zero(invoice.amount_residual))
        self.assertFalse(order.shopify_payment_audit_ids)

    def test_unrecordable_cash_history_does_not_block_invoicing(self):
        order, invoice, payload, events = self._fixture(gross=True)
        payload['refunds'] = []
        count = self.env['account.move'].search_count([])
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, [])):
            order.paid_invoice_ept(invoice)
        self.assertEqual(self.env['account.move'].search_count([]), count)
        self.assertFalse(self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)]))
        self.assertEqual(invoice.amount_residual, invoice.amount_total)
        self.assertIn('No successful Shopify charge', order.message_ids[:1].body)
        self.assertTrue(self.env['common.log.lines.ept'].search([
            ('res_id', '=', order.id), ('message', 'ilike', 'No successful Shopify charge')]))

    def test_zero_value_invoice_skips_cash_recording(self):
        order, invoice, payload, events = self._fixture(gross=True)
        zero = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'journal_id': self.sales_journal.id,
            'invoice_line_ids': [Command.create({'product_id': self.product.id, 'quantity': 1, 'price_unit': 0,
                                                 'tax_ids': [Command.clear()]})],
        })
        zero.action_post()
        with patch.object(type(order), '_shopify_cash_source',
                          side_effect=AssertionError('Zero invoices must not read Shopify')):
            order.paid_invoice_ept(zero)
        self.assertFalse(zero.message_ids.filtered(lambda row: 'skipped' in (row.body or '')))

    def test_preview_finds_order_imported_after_payout(self):
        order, invoice, payload, events = self._fixture()
        payout = self._payout('late-order')
        transaction = payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'charge')
        transaction.write({'source_order_id': order.shopify_order_id, 'amount': 242.10})
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            action = payout.action_preview_payout_payments()
            wizard = self.env['shopify.payment.repair.ept'].browse(action['res_id'])
            self.assertEqual(wizard.order_ids, order)
            self.assertEqual(wizard.state, 'preview', wizard.preview_html)
        self.assertFalse(order.shopify_payment_audit_ids)

    def test_cumulative_refund_quantity_cannot_credit_an_item_twice(self):
        order, invoice, payload, events = self._fixture(gross=True)
        events.append(dict(events[1], id='another-refund', amount='25'))
        extra = deepcopy(payload['refunds'][0])
        extra.update(id='another-document', transactions=[dict(id='another-refund')])
        extra['refund_line_items'][0]['subtotal'] = '25'
        payload['refunds'].append(extra)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'blocked')
        self.assertFalse(order.shopify_payment_audit_ids)

    def test_one_legacy_payment_cannot_be_assigned_to_either_identical_charge(self):
        order, invoice, payload, events = self._fixture(gross=True)
        payload['refunds'] = []
        events = [dict(events[0], id='first-charge', amount='121.05'),
                  dict(events[0], id='second-charge', amount='121.05')]
        legacy = self._legacy(invoice, 121.05)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'blocked')
        self.assertFalse(legacy.shopify_order_transaction_id)
        self.assertFalse(order.shopify_payment_audit_ids)

    def test_payout_matching_does_not_tag_ambiguous_legacy_payment(self):
        order, invoice, payload, events = self._fixture(gross=True)
        legacy = self._legacy(invoice, 121.05)
        first, second = self._payout('ambiguous-first'), self._payout('ambiguous-second')
        transactions = (first | second).payout_transaction_ids.filtered(lambda row: row.transaction_type == 'charge')
        transactions.write({'order_id': order.id, 'source_order_id': order.shopify_order_id, 'amount': 121.05})
        transactions[0].source_order_transaction_id = 'first-charge'
        transactions[1].source_order_transaction_id = 'second-charge'
        with self.assertRaises(UserError):
            first.find_payment_for_payout_transaction(transactions[0])
        self.assertFalse(legacy.shopify_order_transaction_id)

    def test_repair_through_two_payouts_and_real_bank_ledger(self):
        """Exercise actual ledger reconciliation; Enterprise widget UI is tested separately."""
        order, invoice, payload, events = self._fixture()
        legacy = self._legacy(invoice, 197.10)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            wizard.action_apply()
        transit = self.outstanding.copy({'name': 'Cash Sync Transit', 'code': 'CSSTRAN'})
        bank = self.journal.copy({
            'name': 'Cash Sync Real Bank', 'code': 'CSBANK',
            'default_account_id': self.journal.default_account_id.copy({'code': 'CSSBANK'}).id,
        })
        (bank.inbound_payment_method_line_ids | bank.outbound_payment_method_line_ids).payment_account_id = transit
        transfer = self.env['account.journal'].create({'name': 'Cash Sync Transfers', 'code': 'CSTR', 'type': 'general'})
        (self.journal | bank).autocheck_on_post = True
        self.instance.write({'shopify_payout_bank_journal_id': bank.id,
                             'shopify_payout_transfer_journal_id': transfer.id,
                             'shopify_payout_transit_account_id': transit.id})
        charge, refund = self._payout('cash-first-day'), self._payout('cash-next-day')
        charge.payout_transaction_ids.unlink()
        refund.payout_transaction_ids.unlink()
        charge.write({'payout_date': invoice.invoice_date, 'amount': 236.35, 'payout_transaction_ids': [
            Command.create({'transaction_id': 'cash-charge', 'source_order_transaction_id': 'charge',
                            'transaction_type': 'charge', 'amount': 242.10, 'fee': 5.75, 'order_id': order.id}),
            Command.create({'transaction_type': 'fees', 'amount': -5.75}),
        ]})
        refund.write({'amount': -45, 'payout_transaction_ids': [Command.create({
            'transaction_id': 'cash-refund', 'source_order_transaction_id': 'refund',
            'transaction_type': 'refund', 'amount': -45, 'order_id': order.id,
        })]})
        for payout in charge | refund:
            for transaction in payout.payout_transaction_ids:
                payment = payout.find_payment_for_payout_transaction(transaction)
                statement = self.env['account.bank.statement.line'].create({
                    'journal_id': self.journal.id, 'date': payout.payout_date,
                    'payment_ref': transaction.transaction_id or transaction.transaction_type,
                    'amount': transaction.amount, 'payout_id': payout.id, 'payout_line_id': transaction.id,
                    'shopify_transaction_type': transaction.transaction_type,
                    'shopify_order_transaction_id': transaction.source_order_transaction_id,
                    'counterpart_account_id': self.outstanding.id if payment else self.instance.transaction_line_ids.account_id.id,
                })
                if payment:
                    (payment._seek_for_lines()[0] | statement.line_ids.filtered(
                        lambda line: line.account_id == self.outstanding)).reconcile()
            payout.validate_statement()
            payout.action_create_settlement_transfer()
            self.assertEqual(payout.settlement_status, 'pending')
            bank_statement = self.env['account.bank.statement.line'].create({
                'journal_id': bank.id, 'date': payout.payout_date, 'payment_ref': payout.payout_reference_id,
                'amount': payout.amount, 'counterpart_account_id': transit.id,
            })
            (payout.settlement_line_id | bank_statement.line_ids.filtered(
                lambda line: line.account_id == transit)).reconcile()
            self.assertEqual(payout.settlement_status, 'matched')
        self.assertEqual(legacy.state, 'canceled')
        self.assertEqual(len(legacy.move_id.reversal_move_ids), 1)
        self.assertTrue(order.currency_id.is_zero(invoice.amount_residual))
        for account in (self.journal.default_account_id, self.outstanding, transit, self.receivable):
            items = self.env['account.move.line'].search([('account_id', '=', account.id), ('parent_state', '=', 'posted')])
            self.assertTrue(order.currency_id.is_zero(sum(items.mapped('balance'))), account.name)
        bank_items = self.env['account.move.line'].search([('account_id', '=', bank.default_account_id.id)])
        self.assertEqual(order.currency_id.round(sum(bank_items.mapped('balance'))), 191.35)

    def test_gross_refund_preserves_invoice_tax(self):
        order, invoice, payload, events = self._fixture(gross=True)
        tax = self.env['account.tax'].create({
            'name': 'Cash Sync 10%', 'amount': 10, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'company_id': order.company_id.id,
        })
        invoice.button_draft()
        invoice.invoice_line_ids.filtered(lambda line: line.name == 'removed').tax_ids = tax
        invoice.action_post()
        events[0]['amount'] = '246.60'
        events[1]['amount'] = '49.50'
        payload['refunds'][0]['refund_line_items'][0]['total_tax'] = '4.50'
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order._sync_shopify_cash()
        credit = order.invoice_ids.filtered(lambda move: move.move_type == 'out_refund')
        self.assertEqual(credit.amount_total, 49.50)
        self.assertEqual(credit.amount_tax, 4.50)
        self.assertTrue(order.currency_id.is_zero(credit.amount_residual + invoice.amount_residual))

    def test_batch_failure_rolls_back_prior_order_and_audit(self):
        first, invoice, payload, events = self._fixture()
        legacy = self._legacy(invoice, 197.10)
        second, invoice2, payload2, events2 = self._fixture()
        second.shopify_order_id = 'second-order'
        for event in events2:
            event['order_id'] = 'second-order'
            event['id'] += '-second'
            if event.get('parent_id'):
                event['parent_id'] += '-second'
        payload2['refunds'][0]['transactions'][0]['id'] = 'refund-second'
        data = {first.id: (payload, events), second.id: (payload2, events2)}
        post = type(self.env['account.payment']).action_post
        def fail_second(payment):
            if payment.shopify_cash_order_id == second:
                raise UserError('Second order posting failure')
            return post(payment)
        with patch.object(type(first), '_shopify_cash_source', lambda order: data[order.id]):
            action = (first | second).action_preview_shopify_payments()
            wizard = self.env['shopify.payment.repair.ept'].browse(action['res_id'])
            self.assertEqual(wizard.state, 'preview', wizard.preview_html)
            with patch.object(type(self.env['account.payment']), 'action_post', fail_second):
                with self.assertRaisesRegex(UserError, 'Second order posting failure'):
                    wizard.action_apply()
        self.assertFalse(legacy.move_id.reversal_move_ids)
        self.assertFalse((first | second).shopify_payment_audit_ids)
        self.assertNotEqual(legacy.state, 'canceled')
        self.assertFalse(self.env['account.payment'].search([('shopify_cash_order_id', 'in', (first | second).ids)]))
