"""Exercise the real order-update methods without requiring an Odoo database."""
import ast
from datetime import datetime
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('order_update_utils', ROOT / 'models/shopify_order_utils.py')
UTILS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UTILS)


def load_methods():
    tree = ast.parse((ROOT / 'models/sale_order.py').read_text())
    model = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    names = {
        'get_price_based_on_customer_visible_currency', '_get_shopify_order_amount',
        '_get_shopify_order_tax_amount', 'create_shopify_tax_line', 'update_shopify_tax_line_ept',
        '_get_shopify_order_discount_data', '_get_shopify_discount_allocation_amount',
        'update_discount_price_in_order_line_webhook', '_get_discount_code_for_line',
        'prepare_response_data_of_order_qty', 'prepare_existing_order_data_of_qty',
        'update_qty_in_order_webhook_ept', 'prepare_data_for_not_exist_product_in_order',
        'add_new_product_in_order_webhook_ept', 'webhook_create_shopify_order_lines',
        'update_shopify_order',
    }
    methods = [node for node in model.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for method in methods:
        method.decorator_list = []
    namespace = {
        '_': lambda message: message, '_logger': logging.getLogger(__name__),
        'datetime': datetime, 'json': json,
        'float_is_zero': lambda amount, precision_digits: round(amount, precision_digits) == 0,
        'float_compare': lambda a, b, precision_digits: round(a - b, precision_digits),
        **{name: getattr(UTILS, name) for name in (
            'filter_importable_order_lines', 'is_removed_order_line', 'is_cash_only_shopify_order')},
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
                 'sale_order.py', 'exec'), namespace)
    return {name: namespace[name] for name in names}


class Records(list):
    def __getitem__(self, key):
        value = super().__getitem__(key)
        return Records(value) if isinstance(key, slice) else value

    def __sub__(self, other):
        return Records(row for row in self if row not in other)

    def __getattr__(self, key):
        if len(self) != 1:
            raise AssertionError('Expected one record for %s' % key)
        return getattr(self[0], key)

    def filtered(self, predicate):
        return Records(row for row in self if (getattr(row, predicate) if isinstance(predicate, str) else predicate(row)))

    def mapped(self, key):
        return [getattr(row, key) for row in self]

    def write(self, values):
        for row in self:
            row.write(values)
        return True


class Line:
    def __init__(self, product, price=0, quantity=1, remote_id=False, related_id=False, invoiced=0):
        self.id = id(self)
        self.product_id = product
        self.price_unit, self.product_uom_qty = price, quantity
        self.shopify_line_id, self.shopify_related_line_id = remote_id, related_id
        self.qty_invoiced, self.qty_delivered = invoiced, 0
        self.shopify_discount_code = False
        self.discount, self.tax_id = 0, []

    @property
    def price_subtotal(self):
        return round(self.price_unit * self.product_uom_qty * (1 - self.discount / 100), 2)

    def write(self, values):
        for key, value in values.items():
            setattr(self, key, value)

    def with_context(self, **_kwargs):
        return self

    def _compute_amount(self):
        pass


class Environment(dict):
    def __init__(self):
        super().__init__()
        self.context = {}


