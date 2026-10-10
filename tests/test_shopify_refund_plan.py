"""Cutover dates, adjustment signs, and final-order cash evidence."""
import importlib.util
import sys
import types
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import unittest

package = types.ModuleType('shopify_source_plans')
package.__path__ = [str(Path(__file__).resolve().parents[1] / 'models')]
sys.modules.setdefault(package.__name__, package)
spec = importlib.util.spec_from_file_location(package.__name__ + '.shopify_refund_plan',
                                             Path(package.__path__[0]) / 'shopify_refund_plan.py')
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


def compare(a, b):
    a, b = Decimal(str(a)).quantize(Decimal('.01')), Decimal(str(b)).quantize(Decimal('.01'))
    return (a > b) - (a < b)


class TestShopifyRefundPlan(unittest.TestCase):
    def setUp(self):
        self.payload = dict(line_items=[dict(id='item', quantity=1, current_quantity=1)],
                            fulfillments=[dict(status='success', created_at='2026-07-25T18:00:00Z',
                                               line_items=[dict(id='item', quantity=1)])])
        self.events = [dict(id='charge', kind='sale', amount='245.70', currency='USD'),
                       dict(id='refund', kind='refund', amount='215.35', currency='USD')]
        self.refund = dict(id='refund-doc', transactions=[dict(id='refund')],
                           refund_line_items=[dict(line_item_id='item', quantity=1, subtotal='224.10', total_tax='0')],
                           order_adjustments=[dict(kind='refund_discrepancy', amount='8.75', tax_amount='0')])

    def test_historical_fulfillment_requires_completed_line_evidence(self):
        plan.prove_historical_fulfillment(self.payload, '2026-08-01', 'America/New_York')
        for replacement in ([], [dict(status='cancelled', created_at='2026-07-25T18:00:00Z', line_items=[])],
                            [dict(status='success', created_at='2026-08-01T18:00:00Z', line_items=[dict(id='item', quantity=1)])]):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                plan.prove_historical_fulfillment(dict(self.payload, fulfillments=replacement), '2026-08-01', 'America/New_York')

    def test_fulfillment_date_uses_business_timezone(self):
        self.payload['fulfillments'][0]['created_at'] = '2026-08-01T01:00:00Z'
        plan.prove_historical_fulfillment(self.payload, '2026-08-01', 'America/New_York')
        with self.assertRaises(ValueError):
            plan.prove_historical_fulfillment(self.payload, '2026-08-01', 'UTC')

    def test_partial_fulfillment_is_not_historical(self):
        self.payload['line_items'][0]['quantity'] = 2
        with self.assertRaises(ValueError):
            plan.prove_historical_fulfillment(self.payload, '2026-08-01', 'UTC')

    def test_return_deduction_preserves_gross_and_adjustment(self):
        parts, events = plan.refund_components(self.refund, self.events, 'USD', 'USD', compare)
        self.assertEqual([part['subtotal'] for part in parts], ['224.10', '-8.75'])
        self.assertEqual(events, self.events[1:])

    def test_amount_only_refund_has_negative_source_discrepancy(self):
        self.refund['refund_line_items'] = []
        self.refund['order_adjustments'][0]['amount'] = '-215.35'
        parts, _ = plan.refund_components(self.refund, self.events, 'USD', 'USD', compare)
        self.assertEqual(parts[0]['subtotal'], '215.35')

    def test_unknown_adjustment_sign_or_total_needs_review(self):
        self.refund['order_adjustments'][0]['amount'] = '-8.75'
        with self.assertRaises(ValueError):
            plan.refund_components(self.refund, self.events, 'USD', 'USD', compare)
        self.refund['order_adjustments'][0]['kind'] = 'unknown'
        with self.assertRaises(ValueError):
            plan.refund_components(self.refund, self.events, 'USD', 'USD', compare)

    def test_shipping_tax_must_be_explicit(self):
        refund = dict(id='doc', transactions=[dict(id='refund')],
                      refund_shipping_lines=[dict(subtotal_amount='215.35')])
        with self.assertRaises(ValueError):
            plan.refund_components(refund, self.events, 'USD', 'USD', compare)
        refund['refund_shipping_lines'][0]['shipping_line'] = dict(tax_lines=[])
        parts, _ = plan.refund_components(refund, self.events, 'USD', 'USD', compare)
        self.assertEqual(parts[0]['tax'], '0')

    def test_retroactive_discount_requires_current_total_and_quantities(self):
        payload = dict(self.payload, currency='USD', current_total_price='30.35', refunds=[self.refund])
        plan.prove_final_net_invoice(payload, self.events, {'item': 1}, 30.35, compare)
        with self.assertRaises(ValueError):
            plan.prove_final_net_invoice(payload, self.events, {'item': 2}, 30.35, compare)
        payload['current_total_price'] = '29.35'
        with self.assertRaises(ValueError):
            plan.prove_final_net_invoice(payload, self.events, {'item': 1}, 30.35, compare)

    def test_duplicate_refund_document_is_not_proof(self):
        payload = dict(self.payload, currency='USD', current_total_price='30.35', refunds=[self.refund, deepcopy(self.refund)])
        with self.assertRaises(ValueError):
            plan.prove_final_net_invoice(payload, self.events, {'item': 1}, 30.35, compare)
