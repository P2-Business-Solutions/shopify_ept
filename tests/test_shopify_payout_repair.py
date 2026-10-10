"""Bulk historical refunds, source changes, rollback, and net cash repair."""
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import patch
try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from .test_shopify_cash_sync import TestShopifyCashSync as CashFixtures
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from test_shopify_cash_sync import TestShopifyCashSync as CashFixtures
if ODOO_AVAILABLE:
    from odoo import Command, fields
    from odoo.exceptions import UserError


class TestShopifyPayoutRepair(PayoutTestCase):
    _fixture = CashFixtures._fixture
    _legacy = CashFixtures._legacy

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref('account.group_account_manager')
        cls.instance.shopify_transaction_payment_sync = True
        cls.outstanding, cls.receivable, cls.revenue, cls.adjustment_revenue = cls.env['account.account'].create([
            dict(name=name, code=code, account_type=kind, reconcile=kind != 'income', company_ids=[Command.set(cls.env.company.ids)])
            for name, code, kind in [('Bulk Outstanding', 'BULKOUT', 'asset_current'),
                                     ('Bulk Receivable', 'BULKREC', 'asset_receivable'),
                                     ('Bulk Returns', 'BULKSALE', 'income'),
                                     ('Generic Adjustment Sales', 'BULKADJ', 'income')]
        ])
        (cls.journal.inbound_payment_method_line_ids | cls.journal.outbound_payment_method_line_ids).payment_account_id = cls.outstanding
        cls.partner = cls.env['res.partner'].create(dict(name='Bulk Customer', property_account_receivable_id=cls.receivable.id))
        cls.sales_journal = cls.env['account.journal'].create(dict(name='Bulk Sales', code='BULKS', type='sale', company_id=cls.env.company.id))
        cls.reader_category = cls.env['product.category'].create(dict(name='Bulk Readers',
            property_account_income_categ_id=cls.revenue.id))
        cls.product = cls.env['product.product'].create(dict(name='Bulk Returned Item', type='service',
            categ_id=cls.reader_category.id, property_account_income_id=False))
        cls.adjustment_product = cls.env['product.product'].create(dict(name='Generic Refund Adjustment', type='service',
            property_account_income_id=cls.adjustment_revenue.id))
        cls.gateway = cls.env['shopify.payment.gateway.ept'].create(dict(name='Shopify Payments', code='shopify_payments', shopify_instance_id=cls.instance.id))
        cls.workflow = cls.env['sale.workflow.process.ept'].create(dict(name='Bulk Workflow', journal_id=cls.journal.id, register_payment=True,
            inbound_payment_method_id=cls.journal.inbound_payment_method_line_ids[:1].payment_method_id.id))
        cls.instance.refund_adjustment_product_id = cls.adjustment_product
        cls.env['shopify.res.partner.ept'].create(dict(partner_id=cls.partner.id, shopify_instance_id=cls.instance.id, shopify_customer_id='customer'))
        template = cls.env['shopify.product.template.ept'].create(dict(name='Bulk Template', shopify_instance_id=cls.instance.id))
        cls.env['shopify.product.product.ept'].create(dict(name='Bulk Variant', shopify_instance_id=cls.instance.id,
            shopify_template_id=template.id, product_id=cls.product.id, variant_id='variant'))

    def _historical(self, suffix='one'):
        date = fields.Date.today()
        cutover = date - timedelta(days=10)
        source_id = 'historical-' + suffix
        events = [dict(id='charge-' + suffix, order_id=source_id, currency=self.env.company.currency_id.name,
                       gateway='shopify_payments', status='success', kind='sale', amount='245.70',
                       processed_at=(cutover - timedelta(days=5)).isoformat() + 'T10:00:00-04:00'),
                  dict(id='refund-' + suffix, order_id=source_id, currency=self.env.company.currency_id.name,
                       gateway='shopify_payments', status='success', kind='refund', amount='215.35',
                       parent_id='charge-' + suffix, processed_at=date.isoformat() + 'T10:00:00-04:00')]
        payload = dict(id=source_id, name='#129128', currency=self.env.company.currency_id.name, customer=dict(id='customer'),
                       line_items=[dict(id='item', quantity=1, current_quantity=0, variant_id='variant', title='Returned Item')],
                       fulfillments=[dict(status='success', created_at=events[0]['processed_at'], line_items=[dict(id='item', quantity=1)])],
                       refunds=[dict(id='refund-doc-' + suffix, transactions=[dict(id=events[1]['id'])],
                                     refund_line_items=[dict(line_item_id='item', quantity=1, subtotal='224.10', total_tax='0')],
                                     order_adjustments=[dict(kind='refund_discrepancy', amount='8.75', tax_amount='0')])])
        payout = self.env['shopify.payout.report.ept'].create(dict(instance_id=self.instance.id, payout_reference_id='bulk-' + suffix,
            payout_date=date, currency_id=self.env.company.currency_id.id, amount=-215.35,
            payout_transaction_ids=[Command.create(dict(transaction_type='refund', transaction_id='balance-' + suffix,
                source_order_id=source_id, source_order_transaction_id=events[1]['id'], amount=-215.35))]))
        wizard = self.env['shopify.payout.repair.ept'].create(dict(payout_ids=[Command.set(payout.ids)],
            cutover_date=cutover, timezone='America/New_York', sales_journal_id=self.sales_journal.id, rematch_payouts=False))
        return payout, wizard, payload, events

    def test_historical_refund_posts_without_order_or_original_receipt_and_reuses(self):
        payout, wizard, payload, events = self._historical()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            count = self.env['account.move'].search_count([])
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'ready', wizard.line_ids.detail)
            self.assertIn('224.1', wizard.line_ids.review_html)
            self.assertIn('-8.75', wizard.line_ids.review_html)
            self.assertIn(self.revenue.display_name, wizard.line_ids.review_html)
            self.assertEqual(self.env['account.move'].search_count([]), count)
            wizard.action_apply()
            credit, payment = wizard.line_ids.credit_ids, wizard.line_ids.payment_ids
            self.assertEqual(credit.state, 'posted')
            self.assertEqual(credit.amount_total, 215.35)
            self.assertEqual(credit.invoice_line_ids.mapped('price_unit'), [224.10, -8.75])
            self.assertEqual(credit.invoice_line_ids.account_id, self.revenue)
            self.assertAlmostEqual(sum(credit.line_ids.filtered(lambda line: line.account_id == self.revenue).mapped('balance')), 215.35)
            self.assertFalse(credit.line_ids.filtered(lambda line: line.account_id == self.adjustment_revenue))
            self.assertEqual(credit.amount_residual, 0)
            self.assertEqual(payment.amount, 215.35)
            self.assertEqual(payment.payment_type, 'outbound')
            self.assertFalse(payment._seek_for_lines()[0].reconciled)
            self.assertFalse(self.env['sale.order'].search([('shopify_order_id', '=', payload['id'])]))
            self.assertEqual(len(payout.repair_audit_ids), 1)
            wizard.action_preview()
            wizard.action_apply()
            self.assertEqual(wizard.line_ids.credit_ids, credit)
            self.assertEqual(wizard.line_ids.payment_ids, payment)

    def test_pending_refund_discrepancies_follow_returned_account_and_summary_is_unique(self):
        payout, wizard, payload, events = self._historical()
        payload['refunds'][0]['order_adjustments'].extend([
            dict(kind='refund_discrepancy', amount='215.35', tax_amount='0', reason='Refund discrepancy'),
            dict(kind='refund_discrepancy', amount='-215.35', tax_amount='0', reason='Pending refund discrepancy'),
        ])
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'ready', wizard.line_ids.detail)
            self.assertEqual(wizard.line_ids.detail.count(self.revenue.display_name), 1)
            self.assertNotIn(self.adjustment_revenue.display_name, wizard.line_ids.detail)
            wizard.action_apply()
        credit = wizard.line_ids.credit_ids
        self.assertEqual(len(credit.invoice_line_ids), 4)
        self.assertEqual(credit.invoice_line_ids.account_id, self.revenue)
        self.assertAlmostEqual(sum(credit.line_ids.filtered(lambda line: line.account_id == self.revenue).mapped('balance')), 215.35)

    def test_mixed_refund_accounts_require_explicit_adjustment_allocation(self):
        payout, wizard, payload, events = self._historical()
        template = self.env['shopify.product.template.ept'].create(dict(name='Mixed Refund Template', shopify_instance_id=self.instance.id))
        self.env['shopify.product.product.ept'].create(dict(name='Mixed Refund Variant', shopify_instance_id=self.instance.id,
            shopify_template_id=template.id, product_id=self.adjustment_product.id, variant_id='mixed-variant'))
        payload['line_items'].append(dict(id='second-item', quantity=1, current_quantity=0, variant_id='mixed-variant'))
        payload['fulfillments'][0]['line_items'].append(dict(id='second-item', quantity=1))
        payload['refunds'][0]['refund_line_items'].append(dict(line_item_id='second-item', quantity=1, subtotal='20', total_tax='0'))
        payload['refunds'][0]['order_adjustments'][0]['amount'] = '28.75'
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('multiple revenue accounts', wizard.line_ids.detail)
        self.assertFalse(wizard.line_ids.selected)
        self.assertFalse(wizard.line_ids.credit_ids)

    def test_existing_historical_credit_on_generic_adjustment_account_needs_review(self):
        payout, wizard, payload, events = self._historical()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            credit = self.env['account.move'].create(wizard.line_ids.plan['values'])
            credit.invoice_line_ids.filtered(lambda line: line.price_unit < 0).account_id = self.adjustment_revenue
            credit.action_post()
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('different refund accounts', wizard.line_ids.detail)
        self.assertFalse(wizard.line_ids.payment_ids)
        self.assertTrue(credit.invoice_line_ids.filtered(lambda line: line.account_id == self.adjustment_revenue))
        self.assertEqual(credit.state, 'posted')

    def test_current_order_refund_discrepancy_follows_original_item_account(self):
        order, invoice, payload, events = self._fixture(gross=True, retained=188.35, removed=53.75)
        events[1]['amount'] = '45.00'
        payload['refunds'][0]['order_adjustments'] = [dict(kind='refund_discrepancy', amount='8.75', tax_amount='0')]
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            plan = order._build_shopify_cash_plan(repair=True)
            order._apply_shopify_cash_plan(plan)
        credit = order.shopify_payment_audit_ids.credit_note_ids
        self.assertEqual(credit.amount_total, 45)
        self.assertEqual(credit.invoice_line_ids.account_id, self.revenue)
        self.assertFalse(credit.line_ids.filtered(lambda line: line.account_id == self.adjustment_revenue))
        self.assertAlmostEqual(sum(credit.line_ids.filtered(lambda line: line.account_id == self.revenue).mapped('balance')), 45)

    def test_historical_refund_matches_and_validates_existing_statement(self):
        payout, wizard, payload, events = self._historical()
        payout.generate_bank_statement()
        wizard.rematch_payouts = True
        def ledger_match(model, statement_id, line_ids):
            statement = self.env['account.bank.statement.line'].browse(statement_id)
            lines = self.env['account.move.line'].browse(line_ids)
            self.assertEqual(len(lines.account_id), 1)
            statement._seek_for_lines()[1].write({'account_id': lines.account_id.id})
            (lines | statement.line_ids.filtered(lambda row: row.account_id == lines.account_id)).reconcile()
        matcher = nullcontext() if 'bank.rec.widget' in self.env.registry.models else patch.object(
            type(payout), 'shopify_reconcile_bank_statement_line_ept', ledger_match)
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), matcher:
            wizard.action_preview()
            wizard.action_apply()
        self.assertEqual(payout.state, 'validated')
        self.assertTrue(payout.payout_statement_line_ids.is_reconciled)
        self.assertTrue(wizard.line_ids.payment_ids._seek_for_lines()[0].reconciled)

    def test_draft_option_defers_cash_until_credit_is_posted(self):
        payout, wizard, payload, events = self._historical()
        wizard.post_historical = False
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            wizard.action_apply()
            credit = wizard.line_ids.credit_ids
            self.assertEqual(credit.state, 'draft')
            self.assertFalse(wizard.line_ids.payment_ids)
            credit.action_post()
            wizard.action_preview()
            wizard.action_apply()
            self.assertTrue(wizard.line_ids.payment_ids)

    def test_source_change_and_closed_period_leave_case_unposted(self):
        payout, wizard, payload, events = self._historical()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            payload['note'] = 'changed source'
            wizard.action_apply()
            self.assertEqual(wizard.line_ids.status, 'review')
            self.assertFalse(wizard.line_ids.credit_ids)
        with patch.object(type(payout), '_check_payout_reimport_period', side_effect=UserError('Period closed')):
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')

    def test_failure_rolls_back_credit_and_payment_for_one_case(self):
        payout, wizard, payload, events = self._historical()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            with patch.object(type(self.env['account.payment']), 'action_post', side_effect=UserError('Payment failed')):
                wizard.action_apply()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertFalse(self.env['account.move'].search([('shopify_refund_id', '=', 'refund-doc-one')]))
        self.assertFalse(payout.repair_audit_ids)

    def test_ready_case_runs_alongside_missing_order_exception(self):
        payout, wizard, payload, events = self._historical()
        self.env['shopify.payout.report.line.ept'].create(dict(payout_id=payout.id, transaction_type='charge',
            transaction_id='unidentified', source_order_id='another-order', amount=100))
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            self.assertEqual(len(wizard.line_ids), 2)
            wizard.action_apply()
        self.assertEqual(wizard.line_ids.mapped('status'), ['done', 'review'])

    def test_retroactive_discount_repairs_net_payment_without_extra_credit(self):
        order, invoice, payload, events = self._fixture()
        payload.update(currency=order.currency_id.name, current_total_price='197.10', current_total_tax='0')
        payload['line_items'].append(dict(id='retained', quantity=1, current_quantity=1))
        payload['refunds'][0].update(refund_line_items=[],
            order_adjustments=[dict(kind='refund_discrepancy', amount='-45.00', tax_amount='0')])
        legacy = self._legacy(invoice, 197.10)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            plan = order._build_shopify_cash_plan(repair=True)
            order._apply_shopify_cash_plan(plan)
        self.assertEqual(legacy.state, 'canceled')
        self.assertEqual(invoice.amount_total, 197.10)
        self.assertFalse(order.invoice_ids.filtered(lambda move: move.move_type == 'out_refund'))
        self.assertEqual(sorted(order.shopify_payment_audit_ids.payment_ids.mapped('amount')), [45.0, 242.1])

    def test_wrong_tax_in_net_invoice_remains_for_review(self):
        order, invoice, payload, events = self._fixture()
        payload.update(currency=order.currency_id.name, current_total_price='197.10', current_total_tax='1')
        payload['line_items'].append(dict(id='retained', quantity=1, current_quantity=1))
        payload['refunds'][0].update(order_adjustments=[dict(kind='refund_discrepancy', amount='0', tax_amount='0')])
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaises(UserError):
                order._build_shopify_cash_plan(repair=True)

    def test_order_navigation_resolves_order_imported_after_payout(self):
        order, invoice, payload, events = self._fixture()
        payout = self._payout('navigation')
        transaction = payout.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'charge')
        transaction.source_order_id = order.shopify_order_id
        self.assertEqual(transaction.action_open_order()['res_id'], order.id)
        self.assertIn('/admin/orders/' + order.shopify_order_id, transaction.action_open_shopify_order()['url'])

    def test_existing_unidentified_cash_and_conflicting_credit_are_blocked(self):
        payout, wizard, payload, events = self._historical()
        self.env['account.payment'].create(dict(partner_id=self.partner.id, partner_type='customer', payment_type='outbound',
            journal_id=self.journal.id, payment_method_line_id=self.journal.outbound_payment_method_line_ids[:1].id,
            amount=215.35, currency_id=payout.currency_id.id, date=fields.Date.today()))
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('unidentified refund payment', wizard.line_ids.detail)

    def test_audit_and_settings_cannot_bypass_review(self):
        payout, wizard, payload, events = self._historical()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            wizard.cutover_date += timedelta(days=1)
            with self.assertRaises(UserError):
                wizard.action_apply()
            wizard.action_preview()
            wizard.action_apply()
        audit = payout.repair_audit_ids
        with self.assertRaises(UserError):
            audit.write({'source_order_id': 'forged'})
        with self.assertRaises(UserError):
            audit.unlink()
        with self.assertRaises(UserError):
            self.env['shopify.payout.repair.audit.ept'].create(dict(company_id=self.env.company.id))

    def test_original_invoice_gets_discount_credit_and_gross_cash_pair(self):
        order, invoice, payload, events = self._fixture(gross=True)
        self.instance.refund_adjustment_product_id = self.product
        payload['refunds'][0].update(refund_line_items=[],
            order_adjustments=[dict(kind='refund_discrepancy', amount='-45.00', tax_amount='0')])
        legacy = self._legacy(invoice, 197.10)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            plan = order._build_shopify_cash_plan(repair=True)
            self.assertEqual(plan['mode'], 'gross')
            self.assertEqual(plan['replace_ids'], legacy.ids)
            order._apply_shopify_cash_plan(plan)
        credits = order.shopify_payment_audit_ids.credit_note_ids
        self.assertEqual(credits.amount_total, 45)
        self.assertEqual(invoice.amount_total, 242.10)
        self.assertEqual(invoice.amount_residual, 0)
        self.assertEqual(credits.amount_residual, 0)
        self.assertEqual(legacy.state, 'canceled')
        self.assertEqual(sorted(order.shopify_payment_audit_ids.payment_ids.mapped('amount')), [45.0, 242.1])

    def test_multiple_cash_refunds_across_payouts_create_one_credit_and_shared_audit(self):
        payout, wizard, payload, events = self._historical()
        events[1]['amount'] = '100'
        second = dict(events[1], id='second-refund', amount='115.35')
        events.append(second)
        payload['refunds'][0]['transactions'].append(dict(id='second-refund'))
        payout.payout_transaction_ids.amount = -100
        payout.amount = -100
        second_payout = self.env['shopify.payout.report.ept'].create(dict(instance_id=self.instance.id,
            payout_reference_id='bulk-second', payout_date=payout.payout_date,
            currency_id=payout.currency_id.id, amount=-115.35,
            payout_transaction_ids=[Command.create(dict(transaction_type='refund', transaction_id='second-balance',
                source_order_id=payload['id'], source_order_transaction_id='second-refund', amount=-115.35))]))
        wizard.payout_ids |= second_payout
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            self.assertEqual(len(wizard.line_ids), 1)
            wizard.action_apply()
        self.assertEqual(wizard.line_ids.credit_ids.amount_total, 215.35)
        self.assertEqual(sorted(wizard.line_ids.payment_ids.mapped('amount')), [100, 115.35])
        self.assertEqual(wizard.line_ids.credit_ids.amount_residual, 0)
        self.assertEqual(payout.repair_audit_ids, second_payout.repair_audit_ids)
        self.assertEqual(set(payout.repair_audit_ids.payout_ids.ids), {payout.id, second_payout.id})

    def test_bulk_generation_and_rematching_leave_unreviewed_order_untouched(self):
        payout, wizard, payload, events = self._historical()
        order, invoice, _payload, _events = self._fixture()
        self.env['shopify.payout.report.line.ept'].create(dict(payout_id=payout.id, transaction_type='charge',
            transaction_id='unreviewed-charge', source_order_id=order.shopify_order_id,
            source_order_transaction_id='unreviewed-cash', order_id=order.id, amount=40))
        wizard.rematch_payouts = True
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), \
                patch.object(type(order), '_shopify_cash_source', side_effect=UserError('Invoice mismatch')), \
                patch.object(type(payout), 'check_for_invoice_refund', side_effect=AssertionError('Unreviewed import')) as generation, \
                patch.object(type(payout), 'get_invoices_for_reconcile', side_effect=AssertionError('Unreviewed match')) as matching:
            wizard.action_preview()
            wizard.action_apply()
            generation.assert_not_called()
            matching.assert_not_called()
        self.assertFalse(order.shopify_payment_audit_ids)
        self.assertEqual(invoice.amount_total, 197.10)
        self.assertEqual(payout.state, 'partially_processed')


del CashFixtures