class Order:
    locals().update(load_methods())

    def __init__(self, workflow=False):
        self.id, self.name, self.state = 1, 'SO-EDIT', 'sale'
        self.order_line = Records()
        self.product = SimpleNamespace(name='Item', default_code='ITEM', invoice_policy='order', type='service', is_storable=False)
        self.tax_product = SimpleNamespace(name='Shopify tax', invoice_policy='order', type='service', is_storable=False)
        self.discount_product = SimpleNamespace(name='Discount', invoice_policy='order', type='service', is_storable=False)
        self.shopify_instance_id = SimpleNamespace(
            id=1, order_visible_currency=False, tax_product_id=self.tax_product,
            discount_product_id=self.discount_product, customer_order_webhook=False,
            is_delivery_multi_warehouse=False, add_new_product_order_webhook=True,
            update_qty_order_webhook=True, update_qty_to_invoice_order_webhook=True,
            ship_order_webhook=False, refund_order_webhook=False,
        )
        self.currency_id = SimpleNamespace(
            is_zero=lambda amount: round(amount, 2) == 0,
            compare_amounts=lambda a, b: round(a - b, 2))
        self.auto_workflow_process_id = workflow
        self.env = Environment()
        self.env['common.log.lines.ept'] = Mock()
        self.env['sale.order.line'] = SimpleNamespace(create=self.create_line, search=self.search_line)
        for name in ('_sync_shopify_discount_data', '_shopify_apply_external_order_adjustments',
                     '_apply_discount_code_analytic_account', '_shopify_resolve_shipping_lines',
                     'update_shopify_shipping_lines_ept'):
            setattr(self, name, Mock())
        for name in ('_shopify_external_order_error', '_shopify_record_external_order_error',
                     '_shopify_skip_exact_tax_line', '_get_discount_code_config', 'check_mismatch_details'):
            setattr(self, name, Mock(return_value=False))
        self.invoice_snapshots = []

    def ensure_one(self):
        pass

    def with_context(self, **kwargs):
        self.env.context.update(kwargs)
        return self

    def mapped(self, key):
        return getattr(self, key)

    def write(self, values):
        for _operation, line_id, updates in values['order_line']:
            next(line for line in self.order_line if line.id == line_id).write(updates)

    def search_existing_shopify_order(self, *_args):
        return self

    def search_line(self, domain, **_kwargs):
        remote_id = str(next(value for field, _op, value in domain if field == 'shopify_line_id'))
        return self.order_line.filtered(lambda line: line.shopify_line_id == remote_id)[:1]

    def create_line(self, values):
        line = Line(values['product_id'], values['price_unit'], values['product_uom_qty'],
                    values.get('shopify_line_id', False))
        self.order_line.append(line)
        return line

    def prepare_vals_for_sale_order_line(self, product, _name, price, quantity):
        return dict(product_id=product, price_unit=price, product_uom_qty=quantity)

    def search_custom_tip_gift_card_product(self, *_args):
        return False, False, self.product

    def shopify_create_sale_order_line(self, source, product, quantity, _name, price, _payload, **_kwargs):
        return self.create_line(dict(product_id=product, price_unit=float(price),
                                     product_uom_qty=quantity, shopify_line_id=str(source['id'])))

    def _create_discount_or_gift_card_line(self, _instance, _config, code, _name, amount, _payload, previous_line):
        line = Line(self.discount_product, -amount, related_id=(
            'discount_%s' % previous_line.shopify_line_id if previous_line else False))
        line.shopify_discount_code = code
        self.order_line.append(line)
        return line

    def webhook_call_auto_invoice_workflow(self, _workflow):
        self.invoice_snapshots.append((self.discount_total(), self.tax_total(),
                                       sum(line.price_subtotal for line in self.order_line)))

    def discount_total(self):
        return -sum(line.price_subtotal for line in self.order_line
                    if (line.shopify_related_line_id or '').startswith('discount_'))

    def tax_total(self):
        return sum(line.price_subtotal for line in self.order_line if line.product_id == self.tax_product)

    def sync_discount(self, payload):
        return self.update_discount_price_in_order_line_webhook(self._get_shopify_order_discount_data(payload), payload)


def source_line(identity=101, discount=0, **changes):
    return dict(id=identity, price='100', quantity=1, current_quantity=1, fulfillable_quantity=1,
                discount_allocations=[dict(amount=str(discount))] if discount else [], **changes)


def queue(payload, instance):
    result = SimpleNamespace(id=1, order_data=json.dumps(payload), shopify_instance_id=instance,
                             state='draft', name='Order edit')
    result.write = lambda values: result.__dict__.update(values)
    return result


