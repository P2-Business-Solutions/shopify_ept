"""Database regressions for sale-line totals and posted invoice preservation."""
import json
from unittest.mock import patch

try:
    from .test_shopify_payout_generation import PayoutTestCase
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase


class TestShopifyOrderUpdateAmountsORM(PayoutTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.income = cls.env['account.account'].create({
            'name': 'Order Update Revenue', 'code': 'SHEDITREV',
            'account_type': 'income', 'company_ids': [(6, 0, cls.env.company.ids)],
        })
        cls.sales_journal = cls.env['account.journal'].create({
            'name': 'Order Update Sales', 'code': 'SHED', 'type': 'sale',
            'company_id': cls.env.company.id,
        })
        cls.product, cls.discount_product, cls.tax_product = cls.env['product.product'].create([
            {'name': name, 'type': 'service', 'invoice_policy': 'order',
             'taxes_id': [(5, 0, 0)], 'property_account_income_id': cls.income.id}
            for name in ('Replacement Item', 'Order Update Discount', 'Order Update Tax')
        ])
        cls.instance.write({
            'tax_product_id': cls.tax_product.id, 'discount_product_id': cls.discount_product.id,
            'add_new_product_order_webhook': True, 'update_qty_order_webhook': True,
        })

    def order(self):
        return self.env['sale.order'].create({
            'partner_id': self.env['res.partner'].create({'name': 'Order Update Customer'}).id,
            'shopify_instance_id': self.instance.id, 'shopify_order_id': '999001234',
        })

    def line(self, order, product, price, **values):
        return self.env['sale.order.line'].create({
            'order_id': order.id, 'product_id': product.id, 'name': product.name,
            'product_uom_qty': 1, 'price_unit': price, 'tax_id': [(5, 0, 0)], **values,
        })

    def test_replacement_replays_with_exact_totals_on_real_sale_lines(self):
        order = self.order()
        original = self.line(order, self.product, 100, shopify_line_id='101')
        discount = self.line(order, self.discount_product, -20, shopify_related_line_id='discount_101')
        self.line(order, self.tax_product, 16)
        payload = {
            'line_items': [
                {'id': 101, 'quantity': 1, 'current_quantity': 0,
                 'discount_allocations': [{'amount': '20'}]},
                {'id': 102, 'quantity': 1, 'current_quantity': 1, 'price': '50',
                 'discount_allocations': [{'amount': '5'}]},
            ],
            'total_discounts': '25', 'current_total_discounts': '5',
            'total_tax': '16', 'current_total_tax': '4',
        }
        queue = self.env['shopify.order.data.queue.ept'].create({
            'shopify_instance_id': self.instance.id, 'queue_type': 'unshipped', 'created_by': 'webhook',
        })
        queued = self.env['shopify.order.data.queue.line.ept'].create({
            'shopify_order_data_queue_id': queue.id, 'shopify_instance_id': self.instance.id,
            'shopify_order_id': order.shopify_order_id, 'order_data': json.dumps(payload), 'name': '#EDIT',
        })
        orders = self.env['sale.order']
        with patch.object(type(orders), 'search_existing_shopify_order', return_value=order), \
                patch.object(type(orders), 'search_custom_tip_gift_card_product', return_value=(False, False, self.product)), \
                patch.object(type(orders), 'check_mismatch_details', return_value=False):
            orders.update_shopify_order(queued, 'webhook', self.instance)
            self.assertEqual(queued.state, 'done')
            self.assertEqual((original.product_uom_qty, discount.product_uom_qty), (0, 0))
            self.assertAlmostEqual(order.amount_total, 49)
            count = len(order.order_line)
            orders.update_shopify_order(queued, 'webhook', self.instance)
            self.assertEqual(queued.state, 'done')
            self.assertAlmostEqual(order.amount_total, 49)
            self.assertEqual(len(order.order_line), count)

    def test_current_discount_total_reconciles_stale_allocations_once(self):
        order = self.order()
        self.line(order, self.product, 100, shopify_line_id='101')
        payload = {'current_total_discounts': '7.01', 'line_items': [
            {'id': 101, 'discount_allocations': [{'amount': '20'}]}]}
        data = order._get_shopify_order_discount_data(payload)
        self.assertTrue(order.update_discount_price_in_order_line_webhook(data, payload))
        self.assertAlmostEqual(order.amount_total, 92.99)
        self.assertFalse(order.update_discount_price_in_order_line_webhook(data, payload))

    def test_tax_delta_preserves_posted_invoice_and_stays_invoiceable(self):
        order = self.order()
        tax_line = self.line(order, self.tax_product, 8)
        order.action_confirm()
        invoice = order.with_context(default_journal_id=self.sales_journal.id)._create_invoices()
        invoice.action_post()
        before = invoice.invoice_line_ids.read(['price_unit', 'quantity', 'balance'])
        self.assertEqual(tax_line.qty_invoiced, 1)
        self.assertTrue(order.update_shopify_tax_line_ept(self.instance, {'current_total_tax': '6'}))
        adjustment = order.order_line - tax_line
        self.assertEqual(adjustment.price_unit, -2)
        self.assertIn(adjustment, order._get_invoiceable_lines(final=True))
        self.assertEqual(tax_line.price_unit, 8)
        self.assertEqual(invoice.invoice_line_ids.read(['price_unit', 'quantity', 'balance']), before)
        self.assertAlmostEqual(order.amount_total, 6)
        self.assertFalse(order.update_shopify_tax_line_ept(self.instance, {'current_total_tax': '6'}))
