"""Cash-history planning tests, including the 242.10 / 45 / 197.10 regression."""
import importlib.util
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('cash_plan', Path(__file__).resolve().parents[1] / 'models/shopify_payment_plan.py')
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


def compare(a, b):
    a, b = (Decimal(str(x)).quantize(Decimal('.01')) for x in (a, b))
    return (a > b) - (a < b)


def transaction(key='charge', kind='sale', amount='242.10', **kw):
    return dict(id=key, order_id='order', kind=kind, amount=amount, currency='USD', gateway='shopify_payments',
                status='success', processed_at='2026-08-03T14:00:00-04:00', **kw)


class TestShopifyPaymentPlan(unittest.TestCase):
    def setUp(self):
        self.raw = [transaction(), transaction('refund', 'refund', '45.00', parent_id='charge')]
        self.events = plan.cash_events(self.raw, 'order', 'USD')
        self.payload = {'line_items': [dict(id='removed', quantity=1, current_quantity=0)],
                        'refunds': [{'id': 'refund-doc', 'transactions': [{'id': 'refund'}],
                                     'refund_line_items': [dict(line_item_id='removed', quantity=1,
                                                               subtotal='45.00', total_tax='0.00')]}]}

    def test_original_charge_and_refund_remain_separate(self):
        self.assertEqual([(row['kind'], row['amount']) for row in self.events], [('sale', '242.10'), ('refund', '45.00')])
        self.assertEqual(self.events[1]['parent_id'], 'charge')

    def test_cross_day_refund_keeps_each_source_date(self):
        self.raw[1]['processed_at'] = '2026-08-04T00:15:00-04:00'
        events = plan.cash_events(self.raw, 'order', 'USD')
        self.assertEqual([row['date'] for row in events], ['2026-08-03', '2026-08-04'])

    def test_multiple_captures_are_not_netted_with_refunds(self):
        events = plan.cash_events([
            transaction('auth', 'authorization'),
            transaction('first', 'capture', '100.00', parent_id='auth'),
            transaction('second', 'capture', '142.10', parent_id='auth'),
            transaction('refund', 'refund', '45.00', parent_id='first'),
        ], 'order', 'USD')
        self.assertEqual(len(events), 3)
        self.assertEqual(plan.invoice_mode(events, 197.10, 0, compare), 'net')

    def test_net_invoice_and_original_invoice_credit_note_modes(self):
        self.assertEqual(plan.invoice_mode(self.events, 197.10, 0, compare), 'net')
        self.assertEqual(plan.invoice_mode(self.events, 242.10, 45, compare), 'gross')
        self.assertEqual(plan.invoice_mode(self.events, 242.10, 0, compare), 'gross')
        with self.assertRaises(plan.PaymentPlanError):
            plan.invoice_mode(self.events, 197.10, 45, compare)

    def test_net_invoice_requires_removed_item_evidence(self):
        plan.prove_net_refunds(self.payload, self.events, {})
        with self.assertRaises(plan.PaymentPlanError):
            plan.prove_net_refunds(self.payload, self.events, {'removed': 1})
        self.payload['line_items'][0]['current_quantity'] = 1
        with self.assertRaises(plan.PaymentPlanError):
            plan.prove_net_refunds(self.payload, self.events, {})

    def test_retained_partial_quantity_can_explain_net_invoice(self):
        self.payload['line_items'][0].update(quantity=2, current_quantity=1)
        plan.prove_net_refunds(self.payload, self.events, {'removed': 1})

    def test_unexplained_cash_or_adjustments_are_not_written_off(self):
        for field, value in [('order_adjustments', [{'amount': '45'}]), ('refund_shipping_lines', [{'amount': '45'}])]:
            payload = deepcopy(self.payload)
            payload['refunds'][0][field] = value
            with self.assertRaises(plan.PaymentPlanError):
                plan.prove_net_refunds(payload, self.events, {})
        self.payload['refunds'][0]['refund_line_items'][0]['subtotal'] = '44.00'
        with self.assertRaises(plan.PaymentPlanError):
            plan.prove_net_refunds(self.payload, self.events, {})

    def test_missing_refund_document_is_blocked(self):
        with self.assertRaises(plan.PaymentPlanError):
            plan.prove_net_refunds({'refunds': []}, self.events, {})

    def test_failed_refunds_and_authorizations_are_not_cash(self):
        failed = transaction('bad', 'refund', '45', parent_id='charge')
        failed['status'] = 'failure'
        events = plan.cash_events([self.raw[0], failed, transaction('auth', 'authorization')], 'order', 'USD')
        self.assertEqual([row['id'] for row in events], ['charge'])

    def test_same_page_duplicate_is_idempotent_conflicting_duplicate_is_blocked(self):
        self.assertEqual(plan.cash_events(self.raw * 2, 'order', 'USD'), self.events)
        with self.assertRaises(plan.PaymentPlanError):
            plan.cash_events(self.raw + [transaction(amount='197.10')], 'order', 'USD')

    def test_missing_parent_and_excess_refund_are_blocked(self):
        for parent, amount in [('unknown', '45'), ('charge', '243')]:
            with self.assertRaises(plan.PaymentPlanError):
                plan.cash_events([self.raw[0], transaction('r', 'refund', amount, parent_id=parent)], 'order', 'USD')

    def test_multiple_refunds_link_to_their_parent(self):
        events = plan.cash_events(self.raw + [transaction('second', 'refund', '25', parent_id='charge')], 'order', 'USD')
        self.assertEqual(len(events), 3)
        self.assertEqual(plan.invoice_mode(events, 172.10, 0, compare), 'net')

    def test_currency_order_gateway_and_date_must_be_known(self):
        for key, value in [('currency', 'EUR'), ('order_id', 'other'), ('gateway', 'gift_card'),
                           ('processed_at', None), ('amount', 'NaN'), ('id', None), ('amount', '0')]:
            row = transaction()
            row[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(plan.PaymentPlanError):
                plan.cash_events([row], 'order', 'USD')

    def test_fingerprint_ignores_json_tuple_conversion_but_detects_changes(self):
        self.assertEqual(plan.fingerprint({'a': [(1, 2)]}), plan.fingerprint({'a': [[1, 2]]}))
        self.assertNotEqual(plan.fingerprint({'amount': 197.1}), plan.fingerprint({'amount': 242.1}))

    def test_component_currency_uses_explicit_presentment_amount(self):
        item = {'subtotal': '50', 'subtotal_set': {
            'shop_money': {'currency_code': 'CAD', 'amount': '50'},
            'presentment_money': {'currency_code': 'USD', 'amount': '45'},
        }}
        self.assertEqual(plan.component_money(item, 'subtotal', 'USD', 'CAD'), Decimal('45'))
        with self.assertRaises(plan.PaymentPlanError):
            plan.component_money(item, 'subtotal', 'EUR', 'CAD')

    def test_multiple_gateway_refund_cannot_point_to_another_gateway(self):
        row = deepcopy(self.raw[1])
        row['gateway'] = 'paypal'
        with self.assertRaises(plan.PaymentPlanError):
            plan.cash_events([self.raw[0], row], 'order', 'USD')

    def test_full_refund_of_original_invoice(self):
        events = plan.cash_events([self.raw[0], transaction('full', 'refund', '242.10', parent_id='charge')], 'order', 'USD')
        self.assertEqual(plan.invoice_mode(events, 242.10, 242.10, compare), 'gross')

    def test_audited_net_invoice_allows_only_new_refund_credit(self):
        events = self.events + plan.cash_events([
            self.raw[0], transaction('later', 'refund', '25', parent_id='charge')], 'order', 'USD')[1:]
        self.assertEqual(plan.invoice_mode(events, 197.10, 25, compare, 45), 'net_with_credits')
        with self.assertRaises(plan.PaymentPlanError):
            plan.invoice_mode(events, 197.10, 70, compare, 45)
        with self.assertRaises(plan.PaymentPlanError):
            plan.invoice_mode(events, 172.10, 25, compare, 45)

    def test_three_decimal_currency_is_not_rounded_to_cents(self):
        row = transaction(amount='1.234')
        row['currency'] = 'KWD'
        self.assertEqual(plan.cash_events([row], 'order', 'KWD')[0]['amount'], '1.234')