class TestShopifyOrderUpdateAmounts(unittest.TestCase):
    def setUp(self):
        self.order = Order()
        self.item = Line(self.order.product, 100, remote_id='101')
        self.order.order_line.append(self.item)
        self.instance = self.order.shopify_instance_id

    def discount(self, amount, **kwargs):
        line = Line(self.order.discount_product, -amount, related_id='discount_101', **kwargs)
        self.order.order_line.append(line)
        return line

    def test_removed_item_clears_historical_allocations_and_free_product_code(self):
        old = self.discount(100)
        self.item.shopify_discount_code = 'FREE'
        payload = {'line_items': [dict(source_line(discount=100), current_quantity=0)],
                   'current_total_discounts': '0'}
        self.assertTrue(self.order.sync_discount(payload))
        self.assertEqual(old.product_uom_qty, 0)
        self.assertFalse(self.item.shopify_discount_code)
        self.assertEqual(self.order.discount_total(), 0)
        self.assertFalse(self.order.sync_discount(payload))

    def test_decrease_increase_and_clear_without_quantity_changes(self):
        self.discount(20)
        for amount in (10, 30, 0, 5):
            with self.subTest(amount=amount):
                payload = {'line_items': [source_line(discount=amount)]}
                self.assertTrue(self.order.sync_discount(payload))
                self.assertEqual(self.order.discount_total(), amount)
                count = len(self.order.order_line)
                self.assertFalse(self.order.sync_discount(payload))
                self.assertEqual(len(self.order.order_line), count)

    def test_duplicate_discount_lines_and_quantities_are_aggregated(self):
        self.discount(10, quantity=2)
        self.discount(5)
        payload = {'line_items': [source_line(discount=12)]}
        self.order.sync_discount(payload)
        self.assertEqual(self.order.discount_total(), 12)
        self.assertEqual(len([line for line in self.order.order_line
                             if line.shopify_related_line_id and line.product_uom_qty]), 1)

    def test_current_total_reconciles_historical_allocations_and_rounding(self):
        for amount in ('7.00', '7.01', '0.00'):
            payload = {'line_items': [source_line(discount=20)], 'current_total_discounts': amount}
            self.order.sync_discount(payload)
            self.assertEqual(self.order.discount_total(), float(amount))
            self.assertFalse(self.order.sync_discount(payload))

    def test_partial_payload_preserves_missing_allocations_and_tax(self):
        self.discount(15)
        self.order.order_line.append(Line(self.order.tax_product, 8))
        self.assertFalse(self.order.sync_discount({'line_items': [{'id': 101}]}))
        self.assertFalse(self.order.update_shopify_tax_line_ept(self.instance, {}))
        self.assertEqual((self.order.discount_total(), self.order.tax_total()), (15, 8))

    def test_explicit_zero_total_clears_discount_with_omitted_old_item(self):
        self.discount(15)
        self.order.sync_discount({'current_total_discounts': 0})
        self.assertEqual(self.order.discount_total(), 0)

    def test_shipping_discount_changes_are_included(self):
        self.item.shopify_line_id = '501'
        self.order.sync_discount({'shipping_lines': [source_line(identity=501, discount=5)]})
        self.assertEqual(self.order.discount_total(), 5)
        self.order.sync_discount({'shipping_lines': [dict(source_line(identity=501, discount=5), is_removed=True)]})
        self.assertEqual(self.order.discount_total(), 0)

    def test_discount_currency_fallback_and_zero_presentment_amount(self):
        self.instance.order_visible_currency = True
        payload = {'presentment_currency': 'EUR', 'line_items': [source_line(discount=10)],
                   'current_total_discounts': '10', 'current_total_discounts_set': {
                       'shop_money': {'amount': '10', 'currency_code': 'USD'},
                       'presentment_money': {'amount': '0', 'currency_code': 'EUR'}}}
        self.assertEqual(self.order._get_shopify_order_discount_data(payload)['discount_101'], 10)
        self.discount(20)
        self.order.sync_discount(payload)
        self.assertEqual(self.order.discount_total(), 0)

    def test_current_tax_wins_including_explicit_zero(self):
        self.order.order_line.append(Line(self.order.tax_product, 16))
        for amount in (8, 0, 10):
            payload = {'total_tax': '16', 'current_total_tax': str(amount)}
            self.assertTrue(self.order.update_shopify_tax_line_ept(self.instance, payload))
            self.assertEqual(self.order.tax_total(), amount)
            self.assertFalse(self.order.update_shopify_tax_line_ept(self.instance, payload))

    def test_tax_money_sets_use_current_presentment_currency(self):
        self.instance.order_visible_currency = True
        for amount in ('6', '0'):
            payload = {'presentment_currency': 'EUR', 'total_tax': '16', 'current_total_tax_set': {
                'shop_money': {'amount': '8', 'currency_code': 'USD'},
                'presentment_money': {'amount': amount, 'currency_code': 'EUR'}}}
            self.assertEqual(self.order._get_shopify_order_tax_amount(self.instance, payload), float(amount))

    def test_tax_legacy_payload_falls_back_to_total(self):
        self.order.update_shopify_tax_line_ept(self.instance, {'total_tax': '8'})
        self.assertEqual(self.order.tax_total(), 8)

    def test_cash_only_import_keeps_original_tax_for_refund_evidence(self):
        payload = {'cancelled_at': '2026-10-22T12:00:00Z', 'total_tax': '8', 'current_total_tax': '0'}
        self.assertEqual(self.order._get_shopify_order_tax_amount(self.instance, payload), 8)

    def test_duplicate_tax_lines_and_nonunit_quantity_are_reconciled(self):
        self.order.order_line.extend([Line(self.order.tax_product, 8, quantity=2), Line(self.order.tax_product, 8)])
        self.order.update_shopify_tax_line_ept(self.instance, {'current_total_tax': '8'})
        self.assertEqual(self.order.tax_total(), 8)
        self.assertFalse(self.order.update_shopify_tax_line_ept(self.instance, {'current_total_tax': '8'}))

    def test_invoiced_tax_adjustments_stay_unchanged_and_new_delta_remains_invoiceable(self):
        original = Line(self.order.tax_product, 8, invoiced=1)
        adjustment = Line(self.order.tax_product, 2, remote_id='shopify-tax-adjustment', invoiced=1)
        self.order.order_line.extend([original, adjustment])
        self.order.update_shopify_tax_line_ept(self.instance, {'current_total_tax': '6'})
        self.assertEqual((original.price_unit, adjustment.price_unit), (8, 2))
        delta = self.order.order_line[-1]
        self.assertEqual((delta.price_unit, delta.qty_invoiced), (-4, 0))
        self.assertEqual(self.order.tax_total(), 6)
        self.order.update_shopify_tax_line_ept(self.instance, {'current_total_tax': '0'})
        self.assertEqual(delta.price_unit, -10)
        self.assertEqual(self.order.tax_total(), 0)

    def test_external_tax_override_is_respected(self):
        self.order._shopify_skip_exact_tax_line.return_value = True
        self.assertFalse(self.order.update_shopify_tax_line_ept(self.instance, {'current_total_tax': '8'}))
        self.assertEqual(self.order.tax_total(), 0)

    def test_quantity_update_uses_current_quantity_and_ignores_adjustment_ids_without_workflow(self):
        self.order.order_line.append(Line(self.order.tax_product, 8, remote_id='shopify-tax-adjustment'))
        payload = {'line_items': [dict(source_line(), current_quantity=0)]}
        self.assertTrue(self.order.update_qty_in_order_webhook_ept(self.instance, queue(payload, self.instance), payload))
        self.assertEqual(self.item.product_uom_qty, 0)
        quantities, _ids = self.order.prepare_response_data_of_order_qty({
            'line_items': [dict(source_line(), quantity=3, current_quantity=2, fulfillable_quantity=0)]})
        self.assertEqual(quantities[101], 2)

    def test_discount_only_update_runs_without_quantity_change(self):
        self.discount(20)
        payload = {'line_items': [source_line(discount=5)]}
        self.assertTrue(self.order.update_qty_in_order_webhook_ept(self.instance, queue(payload, self.instance), payload))
        self.assertEqual(self.order.discount_total(), 5)

    def test_partially_fulfilled_order_can_remove_remaining_units(self):
        self.item.product_uom_qty, self.item.qty_delivered = 3, 1
        payload = {'fulfillment_status': 'partial', 'line_items': [
            dict(source_line(), quantity=3, current_quantity=1, fulfillable_quantity=0)]}
        queued = queue(payload, self.instance)
        self.order.update_shopify_order([queued], 'webhook', self.instance)
        self.assertEqual(queued.state, 'done')
        self.assertEqual(self.item.product_uom_qty, 1)

    def test_failed_quantity_change_is_not_invoiced_or_marked_done(self):
        self.order.auto_workflow_process_id = True
        self.item.qty_delivered = 1
        payload = {'line_items': [dict(source_line(), current_quantity=0)], 'current_total_tax': '8'}
        queued = queue(payload, self.instance)
        self.order.update_shopify_order([queued], 'webhook', self.instance)
        self.assertEqual(queued.state, 'failed')
        self.assertEqual(self.item.product_uom_qty, 1)
        self.assertFalse(self.order.invoice_snapshots)

    def test_refund_updates_leave_credit_creation_to_refund_workflow(self):
        self.order.auto_workflow_process_id = True
        payload = {'financial_status': 'partially_refunded', 'current_total_tax': '8'}
        self.order.update_shopify_order([queue(payload, self.instance)], 'webhook', self.instance)
        self.assertEqual(self.order.tax_total(), 8)
        self.assertFalse(self.order.invoice_snapshots)

    def test_discount_and_tax_updates_do_not_require_quantity_sync_enabled(self):
        self.instance.update_qty_order_webhook = False
        self.discount(20)
        payload = {'line_items': [source_line(discount=5)], 'current_total_tax': '4'}
        queued = queue(payload, self.instance)
        self.order.update_shopify_order([queued], 'webhook', self.instance)
        self.assertEqual(queued.state, 'done')
        self.assertEqual((self.order.discount_total(), self.order.tax_total()), (5, 4))

    def test_order_replacement_reconciles_before_invoicing_for_webhook_and_manual_update(self):
        for created_by in ('webhook', 'Manual Update'):
            with self.subTest(created_by=created_by):
                order = Order(workflow=True)
                old_item = Line(order.product, 100, remote_id='101')
                old_discount = Line(order.discount_product, -20, related_id='discount_101')
                order.order_line.extend([old_item, old_discount, Line(order.tax_product, 16)])
                payload = {'line_items': [dict(source_line(discount=20), current_quantity=0),
                                          dict(source_line(identity=102, discount=5), price='50')],
                           'total_tax': '16', 'current_total_tax': '4', 'current_total_discounts': '5'}
                queued = queue(payload, order.shopify_instance_id)
                order.update_shopify_order([queued], created_by, order.shopify_instance_id)
                self.assertEqual(queued.state, 'done')
                self.assertEqual(old_item.product_uom_qty, 0)
                self.assertEqual(old_discount.product_uom_qty, 0)
                self.assertEqual(order.invoice_snapshots, [(5, 4, 49)])
                count = len(order.order_line)
                order.update_shopify_order([queued], created_by, order.shopify_instance_id)
                self.assertEqual(len(order.order_line), count)
                self.assertEqual(order.invoice_snapshots, [(5, 4, 49)])


if __name__ == '__main__':
    unittest.main()
