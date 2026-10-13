"""Database coverage for POS fulfillment stock, deliveries, invoices, and retries."""
import json
from unittest.mock import patch

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
if ODOO_AVAILABLE:
    from odoo import Command, fields
    from odoo.exceptions import UserError


class TestShopifyPosFulfillment(PayoutTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.instance.shopify_warehouse_id
        cls.store = cls.env['stock.warehouse'].create({
            'name': 'POS Store', 'code': 'POST', 'company_id': cls.env.company.id,
        })
        cls.partner = cls.env['res.partner'].create({'name': 'POS Test Customer'})
        cls.revenue = cls.env['account.account'].create({
            'name': 'POS Sales', 'code': 'POSSAL', 'account_type': 'income',
            'company_ids': [Command.set(cls.env.company.ids)],
        })
        cls.sales_journal = cls.env['account.journal'].create({
            'name': 'POS Sales', 'code': 'POSS', 'type': 'sale', 'company_id': cls.env.company.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'POS Stock Item', 'default_code': 'POS-SKU', 'type': 'consu',
            'is_storable': True, 'invoice_policy': 'delivery',
            'property_account_income_id': cls.revenue.id, 'taxes_id': [Command.clear()],
        })
        cls.gateway = cls.env['shopify.payment.gateway.ept'].create({
            'name': 'POS Cash', 'code': 'pos_cash', 'shopify_instance_id': cls.instance.id,
        })
        cls.workflow = cls.env['sale.workflow.process.ept'].create({
            'name': 'POS Automatic', 'validate_order': True, 'create_invoice': True,
            'sale_journal_id': cls.sales_journal.id, 'register_payment': False,
        })
        for status in ('fulfilled', 'partial'):
            cls.env['sale.auto.workflow.configuration.ept'].create({
                'shopify_instance_id': cls.instance.id, 'payment_gateway_id': cls.gateway.id,
                'financial_status': 'paid', 'auto_workflow_id': cls.workflow.id,
                'shopify_order_payment_status': cls.env['import.shopify.order.status'].search([
                    ('status', '=', status)], limit=1).id,
            })
        for location_id, warehouse in (('201', cls.store), ('301', cls.warehouse)):
            cls.env['shopify.location.ept'].create({
                'name': 'POS Location ' + location_id, 'shopify_location_id': location_id,
                'instance_id': cls.instance.id, 'warehouse_for_order': warehouse.id,
            })
        cls.instance.import_order_after_date = fields.Date.to_date('2020-01-01')

    def _fixture(self, quantity=2):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'warehouse_id': self.warehouse.id,
            'shopify_instance_id': self.instance.id, 'shopify_order_id': '10001',
            'shopify_order_number': '10001', 'is_pos_order': True,
            'shopify_order_status': 'fulfilled', 'auto_workflow_process_id': self.workflow.id,
            'shopify_payment_gateway_id': self.gateway.id,
            'order_line': [Command.create({
                'product_id': self.product.id, 'product_uom_qty': quantity,
                'price_unit': 25, 'tax_id': [Command.clear()], 'shopify_line_id': '101',
            })],
        })
        payload = {
            'id': 10001, 'order_number': 10001, 'name': '#10001', 'source_name': 'pos',
            'location_id': 301, 'fulfillment_status': 'fulfilled', 'financial_status': 'paid',
            'created_at': '2026-10-13T12:00:00-04:00', 'payment_gateway_names': ['pos_cash'],
            'line_items': [{'id': 101, 'quantity': quantity, 'fulfillable_quantity': 0,
                            'price': '25.00', 'sku': 'POS-SKU'}],
            'fulfillments': [{'id': 501, 'status': 'success', 'location_id': 201,
                              'line_items': [{'id': 101, 'quantity': quantity, 'sku': 'POS-SKU'}]}],
        }
        for warehouse in (self.warehouse, self.store):
            self.env['stock.quant']._update_available_quantity(self.product, warehouse.lot_stock_id, 10)
        return order, payload

    def _stock(self, warehouse):
        return self.env['stock.quant']._get_available_quantity(self.product, warehouse.lot_stock_id)

    def _queue(self, payload):
        queue = self.env['shopify.order.data.queue.ept'].create({
            'shopify_instance_id': self.instance.id, 'queue_type': 'shipped', 'created_by': 'import',
        })
        return self.env['shopify.order.data.queue.line.ept'].create({
            'shopify_order_data_queue_id': queue.id, 'shopify_instance_id': self.instance.id,
            'shopify_order_id': str(payload['id']), 'order_data': json.dumps(payload),
        })

    def test_completed_fulfillment_creates_delivery_and_posted_invoice_at_actual_location(self):
        order, payload = self._fixture()
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.state, 'sale')
        self.assertEqual(order.warehouse_id, self.store)
        self.assertEqual(len(order.picking_ids), 1)
        picking = order.picking_ids
        self.assertEqual(picking.state, 'done')
        self.assertEqual(picking.location_id, self.store.lot_stock_id)
        self.assertEqual(picking.shopify_fulfillment_id, '501')
        self.assertTrue(picking.updated_in_shopify)
        self.assertEqual(order.order_line.qty_delivered, 2)
        self.assertEqual(order.invoice_ids.state, 'posted')
        self.assertEqual(order.invoice_ids.amount_total, 50)
        self.assertEqual(self._stock(self.store), 8)
        self.assertEqual(self._stock(self.warehouse), 10)

    def test_repeated_import_does_not_duplicate_deliveries_stock_or_invoices(self):
        order, payload = self._fixture()
        order.process_shopify_pos_fulfillments(payload)
        pickings, invoices, moves = order.picking_ids, order.invoice_ids, order.order_line.move_ids
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.picking_ids, pickings)
        self.assertEqual(order.invoice_ids, invoices)
        self.assertEqual(order.order_line.move_ids, moves)
        self.assertEqual(self._stock(self.store), 8)

    def test_partial_fulfillments_split_same_line_between_locations_and_invoice_only_deliveries(self):
        order, payload = self._fixture(quantity=3)
        payload['fulfillment_status'] = 'partial'
        payload['fulfillments'][0]['line_items'][0]['quantity'] = 1
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.order_line.qty_delivered, 1)
        self.assertEqual(order.invoice_ids.amount_total, 25)
        self.assertEqual(self._stock(self.store), 9)
        payload['fulfillment_status'] = 'fulfilled'
        payload['fulfillments'].append({
            'id': 502, 'status': 'success', 'location_id': 301,
            'line_items': [{'id': 101, 'quantity': 2}],
        })
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(len(order.picking_ids), 2)
        self.assertEqual(order.order_line.qty_delivered, 3)
        self.assertEqual(sum(order.invoice_ids.mapped('amount_total')), 75)
        self.assertEqual(self._stock(self.store), 9)
        self.assertEqual(self._stock(self.warehouse), 8)
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(len(order.invoice_ids), 2)

    def test_repeated_sku_is_matched_by_shopify_line_id(self):
        order, payload = self._fixture(quantity=1)
        second_line = self.env['sale.order.line'].create({
            'order_id': order.id, 'product_id': self.product.id, 'product_uom_qty': 1,
            'price_unit': 40, 'tax_id': [Command.clear()], 'shopify_line_id': '102',
        })
        payload['fulfillment_status'] = 'partial'
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.order_line.filtered(lambda line: line.shopify_line_id == '101').qty_delivered, 1)
        self.assertEqual(second_line.qty_delivered, 0)
        self.assertEqual(order.invoice_ids.amount_total, 25)

    def test_failed_cancelled_and_pending_fulfillments_do_not_consume_stock(self):
        order, payload = self._fixture()
        for status in ('failure', 'cancelled', 'pending', 'open'):
            payload['fulfillments'].append({
                'id': status, 'status': status, 'location_id': 301,
                'line_items': [{'id': 101, 'quantity': 2}],
            })
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.picking_ids.shopify_fulfillment_id, '501')
        self.assertEqual(self._stock(self.warehouse), 10)

    def test_missing_mapping_rolls_back_and_can_retry_after_mapping_is_fixed(self):
        order, payload = self._fixture()
        payload['fulfillments'][0]['location_id'] = 999
        with self.assertRaisesRegex(UserError, 'Map Shopify POS fulfillment location'):
            order.process_shopify_pos_fulfillments(payload)
        self.assertFalse(order.picking_ids)
        self.assertFalse(order.invoice_ids)
        self.assertEqual(order.state, 'draft')
        self.assertEqual(self._stock(self.store), 10)
        self.env['shopify.location.ept'].create({
            'name': 'New POS Store', 'shopify_location_id': '999', 'instance_id': self.instance.id,
            'warehouse_for_order': self.store.id,
        })
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.invoice_ids.state, 'posted')

    def test_legacy_completed_move_gains_a_delivery_without_moving_stock_again(self):
        order, payload = self._fixture()
        order.write({'state': 'sale'})
        move = self.env['stock.move'].create({
            'name': 'Legacy POS Move', 'product_id': self.product.id,
            'product_uom_qty': 2, 'product_uom': self.product.uom_id.id,
            'location_id': self.store.lot_stock_id.id,
            'location_dest_id': self.partner.property_stock_customer.id,
            'sale_line_id': order.order_line.id, 'state': 'confirmed',
        })
        move._action_assign()
        move._set_quantity_done(2)
        move.picked = True
        move._action_done()
        self.assertFalse(move.picking_id)
        self.assertEqual(self._stock(self.store), 8)
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(move.picking_id, order.picking_ids)
        self.assertEqual(move.picking_id.state, 'done')
        self.assertEqual(order.order_line.move_ids, move)
        self.assertEqual(self._stock(self.store), 8)
        self.assertEqual(order.invoice_ids.amount_total, 50)

    def test_failed_legacy_sale_with_no_moves_is_repaired(self):
        order, payload = self._fixture()
        order.state = 'sale'
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.picking_ids.state, 'done')
        self.assertEqual(order.invoice_ids.state, 'posted')

    def test_pending_delivery_is_reused_for_partial_fulfillment(self):
        order, payload = self._fixture(quantity=3)
        order.warehouse_id = self.store
        order.action_confirm()
        previous = order.picking_ids
        payload['fulfillment_status'] = 'partial'
        payload['fulfillments'][0]['line_items'][0]['quantity'] = 1
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.order_line.qty_delivered, 1)
        self.assertEqual(previous.move_ids.product_uom_qty, 2)
        self.assertEqual(order.picking_ids.filtered(lambda row: row.state == 'done').move_ids.quantity, 1)
        self.assertEqual(self._stock(self.store), 7)  # two more units are still reserved
        self.assertEqual(sum(self.env['stock.quant'].search([
            ('product_id', '=', self.product.id), ('location_id', '=', self.store.lot_stock_id.id),
        ]).mapped('quantity')), 9)

    def test_invoice_failure_rolls_back_stock_and_delivery_then_retry_succeeds(self):
        order, payload = self._fixture()
        with patch.object(type(order), 'validate_and_paid_invoices_ept', side_effect=UserError('Invoice blocked')):
            with self.assertRaisesRegex(UserError, 'Invoice blocked'):
                order.process_shopify_pos_fulfillments(payload)
        self.assertFalse(order.picking_ids)
        self.assertEqual(self._stock(self.store), 10)
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.invoice_ids.state, 'posted')
        self.assertEqual(self._stock(self.store), 8)

    def test_warehouse_hold_is_respected(self):
        order, payload = self._fixture()
        hold = self.env['sale.order.hold.reason'].create({'name': 'POS Hold'})
        self.store.shopify_order_hold_reason_id = hold
        with self.assertRaisesRegex(UserError, 'on hold'):
            order.process_shopify_pos_fulfillments(payload)
        self.assertFalse(order.picking_ids)
        self.assertEqual(self._stock(self.store), 10)

    def test_new_queue_import_uses_pos_delivery_path(self):
        order, payload = self._fixture()
        queue_line = self._queue(payload)
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(type(order), 'search_existing_shopify_order', return_value=self.env['sale.order']), \
                patch.object(type(order), 'prepare_shopify_customer_and_addresses', return_value=(self.partner,) * 3), \
                patch.object(type(order), 'check_mismatch_details', return_value=False), \
                patch.object(type(order), 'shopify_create_order', return_value=order), \
                patch.object(type(order), 'create_shipped_order_refund', return_value=''):
            self.env['sale.order'].import_shopify_orders(queue_line, self.instance)
        self.assertEqual(queue_line.state, 'done')
        self.assertEqual(queue_line.sale_order_id, order)
        self.assertEqual(order.picking_ids.state, 'done')
        self.assertEqual(order.invoice_ids.state, 'posted')

    def test_existing_queue_order_is_repaired_even_without_shipment_webhooks_enabled(self):
        order, payload = self._fixture()
        self.instance.ship_order_webhook = False
        queue_line = self._queue(payload)
        with patch.object(type(self.instance), 'connect_in_shopify'):
            self.env['sale.order'].import_shopify_orders(queue_line, self.instance)
            self.assertEqual(queue_line.state, 'done')
            self.assertEqual(order.picking_ids.state, 'done')
            self.assertEqual(order.invoice_ids.state, 'posted')
            self.env['sale.order'].import_shopify_orders(queue_line, self.instance)
        self.assertEqual(len(order.picking_ids), 1)
        self.assertEqual(len(order.invoice_ids), 1)
        self.assertEqual(self._stock(self.store), 8)

    def test_unmapped_queue_order_stays_failed_with_payload_for_retry(self):
        order, payload = self._fixture()
        payload['fulfillments'][0]['location_id'] = 999
        queue_line = self._queue(payload)
        with patch.object(type(self.instance), 'connect_in_shopify'):
            self.env['sale.order'].import_shopify_orders(queue_line, self.instance)
        self.assertEqual(queue_line.state, 'failed')
        self.assertTrue(queue_line.order_data)
        self.assertFalse(order.picking_ids)
        self.assertEqual(self._stock(self.store), 10)

    def test_multiwarehouse_pos_import_uses_fulfillments_without_fulfillment_order_api(self):
        order, payload = self._fixture()
        self.instance.is_delivery_multi_warehouse = True
        with patch.object(type(order), 'get_shopify_fulfillment_orders', side_effect=AssertionError('Unexpected API request')):
            order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.picking_ids.location_id, self.store.lot_stock_id)

    def test_kit_delivery_consumes_component_quantities_and_invoices_kit(self):
        if 'mrp.bom' not in self.env:
            self.skipTest('MRP is not installed')
        order, payload = self._fixture()
        kit = self.env['product.product'].create({
            'name': 'POS Kit', 'type': 'consu', 'invoice_policy': 'delivery',
            'property_account_income_id': self.revenue.id, 'taxes_id': [Command.clear()],
        })
        self.env['mrp.bom'].create({
            'product_tmpl_id': kit.product_tmpl_id.id, 'product_qty': 1, 'type': 'phantom',
            'bom_line_ids': [Command.create({
                'product_id': self.product.id, 'product_qty': 3, 'product_uom_id': self.product.uom_id.id,
            })],
        })
        order.order_line.write({'product_id': kit.id, 'price_unit': 25})
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.picking_ids.move_ids.product_id, self.product)
        self.assertEqual(order.picking_ids.move_ids.quantity, 6)
        self.assertEqual(order.order_line.qty_delivered, 2)
        self.assertEqual(order.invoice_ids.amount_total, 50)
        self.assertEqual(self._stock(self.store), 4)

    def test_tracked_stock_requires_lots_and_failed_import_moves_no_stock(self):
        order, payload = self._fixture()
        self.product.tracking = 'lot'
        with self.assertRaises(UserError):
            order.process_shopify_pos_fulfillments(payload)
        self.assertFalse(order.picking_ids)
        self.assertFalse(order.invoice_ids)
        self.assertEqual(self._stock(self.store), 10)

    def test_tracked_stock_uses_reserved_lots(self):
        order, payload = self._fixture()
        self.product.tracking = 'lot'
        self.env['stock.quant']._update_available_quantity(self.product, self.store.lot_stock_id, -10)
        lot = self.env['stock.lot'].create({
            'name': 'POS-LOT', 'product_id': self.product.id, 'company_id': self.env.company.id,
        })
        self.env['stock.quant']._update_available_quantity(self.product, self.store.lot_stock_id, 10, lot_id=lot)
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(order.picking_ids.move_line_ids.lot_id, lot)
        self.assertEqual(order.picking_ids.state, 'done')
        self.assertEqual(order.invoice_ids.amount_total, 50)

    def test_partial_pos_queue_does_not_require_fulfillment_order_api(self):
        _order, payload = self._fixture()
        payload['fulfillment_status'] = 'partial'
        self.instance.is_delivery_multi_warehouse = True
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(type(self.env['sale.order']), 'get_shopify_fulfillment_orders', side_effect=AssertionError('Unexpected API request')), \
                patch.object(self.env.cr, 'commit'), \
                patch.object(type(self.env['shopify.order.data.queue.line.ept']), 'create_order_queue_line') as create_line:
            self.env['shopify.order.data.queue.line.ept'].create_order_data_queue_line(
                [payload], self.instance, 'unshipped')
        self.assertEqual(create_line.call_args.args[2]['fulfillment_data'], [])

    def test_legacy_shipped_stock_move_works_without_active_queue_ids(self):
        order, payload = self._fixture()
        order.is_pos_order = False
        order.with_context(shopify_order_response=payload, active_ids=[]).create_and_done_stock_move_ept(
            order.order_line, self.partner.property_stock_customer)
        self.assertEqual(order.order_line.move_ids.state, 'done')
        self.assertEqual(order.order_line.qty_delivered, 2)

    def test_legacy_shipped_stock_move_without_source_payload_does_not_crash(self):
        order, _payload = self._fixture()
        order.is_pos_order = False
        order.with_context(active_ids=[], shopify_order_response=False, order_data_line=False).create_and_done_stock_move_ept(
            order.order_line, self.partner.property_stock_customer)
        self.assertEqual(order.order_line.move_ids.state, 'done')

    def test_kit_with_repeated_component_lines_consumes_each_component_quantity(self):
        if 'mrp.bom' not in self.env:
            self.skipTest('MRP is not installed')
        order, payload = self._fixture()
        kit = self.env['product.product'].create({
            'name': 'Repeated Component POS Kit', 'type': 'consu', 'invoice_policy': 'delivery',
            'property_account_income_id': self.revenue.id, 'taxes_id': [Command.clear()],
        })
        self.env['mrp.bom'].create({
            'product_tmpl_id': kit.product_tmpl_id.id, 'product_qty': 1, 'type': 'phantom',
            'bom_line_ids': [Command.create({
                'product_id': self.product.id, 'product_qty': quantity, 'product_uom_id': self.product.uom_id.id,
            }) for quantity in (2, 3, 0)],
        })
        order.order_line.write({'product_id': kit.id, 'price_unit': 25})
        order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(sum(order.picking_ids.move_ids.mapped('quantity')), 10)
        self.assertEqual(len(order.picking_ids.move_ids), 2)
        self.assertEqual(order.order_line.qty_delivered, 2)
        self.assertEqual(order.invoice_ids.amount_total, 50)
        self.assertEqual(self._stock(self.store), 0)

    def test_overfulfilled_order_rolls_back_deliveries_and_stock(self):
        order, payload = self._fixture()
        payload['fulfillments'].append({
            'id': 502, 'status': 'success', 'location_id': 201,
            'line_items': [{'id': 101, 'quantity': 1}],
        })
        with self.assertRaisesRegex(UserError, 'exceeds the undelivered quantity'):
            order.process_shopify_pos_fulfillments(payload)
        self.assertFalse(order.picking_ids)
        self.assertFalse(order.invoice_ids)
        self.assertEqual(self._stock(self.store), 10)

    def test_existing_move_in_another_location_requires_review_without_double_consumption(self):
        order, payload = self._fixture()
        order.action_confirm()
        with self.assertRaisesRegex(UserError, 'uses another location'):
            order.process_shopify_pos_fulfillments(payload)
        self.assertEqual(len(order.picking_ids), 1)
        self.assertNotEqual(order.picking_ids.state, 'done')
        self.assertEqual(sum(self.env['stock.quant'].search([
            ('product_id', '=', self.product.id), ('location_id', '=', self.warehouse.lot_stock_id.id),
        ]).mapped('quantity')), 10)
        self.assertEqual(self._stock(self.store), 10)
