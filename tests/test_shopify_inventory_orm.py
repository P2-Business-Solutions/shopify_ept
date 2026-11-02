"""Database verification of stock roots, reservations, kits and Shopify mappings."""
import unittest
from datetime import timedelta
import json
from unittest.mock import patch
try:
    from odoo import Command, fields
    from odoo.exceptions import UserError
    from odoo.tests import TransactionCase, tagged
except ModuleNotFoundError:
    TransactionCase = unittest.TestCase
    ODOO_AVAILABLE = False
    def tagged(*args):
        return lambda cls: cls
else:
    ODOO_AVAILABLE = True
    from .. import shopify


@tagged('post_install', '-at_install')
@unittest.skipUnless(ODOO_AVAILABLE, 'Odoo test runtime is not available')
class TestShopifyInventoryOrm(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.env.company.id)], limit=1)
        cls.other = cls.env['stock.warehouse'].create({'name': 'Inventory Event', 'code': 'IVE'})
        cls.unmapped_warehouse = cls.env['stock.warehouse'].create({'name': 'Inventory Excluded', 'code': 'IVX'})
        cls.bin = cls.env['stock.location'].create({'name': 'Inventory Bin', 'usage': 'internal',
            'location_id': cls.warehouse.lot_stock_id.id})
        cls.instance = cls.env['shopify.instance.ept'].create({
            'name': 'Inventory Test', 'shopify_company_id': cls.env.company.id,
            'shopify_warehouse_id': cls.warehouse.id, 'shopify_host': 'inventory-test.myshopify.com',
            'shopify_api_key': 'test-key', 'shopify_password': 'test-token',
            'shopify_shared_secret': 'test-secret',
            'shopify_stock_field': cls.env['ir.model.fields']._get('product.product', 'free_qty').id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Inventory Item', 'default_code': 'INV-SKU', 'type': 'consu', 'is_storable': True,
        })
        cls.template = cls.env['shopify.product.template.ept'].create({
            'name': 'Inventory Item', 'shopify_instance_id': cls.instance.id,
            'product_tmpl_id': cls.product.product_tmpl_id.id, 'shopify_tmpl_id': '100',
        })
        cls.binding = cls.env['shopify.product.product.ept'].create({
            'name': 'Inventory Item', 'default_code': 'INV-SKU', 'shopify_instance_id': cls.instance.id,
            'shopify_template_id': cls.template.id, 'product_id': cls.product.id,
            'inventory_item_id': '123', 'variant_id': '789', 'exported_in_shopify': True,
        })
        cls.locations = cls.env['shopify.location.ept'].create([
            {'name': name, 'shopify_location_id': location_id, 'instance_id': cls.instance.id,
             'shopify_instance_company_id': cls.env.company.id,
             'export_stock_warehouse_ids': [Command.set(warehouses.ids)]}
            for name, location_id, warehouses in (
                ('Main', '456', cls.warehouse), ('Event', '457', cls.other),
                ('Unmanaged', '458', cls.env['stock.warehouse']),
            )])
        quants = cls.env['stock.quant']
        quants._update_available_quantity(cls.product, cls.warehouse.lot_stock_id, 11)
        quants._update_available_quantity(cls.product, cls.bin, 7)
        quants._update_reserved_quantity(cls.product, cls.warehouse.lot_stock_id, 3)
        quants._update_available_quantity(cls.product, cls.other.lot_stock_id, 29)
        quants._update_available_quantity(cls.product, cls.unmapped_warehouse.lot_stock_id, 43)

    def _line(self, location='456', quantity=999, state='draft'):
        queue = self.env['shopify.export.stock.queue.ept'].create({'shopify_instance_id': self.instance.id})
        return self.env['shopify.export.stock.queue.line.ept'].create({
            'export_stock_queue_id': queue.id, 'shopify_instance_id': self.instance.id,
            'shopify_product_id': self.binding.id, 'inventory_item_id': '123',
            'location_id': location, 'quantity': quantity, 'state': state,
        })

    def _stock(self, warehouse=None):
        return self.binding.check_stock(self.instance, self.product.ids,
            self.env['product.product'], warehouse or self.warehouse)[self.product.id]

    def test_stock_includes_root_and_bins_and_subtracts_reservations(self):
        self.assertEqual(self._stock(), 15)
        self.assertEqual(self._stock(self.other), 29)
        self.assertEqual(self._stock(self.warehouse | self.other), 44)
        # Keep parity with the current shared connector's native stock contexts.
        old = self.env['product.product'].get_free_qty_ept(self.warehouse, self.product.ids)
        self.assertEqual(old[self.product.id], 15)

    def test_onhand_and_forecast_respect_selected_stock_field(self):
        for name in ('qty_available', 'virtual_available'):
            self.instance.shopify_stock_field = self.env['ir.model.fields']._get('product.product', name)
            self.assertEqual(self._stock(), 18)
            self.assertEqual(self._stock(self.other), 29)

    def test_all_stock_fields_use_their_native_math_at_each_location(self):
        self.env['stock.move'].create([
            {'name': name, 'product_id': self.product.id, 'product_uom': self.product.uom_id.id,
             'product_uom_qty': quantity, 'state': 'confirmed', 'location_id': source.id,
             'location_dest_id': destination.id, 'company_id': self.env.company.id}
            for name, quantity, source, destination in (
                ('Inventory Incoming', 5, self.env.ref('stock.stock_location_suppliers'), self.warehouse.lot_stock_id),
                ('Inventory Outgoing', 7, self.warehouse.lot_stock_id, self.env.ref('stock.stock_location_customers')),
            )])
        for field, main, event in (('free_qty', 15, 29), ('qty_available', 18, 29), ('virtual_available', 16, 29)):
            self.instance.shopify_stock_field = self.env['ir.model.fields']._get('product.product', field)
            self.assertEqual(self._stock(), main)
            self.assertEqual(self._stock(self.other), event)
            lines = self._line() | self._line('457')
            lines._refresh_inventory_lines(self.instance, lines)
            self.assertEqual({line.location_id: line.quantity for line in lines}, {'456': main, '457': event})

    def test_pending_forecast_moves_and_quant_changes_are_selected(self):
        self.env['stock.move'].create({'name': 'Forecast Pending', 'product_id': self.product.id,
            'product_uom': self.product.uom_id.id, 'product_uom_qty': 5, 'state': 'confirmed',
            'location_id': self.warehouse.lot_stock_id.id,
            'location_dest_id': self.env.ref('stock.stock_location_customers').id,
            'company_id': self.env.company.id})
        since = fields.Datetime.now() - timedelta(minutes=1)
        cutoff = fields.Datetime.now() + timedelta(seconds=1)
        changed = self.binding._inventory_changed_product_ids(self.instance, since, cutoff)
        self.assertIn(self.product.id, changed)
        legacy = self.env['product.product'].get_products_based_on_movement_date_ept(since, self.env.company)
        self.assertNotIn(self.product.id, legacy)

    def test_policy_and_mapping_changes_require_a_full_refresh(self):
        self.instance.shopify_last_date_update_stock = fields.Datetime.now()
        self.instance.shopify_stock_field = self.env['ir.model.fields']._get('product.product', 'qty_available')
        self.assertFalse(self.instance.shopify_last_date_update_stock)
        self.instance.shopify_last_date_update_stock = fields.Datetime.now()
        self.locations[:1].export_stock_warehouse_ids = False
        self.assertFalse(self.instance.shopify_last_date_update_stock)

    def test_full_export_flow_for_all_stock_fields_and_locations(self):
        remote = {'456': 0, '457': 0}
        submitted = []
        expected = {}
        test = self
        def execute(client, query, variables=None, operation_name=None, timeout=None):
            test.assertEqual(client.headers['X-Shopify-Access-Token'], 'test-token')
            if operation_name == 'InventoryLevels':
                location = variables['locationId'].rsplit('/', 1)[-1]
                test.assertIn(location, expected)
                data = {'location': {'id': variables['locationId'], 'isActive': True}, 'nodes': [{
                    'id': 'gid://shopify/InventoryItem/123', 'tracked': True,
                    'variant': {'id': 'gid://shopify/ProductVariant/789'},
                    'inventoryLevel': {'location': {'id': variables['locationId']},
                        'quantities': [{'name': 'available', 'quantity': remote[location]}]},
                }]}
            else:
                test.assertEqual(operation_name, 'InventorySet')
                for change in variables['input']['quantities']:
                    location = change['locationId'].rsplit('/', 1)[-1]
                    test.assertEqual(change['inventoryItemId'], 'gid://shopify/InventoryItem/123')
                    test.assertEqual(change['quantity'], expected[location])
                    test.assertEqual(change['changeFromQuantity'], remote[location])
                    remote[location] = change['quantity']
                    submitted.append((location, change['quantity']))
                data = {'inventorySetQuantities': {'inventoryAdjustmentGroup': {'createdAt': 'now'}, 'userErrors': []}}
            return json.dumps({'data': data})

        for field, quantity in (('free_qty', 15), ('qty_available', 18), ('virtual_available', 18)):
            expected = {'456': quantity, '457': 29}
            self.instance.shopify_stock_field = self.env['ir.model.fields']._get('product.product', field)
            queues = self.binding.export_stock_queue(self.instance, self.product.ids)
            lines = queues.export_stock_queue_line_ids
            lines.write({'quantity': 999, 'inventory_item_id': '999'})
            with patch.object(shopify.GraphQL, 'execute', execute), patch.object(self.env.cr, 'commit', lambda: None):
                lines.process_export_stock_queue_data()
            self.assertEqual(remote, expected)
            self.assertEqual(queues.state, 'completed')
            self.assertEqual(set(lines.mapped('state')), {'done'})
            states = self.env['shopify.inventory.export.state.ept'].search([('shopify_instance_id', '=', self.instance.id)])
            self.assertEqual({state.location_id: state.quantity for state in states}, expected)
        self.assertNotIn('458', remote)
        self.assertTrue(submitted)

    def test_unknown_stock_field_cannot_zero_the_catalog(self):
        self.instance.shopify_stock_field = False
        with self.assertRaises(UserError):
            self._stock()

    def test_export_uses_each_location_warehouse_and_skips_unmanaged_locations(self):
        before = fields.Datetime.now()
        queues = self.binding.export_stock_queue(self.instance, self.product.ids)
        lines = queues.export_stock_queue_line_ids
        self.assertEqual({line.location_id: line.quantity for line in lines}, {'456': 15, '457': 29})
        self.assertGreaterEqual(self.instance.shopify_last_date_update_stock, before)

    def test_retry_refreshes_stale_quantity_from_current_mapped_warehouse(self):
        lines = self._line() | self._line('457')
        self.assertEqual(lines._refresh_inventory_lines(self.instance, lines), lines)
        self.assertEqual({line.location_id: line.quantity for line in lines}, {'456': 15, '457': 29})
        self.env['stock.quant']._update_available_quantity(self.product, self.warehouse.lot_stock_id, 5)
        lines._refresh_inventory_lines(self.instance, lines)
        self.assertEqual({line.location_id: line.quantity for line in lines}, {'456': 20, '457': 29})

    def test_removed_location_mapping_is_failed_without_sending_zero(self):
        line = self._line()
        self.locations[:1].export_stock_warehouse_ids = False
        self.assertFalse(line._refresh_inventory_lines(self.instance, line))
        self.assertEqual(line.state, 'failed')
        self.assertEqual(line.quantity, 999)

    def test_batch_location_updates_validate_each_location(self):
        self.locations.write({'export_stock_warehouse_ids': [Command.clear()]})
        self.assertFalse(self.locations.export_stock_warehouse_ids)

    def test_duplicate_inventory_item_binding_is_blocked(self):
        self.binding.copy({'variant_id': '790'})
        line = self._line()
        self.assertFalse(line._refresh_inventory_lines(self.instance, line))
        self.assertEqual(line.state, 'failed')

    def test_failed_rows_coalesce_and_older_duplicate_rows_are_cancelled(self):
        old = self._line(state='failed')
        newest = self._line()
        newest.export_stock_queue_id.is_action_require = True
        queues = self.env['shopify.export.stock.queue.ept'].create_export_stock_queue(self.instance, [{
            'shopify_product_id': self.binding, 'inventory_item_id': '123', 'location_id': '456', 'quantity': 15,
        }])
        self.assertEqual(queues, newest.export_stock_queue_id)
        self.assertEqual(newest.quantity, 15)
        self.assertEqual(newest.state, 'draft')
        self.assertFalse(queues.is_action_require)
        self.assertEqual(old.state, 'cancel')

    def test_last_export_audit_does_not_suppress_live_verification(self):
        self.env['shopify.inventory.export.state.ept'].create({
            'shopify_instance_id': self.instance.id, 'shopify_product_id': self.binding.id,
            'inventory_item_id': '123', 'location_id': '456', 'quantity': 15,
        })
        queues = self.env['shopify.export.stock.queue.ept'].create_export_stock_queue(self.instance, [{
            'shopify_product_id': self.binding, 'inventory_item_id': '123', 'location_id': '456', 'quantity': 15,
        }])
        self.assertEqual(len(queues.export_stock_queue_line_ids), 1)

    def test_fixed_caps_and_percentage_apply_per_location(self):
        self.binding.write({'fix_stock_type': 'percentage', 'fix_stock_value': 50})
        lines = self._line() | self._line('457')
        lines._refresh_inventory_lines(self.instance, lines)
        self.assertEqual({line.location_id: line.quantity for line in lines}, {'456': 7, '457': 14})
        self.binding.write({'fix_stock_type': 'fix', 'fix_stock_value': 10})
        lines._refresh_inventory_lines(self.instance, lines)
        self.assertEqual(lines.mapped('quantity'), [10, 10])
        self.binding.write({'fixed_stock_export': True, 'fixed_stock_export_value': 4})
        lines._refresh_inventory_lines(self.instance, lines)
        self.assertEqual(lines.mapped('quantity'), [4, 4])

    def test_phantom_kit_uses_components_in_each_mapped_warehouse(self):
        if 'mrp.bom' not in self.env:
            self.skipTest('Manufacturing is not installed')
        component = self.env['product.product'].create({'name': 'Kit Component', 'type': 'consu', 'is_storable': True})
        kit = self.env['product.product'].create({'name': 'Inventory Kit', 'type': 'consu', 'is_storable': True})
        self.env['mrp.bom'].create({'product_tmpl_id': kit.product_tmpl_id.id, 'type': 'phantom',
            'bom_line_ids': [Command.create({'product_id': component.id, 'product_qty': 2})]})
        self.env['stock.quant']._update_available_quantity(component, self.warehouse.lot_stock_id, 20)
        self.env['stock.quant']._update_available_quantity(component, self.other.lot_stock_id, 6)
        main = self.binding.check_stock(self.instance, kit.ids, self.env['product.product'], self.warehouse)
        event = self.binding.check_stock(self.instance, kit.ids, self.env['product.product'], self.other)
        self.assertEqual(main[kit.id], 10)
        self.assertEqual(event[kit.id], 3)
