"""POS-only date-range imports exclude web orders before creating any queues."""
from datetime import datetime, timedelta
from unittest.mock import call, patch

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
if ODOO_AVAILABLE:
    from .. import shopify


class OrderResource:
    def __init__(self, identity, source='pos'):
        self.id = identity
        self.payload = {
            'id': identity, 'order_number': identity, 'name': '#%s' % identity,
            'source_name': source, 'fulfillment_status': 'fulfilled', 'financial_status': 'paid',
            'created_at': '2026-10-13T12:00:00-04:00', 'tags': '',
            'payment_gateway_names': [], 'line_items': [], 'fulfillments': [],
        }

    def to_dict(self):
        return dict(self.payload)


class OrderPage(list):
    def __init__(self, orders, next_cursor=None):
        super().__init__(orders)
        cursor = next_cursor or 'previous'
        relation = 'next' if next_cursor else 'previous'
        self.metadata = {'headers': {
            'Link': '<https://pos-import.example/orders.json?page_info=%s>; rel="%s"' % (cursor, relation),
        }}


class TestShopifyPosOrderImport(PayoutTestCase):
    def setUp(self):
        super().setUp()
        self.from_date = datetime(2026, 9, 1)
        self.to_date = datetime(2026, 10, 13)
        self.checkpoint = datetime(2026, 8, 15)
        self.instance.last_shipped_order_import_date = self.checkpoint
        self.instance.import_order_after_date = '2020-01-01'
        self.queues = self.env['shopify.order.data.queue.ept']

    def _import(self, page, pos_only=True, next_pages=()):
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(self.env.cr, 'commit'), \
                patch.object(type(self.queues), 'shopify_order_request', return_value=page), \
                patch.object(shopify.Order, 'find', side_effect=list(next_pages)) as find:
            queue_ids = self.queues.shopify_create_order_data_queues(
                self.instance, self.from_date, self.to_date, order_type='shipped', pos_only=pos_only)
        return self.queues.browse(queue_ids), find

    def _source_ids(self, queues):
        return set(queues.order_data_queue_line_ids.mapped('shopify_order_id'))

    def test_only_pos_orders_are_queued_and_other_channels_are_excluded(self):
        page = OrderPage([
            OrderResource(101), OrderResource(102, 'web'), OrderResource(103, 'iphone'),
            OrderResource(104, None), OrderResource(105, 'shopify_draft_order'),
        ])
        page[1].payload['tags'] = 'POS'
        queues, find = self._import(page)
        self.assertEqual(self._source_ids(queues), {'101'})
        self.assertEqual(queues.queue_type, 'shipped')
        self.assertFalse(find.called)

    def test_pos_filter_accepts_dicts_and_resources_without_losing_order_data(self):
        resource = OrderResource(101)
        payload = OrderResource(102).to_dict()
        result = self.queues.filter_shopify_pos_orders([
            resource, payload, OrderResource(103, 'web'), {'id': 104},
        ])
        self.assertEqual(result, [resource, payload])
        self.assertIs(result[0], resource)
        self.assertIs(result[1], payload)

    def test_unchecked_option_preserves_normal_shipped_import_and_checkpoint(self):
        queues, _find = self._import(OrderPage([
            OrderResource(101), OrderResource(102, 'web'),
        ]), pos_only=False)
        self.assertEqual(self._source_ids(queues), {'101', '102'})
        self.assertEqual(self.instance.last_shipped_order_import_date, self.to_date - timedelta(days=2))

    def test_pos_only_import_preserves_general_shipped_import_checkpoint(self):
        queues, _find = self._import(OrderPage([OrderResource(101)]))
        self.assertTrue(queues)
        self.assertEqual(self.instance.last_shipped_order_import_date, self.checkpoint)

    def test_all_web_result_creates_no_queues_and_preserves_checkpoint(self):
        before = self.queues.search_count([('shopify_instance_id', '=', self.instance.id)])
        queues, _find = self._import(OrderPage([OrderResource(101, 'web')]))
        self.assertFalse(queues)
        self.assertEqual(self.queues.search_count([('shopify_instance_id', '=', self.instance.id)]), before)
        self.assertEqual(self.instance.last_shipped_order_import_date, self.checkpoint)

    def test_full_web_first_page_still_fetches_pos_orders_from_next_page(self):
        first = OrderPage([OrderResource(identity, 'web') for identity in range(1000, 1250)], 'second')
        last = OrderPage([OrderResource(2001), OrderResource(2002, 'web')])
        queues, find = self._import(first, next_pages=[last])
        self.assertEqual(self._source_ids(queues), {'2001'})
        find.assert_called_once_with(limit=250, page_info='second')
        self.assertEqual(self.instance.last_shipped_order_import_date, self.checkpoint)

    def test_web_only_middle_page_does_not_stop_pagination_or_queue_web_orders(self):
        first = OrderPage([OrderResource(1001)] + [
            OrderResource(identity, 'web') for identity in range(1002, 1251)], 'second')
        middle = OrderPage([OrderResource(identity, 'web') for identity in range(2000, 2250)], 'third')
        last = OrderPage([OrderResource(3001), OrderResource(3002, 'web')])
        queues, find = self._import(first, next_pages=[middle, last])
        self.assertEqual(self._source_ids(queues), {'1001', '3001'})
        self.assertEqual(find.call_args_list, [
            call(limit=250, page_info='second'), call(limit=250, page_info='third'),
        ])

    def test_paginated_web_only_result_creates_no_queues(self):
        first = OrderPage([OrderResource(identity, 'web') for identity in range(1000, 1250)], 'second')
        last = OrderPage([OrderResource(2001, 'web')])
        queues, find = self._import(first, next_pages=[last])
        self.assertFalse(queues)
        find.assert_called_once_with(limit=250, page_info='second')

    def test_wizard_queues_pos_only_when_checkbox_is_enabled(self):
        wizard = self.env['shopify.process.import.export'].create({
            'shopify_operation': 'import_shipped_orders', 'shopify_instance_id': self.instance.id,
            'orders_from_date': self.from_date, 'orders_to_date': self.to_date, 'pos_orders_only': True,
        })
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(self.env.cr, 'commit'), \
                patch.object(type(self.queues), 'shopify_order_request', return_value=OrderPage([
                    OrderResource(101), OrderResource(102, 'web'),
                ])):
            action = wizard.shopify_execute()
        queues = self.queues.browse(action['res_id'])
        self.assertEqual(self._source_ids(queues), {'101'})
        self.assertEqual(self.instance.last_shipped_order_import_date, self.checkpoint)

    def test_wizard_defaults_to_all_channels(self):
        wizard = self.env['shopify.process.import.export'].create({
            'shopify_operation': 'import_shipped_orders', 'shopify_instance_id': self.instance.id,
            'orders_from_date': self.from_date, 'orders_to_date': self.to_date,
        })
        self.assertFalse(wizard.pos_orders_only)
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(self.env.cr, 'commit'), \
                patch.object(type(self.queues), 'shopify_order_request', return_value=OrderPage([
                    OrderResource(101), OrderResource(102, 'web'),
                ])):
            action = wizard.shopify_execute()
        self.assertEqual(self._source_ids(self.queues.browse(action['res_id'])), {'101', '102'})

    def test_hidden_checkbox_does_not_filter_other_operations(self):
        wizard = self.env['shopify.process.import.export'].create({
            'shopify_operation': 'import_unshipped_orders', 'shopify_instance_id': self.instance.id,
            'orders_from_date': self.from_date, 'orders_to_date': self.to_date, 'pos_orders_only': True,
        })
        with patch.object(type(self.queues), 'shopify_create_order_data_queues', return_value=[]) as create_queues:
            wizard.shopify_execute()
        self.assertNotIn('pos_only', create_queues.call_args.kwargs)
        self.assertEqual(create_queues.call_args.kwargs['order_type'], 'unshipped')

    def test_existing_shipped_web_order_queue_skips_delivery_and_invoice_recreation(self):
        partner = self.env['res.partner'].create({'name': 'Mixed Import Customer'})
        orders = self.env['sale.order'].create([{
            'partner_id': partner.id, 'shopify_instance_id': self.instance.id,
            'shopify_order_id': str(identity), 'shopify_order_number': str(identity),
            'shopify_order_status': 'fulfilled', 'is_pos_order': pos, 'state': 'sale',
        } for identity, pos in ((101, True), (102, False))])
        queues, _find = self._import(OrderPage([
            OrderResource(101), OrderResource(102, 'web'),
        ]), pos_only=False)
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(type(orders), 'process_shopify_pos_fulfillments', return_value=True) as process_pos, \
                patch.object(type(orders), 'fulfilled_shopify_order', side_effect=AssertionError('Unexpected web delivery')), \
                patch.object(type(orders), 'validate_and_paid_invoices_ept', side_effect=AssertionError('Unexpected web invoice')):
            queues.order_data_queue_line_ids.process_import_order_queue_data()
        self.assertEqual(process_pos.call_count, 1)
        self.assertEqual(process_pos.call_args.args[0]['id'], 101)
        self.assertEqual(set(queues.order_data_queue_line_ids.mapped('state')), {'done'})
        self.assertEqual(self.env['sale.order'].search_count([
            ('shopify_instance_id', '=', self.instance.id), ('shopify_order_id', 'in', ['101', '102']),
        ]), 2)
        self.assertFalse(orders.picking_ids)
        self.assertFalse(orders.invoice_ids)

    def test_pos_only_queues_do_not_modify_existing_web_order_or_its_pending_queue(self):
        partner = self.env['res.partner'].create({'name': 'Web Import Customer'})
        web = self.env['sale.order'].create({
            'partner_id': partner.id, 'shopify_instance_id': self.instance.id,
            'shopify_order_id': '102', 'shopify_order_number': '102',
            'shopify_order_status': 'fulfilled', 'state': 'sale',
        })
        web_queues, _find = self._import(OrderPage([OrderResource(102, 'web')]), pos_only=False)
        pending = web_queues.order_data_queue_line_ids
        old_payload, old_write_date = pending.order_data, web.write_date
        queues, _find = self._import(OrderPage([OrderResource(101), OrderResource(102, 'web')]))
        self.assertEqual(self._source_ids(queues), {'101'})
        self.assertEqual(pending.state, 'draft')
        self.assertEqual(pending.order_data, old_payload)
        self.assertEqual(web.write_date, old_write_date)
        self.assertFalse(web.picking_ids)
        self.assertFalse(web.invoice_ids)
