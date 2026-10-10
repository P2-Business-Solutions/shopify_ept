"""Database integration tests for gross cash with net invoices and historical repair."""
from datetime import timedelta
import json
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
        cls.product.property_account_income_id = cls.revenue

    def _cancelled_fixture(self, gateway='shopify_payments'):
        order, invoice, payload, events = self._fixture()
        invoice.button_draft()
        invoice.unlink()
        self.gateway.code = gateway
        events[1]['amount'] = events[0]['amount']
        for event in events:
            event['gateway'] = gateway
        payload.update(cancelled_at=events[1]['processed_at'], financial_status='refunded',
                       cancel_reason=None, fulfillments=[], fulfillment_status=None,
                       order_number=123, name='#123', created_at=events[0]['processed_at'])
        queue = self.env['shopify.order.data.queue.ept'].create({
            'shopify_instance_id': self.instance.id, 'queue_type': 'unshipped', 'created_by': 'import',
        })
        queue_line = self.env['shopify.order.data.queue.line.ept'].create({
            'shopify_order_data_queue_id': queue.id, 'shopify_instance_id': self.instance.id,
            'shopify_order_id': order.shopify_order_id, 'order_data': json.dumps(dict(payload, transaction=events)),
        })
        return order, payload, events, queue_line

    def test_cancelled_import_records_full_cash_without_invoice_or_delivery(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            self.assertTrue(order._process_shopify_cancelled_import(payload, queue_line))
            payments = self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)])
            self.assertEqual(order.state, 'cancel')
            self.assertTrue(order.canceled_in_shopify)
            self.assertFalse(order.invoice_ids)
            self.assertFalse(order.picking_ids)
            self.assertEqual(queue_line.state, 'done')
            self.assertFalse(queue_line.order_data)
            self.assertEqual(payments.mapped('amount'), [242.10, 242.10])
            self.assertEqual(set(payments.mapped('payment_type')), {'inbound', 'outbound'})
            self.assertTrue(all(pay._seek_for_lines()[1].reconciled for pay in payments))
            self.assertTrue(all(not pay._seek_for_lines()[0].reconciled for pay in payments))
            self.assertEqual(set(payments.mapped('date')), {fields.Date.to_date(event['processed_at'][:10]) for event in events})
            self.assertEqual(order.shopify_payment_audit_ids.plan['mode'], 'cancelled_cash')
            original_moves = payments.move_id
            order._process_shopify_cancelled_import(payload, queue_line)
            self.assertEqual(self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)]), payments)
            self.assertEqual(payments.move_id, original_moves)
            self.assertEqual(len(order.shopify_payment_audit_ids), 1)

    def test_cancelled_queue_import_bypasses_auto_workflow_and_retries_existing_order(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        self.instance.import_order_after_date = fields.Date.to_date('2020-01-01')
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)), \
                patch.object(type(order), 'search_existing_shopify_order', side_effect=[self.env['sale.order'], order]), \
                patch.object(type(order), 'prepare_shopify_customer_and_addresses', return_value=(self.partner, self.partner, self.partner)), \
                patch.object(type(order), 'check_mismatch_details', return_value=False), \
                patch.object(type(order), 'shopify_create_order', return_value=order), \
                patch.object(type(order), 'apply_shopify_location_and_warehouse', side_effect=AssertionError('Unexpected fulfillment workflow')):
            self.env['sale.order'].import_shopify_orders(queue_line, self.instance)
            self.assertEqual(queue_line.state, 'done')
            self.assertEqual(queue_line.sale_order_id, order)
            self.assertEqual(order.state, 'cancel')
            queue_line.write({'state': 'draft', 'order_data': json.dumps(dict(payload, transaction=events))})
            self.env['sale.order'].import_shopify_orders(queue_line, self.instance)
            self.assertEqual(queue_line.state, 'done')
            self.assertEqual(len(order.shopify_payment_audit_ids), 1)

    def test_cancelled_partial_refund_keeps_order_cancelled_and_queue_retryable(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        events[1]['amount'] = '45.00'
        payload['financial_status'] = 'partially_refunded'
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            self.assertFalse(order._process_shopify_cancelled_import(payload, queue_line))
        self.assertEqual(order.state, 'cancel')
        self.assertEqual(queue_line.state, 'failed')
        self.assertTrue(queue_line.order_data)
        self.assertFalse(self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)]))

    def test_cancelled_import_requires_opt_in_then_can_retry(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        self.instance.shopify_transaction_payment_sync = False
        self.assertFalse(order._process_shopify_cancelled_import(payload, queue_line))
        self.assertEqual(order.state, 'cancel')
        self.assertEqual(queue_line.state, 'failed')
        self.instance.shopify_transaction_payment_sync = True
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            self.assertTrue(order._process_shopify_cancelled_import(payload, queue_line))

    def test_cancelled_authorization_only_creates_no_payments(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        payload['financial_status'] = 'voided'
        payload['transaction'] = [dict(events[0], kind='authorization')]
        self.assertTrue(order._process_shopify_cancelled_import(payload, queue_line))
        self.assertEqual(order.state, 'cancel')
        self.assertFalse(self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)]))

    def test_cancelled_cash_requires_source_cancellation_and_no_fulfillment(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        order.write({'state': 'cancel', 'canceled_in_shopify': True})
        for changes in ({'cancelled_at': None}, {'fulfillments': [{'id': 'delivery'}]}):
            source = dict(payload, **changes)
            with patch.object(type(order), '_shopify_cash_source', return_value=(source, events)), self.assertRaises(UserError):
                order._sync_shopify_cash()
        self.assertFalse(self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)]))

    def test_cancelled_cash_preview_can_apply_without_invoice(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        order.write({'state': 'cancel', 'canceled_in_shopify': True})
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'preview', wizard.preview_html)
            self.assertIn('receipt and refund only', wizard.preview_html)
            self.assertFalse(self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)]))
            wizard.action_apply()
            self.assertEqual(wizard.state, 'done')
            self.assertFalse(order.invoice_ids)

    def test_cancelled_cash_matches_charge_and_refund_payouts_without_invoice(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order._process_shopify_cancelled_import(payload, queue_line)
        for event in events:
            payout = self._payout('cancelled-' + event['id'])
            payout.payout_transaction_ids.unlink()
            transaction = self.env['shopify.payout.report.line.ept'].create({
                'payout_id': payout.id, 'transaction_id': 'balance-' + event['id'],
                'source_order_transaction_id': event['id'], 'source_order_id': order.shopify_order_id,
                'order_id': order.id, 'amount': 242.10 if event['kind'] == 'sale' else -242.10,
                'transaction_type': 'charge' if event['kind'] == 'sale' else 'refund',
            })
            self.assertEqual(payout.find_payment_for_payout_transaction(transaction).shopify_order_transaction_id, event['id'])

    def test_cancelled_paypal_payments_support_reference_backfill(self):
        if 'payout.shopify.backfill' not in self.env:
            self.skipTest('Payment Payout Reconciliation is not installed')
        order, payload, events, queue_line = self._cancelled_fixture(gateway='paypal')
        events[0]['receipt'] = {'transaction_id': 'CAP12345678901234'}
        events[1]['receipt'] = {'refund_transaction_id': 'REF12345678901234'}
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            self.assertTrue(order._process_shopify_cancelled_import(payload, queue_line))
            payments = self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)])
            wallet, transit = self.env['account.account'].create([
                {'name': name, 'code': code, 'account_type': 'asset_current', 'reconcile': True,
                 'company_ids': [Command.set(self.env.company.ids)]}
                for name, code in [('PayPal Cancellation Balance', 'CSPPBAL'), ('PayPal Cancellation Transit', 'CSPPTRN')]
            ])
            receiving_bank = self.env['account.journal'].create({
                'name': 'PayPal Cancellation Bank', 'code': 'CSBK', 'type': 'bank',
                'default_account_id': self.journal.default_account_id.id,
                'suspense_account_id': self.journal.suspense_account_id.id,
            })
            self.env['account.payment.method.line'].create({
                'name': 'PayPal cancellation deposits', 'journal_id': receiving_bank.id,
                'payment_method_id': self.env.ref('account.account_payment_method_manual_in').id,
                'payment_account_id': transit.id,
            })
            connection = self.env['payout.connection'].create({
                'name': 'Cancelled PayPal Test', 'processor': 'paypal', 'merchant_reference': 'cancelled-test',
                'payment_journal_ids': [Command.set(self.journal.ids)], 'bank_journal_id': receiving_bank.id,
                'journal_id': self.env['account.journal'].create({'name': 'PayPal Settlement Test', 'code': 'CSPP', 'type': 'general'}).id,
                'clearing_account_ids': [Command.set(self.outstanding.ids)],
                'balance_account_id': wallet.id, 'transit_account_id': transit.id,
                'fee_account_id': self.instance.transaction_line_ids.account_id.id,
            })
            wizard = self.env['payout.shopify.backfill'].create({
                'connection_id': connection.id, 'payment_ids': [Command.set(payments.ids)],
                'date_from': min(payments.mapped('date')), 'date_to': max(payments.mapped('date')),
            })
            wizard.action_preview()
            self.assertEqual({row['status'] for row in wizard.plan_json}, {'ready'}, wizard.preview_html)
            wizard.action_apply()
            self.assertEqual(set(payments.mapped('payout_reference')), {'CAP12345678901234', 'REF12345678901234'})
            self.assertEqual(payments.payout_connection_id, connection)
            self.assertFalse(order.invoice_ids)
            from odoo.addons.payment_payout_reconciliation.services.processors import batch, row
            activities = self.env['payout.batch']
            for event in events:
                refund = event['kind'] == 'refund'
                reference = event['receipt']['refund_transaction_id' if refund else 'transaction_id']
                activities |= activities._import_report(connection, batch(
                    reference, event['date'] if 'date' in event else event['processed_at'][:10], order.currency_id.name,
                    [row(reference, reference, 'refund' if refund else 'payment',
                         -242.10 if refund else 242.10, 0 if refund else 3,
                         order.currency_id.name, 'T1107' if refund else 'T0003')]))
            activities.action_match()
            activities.action_post()
            self.assertTrue(all(pay._seek_for_lines()[0].reconciled for pay in payments))
            self.assertEqual(sum(activities.move_id.line_ids.filtered(lambda line: line.account_id == wallet).mapped('balance')), -3)
            count = self.env['account.move'].search_count([])
            order._sync_shopify_cash()
            self.assertEqual(self.env['account.move'].search_count([]), count)
            self.assertTrue(all(pay._seek_for_lines()[0].reconciled for pay in payments))

    def test_cancelled_order_creation_keeps_refunded_lines_and_uses_paid_workflow(self):
        _order, payload, events, queue_line = self._cancelled_fixture()
        self.workflow.picking_policy = 'direct'
        self.env['sale.auto.workflow.configuration.ept'].create({
            'shopify_instance_id': self.instance.id, 'payment_gateway_id': self.gateway.id,
            'financial_status': 'paid', 'shopify_order_payment_status': self.env.ref('shopify_ept.unshipped').id,
            'auto_workflow_id': self.workflow.id,
        })
        self.product.default_code = 'CANCELLED-TEST'
        if 'mrp.production' in self.env:
            self.product.write({'type': 'consu', 'is_storable': True})
            routes = self.env.ref('mrp.route_warehouse0_manufacture') | self.env.ref('stock.route_warehouse0_mto')
            routes.active = True
            self.product.route_ids = routes
            self.env['mrp.bom'].create({'product_tmpl_id': self.product.product_tmpl_id.id, 'product_qty': 1})
        payload.update(currency=self.env.company.currency_id.name, source_name='web', tags='',
                       payment_gateway_names=['shopify_payments'], transaction=events, total_discounts=0,
                       line_items=[dict(id='original', sku='CANCELLED-TEST', quantity=1, current_quantity=0,
                                        price=242.10, requires_shipping=False, name='Original cancelled item')])
        notification_count = self.env['mail.notification'].search_count([])
        email_count = self.env['mail.mail'].search_count([])
        manufacturing_count = self.env['mrp.production'].search_count([]) if 'mrp.production' in self.env else 0
        order = self.env['sale.order'].shopify_create_order(
            self.instance, self.partner, self.partner, self.partner, queue_line, payload, payload['line_items'], 123)
        self.assertTrue(order)
        self.assertEqual(order.order_line.shopify_line_id, 'original')
        self.assertEqual(order.order_line.product_uom_qty, 1)
        self.assertEqual(order.auto_workflow_process_id, self.workflow)
        self.assertEqual(order.state, 'draft')
        self.assertFalse(order.invoice_ids)
        self.assertFalse(order.picking_ids)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            self.assertTrue(order._process_shopify_cancelled_import(payload, queue_line))
        self.assertEqual(order.state, 'cancel')
        self.assertFalse(order.picking_ids)
        self.assertFalse(order.message_follower_ids)
        self.assertEqual(self.env['mail.notification'].search_count([]), notification_count)
        self.assertEqual(self.env['mail.mail'].search_count([]), email_count)
        if 'mrp.production' in self.env:
            self.assertEqual(self.env['mrp.production'].search_count([]), manufacturing_count)

    def test_cancelled_import_audit_and_failures_do_not_notify_followers(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        follower = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Cancellation Notification Test', 'login': 'cancelled-import-follower',
            'email': 'cancelled-import-follower@example.invalid', 'notification_type': 'email',
            'groups_id': [Command.set(self.env.ref('base.group_system').ids)],
        })
        order.message_subscribe(partner_ids=(self.partner | follower.partner_id).ids,
                                subtype_ids=self.env.ref('mail.mt_note').ids)
        notification_count = self.env['mail.notification'].search_count([])
        email_count = self.env['mail.mail'].search_count([])
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            self.assertTrue(order._process_shopify_cancelled_import(payload, queue_line))
        self.assertTrue(order.message_ids.filtered(lambda message: 'Shopify cash transactions synchronized' in str(message.body)))
        self.assertEqual(self.env['mail.notification'].search_count([]), notification_count)
        self.assertEqual(self.env['mail.mail'].search_count([]), email_count)
        events[1]['amount'] = '45.00'
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            self.assertFalse(order._process_shopify_cancelled_import(payload, queue_line))
        queue = queue_line.shopify_order_data_queue_id
        queue.shopify_instance_id.shopify_user_ids = follower
        activity_count = self.env['mail.activity'].search_count([])
        queue.create_schedule_activity(queue)
        self.assertEqual(self.env['mail.activity'].search_count([]), activity_count)
        self.assertTrue(order.message_ids.filtered(lambda message: 'needs financial review' in str(message.body)))
        self.assertEqual(self.env['mail.notification'].search_count([]), notification_count)
        self.assertEqual(self.env['mail.mail'].search_count([]), email_count)

    def test_cancelled_request_uses_status_filter_without_fulfillment_filter(self):
        from .. import shopify
        from unittest.mock import Mock
        remote = Mock()
        remote.find.return_value = []
        queues = self.env['shopify.order.data.queue.ept']
        self.instance.shopify_store_time_zone = 'UTC'
        with patch.object(shopify, 'Order', return_value=remote):
            queues.shopify_order_request(self.instance, fields.Datetime.now(), fields.Datetime.now(), 'cancelled')
        self.assertEqual(remote.find.call_args.kwargs['status'], 'cancelled')
        self.assertNotIn('fulfillment_status', remote.find.call_args.kwargs)

    def test_cancelled_import_queues_every_page_and_activates_processor(self):
        from unittest.mock import Mock
        queues = self.env['shopify.order.data.queue.ept']
        line_model = self.env['shopify.order.data.queue.line.ept']
        orders = [Mock() for _ in range(250)]
        processor = self.env.ref('shopify_ept.process_shopify_order_queue')
        processor.active = False
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(type(queues), 'shopify_order_request', return_value=orders) as request, \
                patch.object(type(line_model), 'create_order_data_queue_line', return_value=[100]) as first_page, \
                patch.object(type(queues), 'list_all_orders', return_value=[101]) as remaining_pages:
            result = queues.shopify_create_order_data_queues(
                self.instance, fields.Datetime.now(), fields.Datetime.now(), order_type='cancelled')
        self.assertEqual(result, [100, 101])
        self.assertEqual(request.call_args.args[-1], 'cancelled')
        self.assertEqual(first_page.call_args.args[2], 'unshipped')
        remaining_pages.assert_called_once()
        self.assertTrue(processor.active)

    def test_cancelled_queue_does_not_fetch_fulfillment_orders(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        self.instance.is_delivery_multi_warehouse = True
        queue_line.shopify_order_data_queue_id.created_by = 'webhook'
        line_model = self.env['shopify.order.data.queue.line.ept']
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(type(line_model), 'create_order_queue_line', return_value=True), \
                patch.object(type(order), 'get_shopify_fulfillment_orders', side_effect=AssertionError('Unexpected fulfillment fetch')):
            line_model.create_order_data_queue_line([payload], self.instance, 'unshipped', created_by='webhook')

    def test_cancelled_existing_invoice_uses_confirmed_refund_without_full_reversal(self):
        order, invoice, payload, events = self._fixture(gross=True)
        payload.update(cancelled_at=events[1]['processed_at'], financial_status='partially_refunded')
        queue_line = self.env['shopify.order.data.queue.line.ept'].create({
            'shopify_instance_id': self.instance.id, 'order_data': json.dumps(payload),
        })
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            self.assertTrue(order._process_shopify_cancelled_import(payload, queue_line))
        self.assertEqual(order.state, 'cancel')
        self.assertEqual(invoice.state, 'posted')
        credits = order.invoice_ids.filtered(lambda move: move.move_type == 'out_refund')
        self.assertEqual(credits.amount_total, 45.0)
        self.assertEqual(len(credits), 1)
        payments = self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)])
        self.assertEqual(sorted(payments.mapped('amount')), [45.0, 242.10])

    def test_cancelled_cash_refuses_receivable_allocation_to_other_order(self):
        order, payload, events, queue_line = self._cancelled_fixture()
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            order._process_shopify_cancelled_import(payload, queue_line)
            payments = self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)])
            payments.move_id.line_ids.filtered(lambda line: line.account_type == 'asset_receivable').remove_move_reconcile()
            _other_order, other_invoice, _other_payload, _other_events = self._fixture(gross=True)
            receipt = payments.filtered(lambda pay: pay.payment_type == 'inbound')
            (receipt._seek_for_lines()[1] | other_invoice.line_ids.filtered(lambda line: line.account_type == 'asset_receivable')).reconcile()
            with self.assertRaises(UserError):
                order._build_shopify_cash_plan(repair=True)

    def _fixture(self, gross=False, retained=197.10, removed=45.0):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'shopify_instance_id': self.instance.id, 'shopify_order_id': '135267',
            'shopify_payment_gateway_id': self.gateway.id, 'auto_workflow_process_id': self.workflow.id,
        })
        invoice_lines = []
        for key, amount in [('retained', retained), ('removed', removed)]:
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
        events = [dict(common, id='charge', kind='sale', amount='%.2f' % (retained + removed)),
                  dict(common, id='refund', kind='refund', amount='%.2f' % removed, parent_id='charge',
                       processed_at=fields.Date.today().isoformat() + 'T10:00:00-04:00')]
        payload = {'id': '135267', 'line_items': [dict(id='removed', quantity=1, current_quantity=0)],
                   'refunds': [dict(id='refund-doc', transactions=[dict(id='refund')],
                                   refund_line_items=[dict(line_item_id='removed', quantity=1,
                                                          subtotal='%.2f' % removed, total_tax='0.00')])]}
        return order, invoice, payload, events

    def _amount_only_fixture(self):
        order, invoice, payload, events = self._fixture(gross=True, retained=405.0, removed=9.95)
        events[1]['processed_at'] = events[0]['processed_at']
        payload['refunds'][0].update(refund_line_items=[], order_adjustments=[{
            'kind': 'refund_discrepancy', 'amount': '-9.95', 'tax_amount': '0.00',
        }])
        return order, invoice, payload, events

    def _reviewed_credit(self, order, invoice, post=True, **overrides):
        values = {
            'journal_id': self.sales_journal.id, 'move_type': 'out_refund',
            'company_id': order.company_id.id, 'partner_id': self.partner.id,
            'currency_id': order.currency_id.id, 'invoice_date': invoice.invoice_date,
            'date': invoice.date, 'shopify_instance_id': self.instance.id,
            'shopify_refund_order_id': order.id, 'shopify_refund_id': 'refund-doc',
            'is_refund_in_shopify': True,
            'invoice_line_ids': [Command.create({
                'product_id': self.product.id, 'name': 'Reviewed hand-delivery adjustment',
                'account_id': self.revenue.id, 'quantity': 1, 'price_unit': 9.95,
                'tax_ids': [Command.clear()],
            })],
        }
        values.update(overrides)
        credit = self.env['account.move'].create(values)
        if post:
            credit.action_post()
        return credit

    def _assert_amount_only_repair(self, applied):
        order, invoice, payload, events = self._amount_only_fixture()
        credit = self._reviewed_credit(order, invoice)
        self.assertNotIn(credit, order.invoice_ids)
        if applied:
            (credit.line_ids | invoice.line_ids).filtered(
                lambda line: line.account_type == 'asset_receivable').reconcile()
            self.assertEqual(invoice.amount_residual, 405.0)
            self.assertTrue(credit.currency_id.is_zero(credit.amount_residual))
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            move_count = self.env['account.move'].search_count([])
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'preview')
            self.assertEqual(self.env['account.move'].search_count([]), move_count)
            self.assertEqual(wizard.plan_json[0]['reused_credit_ids'], credit.ids)
            self.assertFalse(wizard.plan_json[0]['credits'])
            self.assertIn(credit.name, wizard.preview_html)
            wizard.action_apply()
            payments = self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)])
            self.assertEqual(sorted(payments.mapped('amount')), [9.95, 414.95])
            self.assertEqual(len(payments), 2)
            self.assertEqual(invoice.amount_total, 414.95)
            self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))
            self.assertTrue(credit.currency_id.is_zero(credit.amount_residual))
            for payment in payments:
                liquidity, counterpart, _ = payment._seek_for_lines()
                self.assertFalse(liquidity.reconciled)
                self.assertEqual(abs(liquidity.amount_residual_currency), payment.amount)
                self.assertTrue(counterpart.reconciled)
            self.assertNotIn(credit, order.invoice_ids)
            self.assertEqual(self.env['account.move'].search_count([]), move_count + 2)
            audit_count = len(order.shopify_payment_audit_ids)
            order._sync_shopify_cash()
            self.assertEqual(len(order.shopify_payment_audit_ids), audit_count)
            self.assertEqual(self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)]), payments)

    def test_amount_only_refund_reuses_explicit_order_credit(self):
        self._assert_amount_only_repair(applied=False)

    def test_amount_only_refund_reuses_credit_already_applied_to_invoice(self):
        self._assert_amount_only_repair(applied=True)

    def test_standalone_credit_requires_explicit_order_not_just_refund_id(self):
        order, invoice, payload, events = self._amount_only_fixture()
        self._reviewed_credit(order, invoice, shopify_refund_order_id=False)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'reviewed credit note'):
                order._build_shopify_cash_plan(repair=True)

    def test_other_orders_credit_with_same_refund_id_is_not_a_candidate(self):
        order, invoice, payload, events = self._amount_only_fixture()
        other_order = order.copy({'shopify_order_id': 'another-order'})
        self._reviewed_credit(other_order, invoice)
        own_credit = self._reviewed_credit(order, invoice)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            plan = order._build_shopify_cash_plan(repair=True)
        self.assertEqual(plan['reused_credit_ids'], own_credit.ids)
        self.assertFalse(plan['credits'])

    def test_explicit_order_credit_still_checks_customer(self):
        order, invoice, payload, events = self._amount_only_fixture()
        other_partner = self.env['res.partner'].create({
            'name': 'Other refund customer', 'property_account_receivable_id': self.receivable.id,
        })
        self._reviewed_credit(order, invoice, partner_id=other_partner.id)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'another customer'):
                order._build_shopify_cash_plan(repair=True)

    def test_explicit_order_credit_still_checks_store(self):
        order, invoice, payload, events = self._amount_only_fixture()
        self._reviewed_credit(order, invoice, shopify_instance_id=False)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'Shopify store'):
                order._build_shopify_cash_plan(repair=True)

    def test_explicit_order_credit_still_checks_source_refund_id(self):
        order, invoice, payload, events = self._amount_only_fixture()
        self._reviewed_credit(order, invoice, shopify_refund_id='another-refund')
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'reviewed credit note'):
                order._build_shopify_cash_plan(repair=True)

    def test_explicit_order_credit_still_checks_currency(self):
        order, invoice, payload, events = self._amount_only_fixture()
        currency = self.env['res.currency'].with_context(active_test=False).search([
            ('name', '=', 'EUR' if order.currency_id.name != 'EUR' else 'USD'),
        ], limit=1)
        currency.active = True
        self._reviewed_credit(order, invoice, currency_id=currency.id)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'company and currency'):
                order._build_shopify_cash_plan(repair=True)

    def test_explicit_order_credit_still_checks_amount(self):
        order, invoice, payload, events = self._amount_only_fixture()
        self._reviewed_credit(order, invoice, invoice_line_ids=[Command.create({
            'name': 'Incorrect adjustment', 'account_id': self.revenue.id,
            'quantity': 1, 'price_unit': 9.94, 'tax_ids': [Command.clear()],
        })])
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'differs from its Shopify refund'):
                order._build_shopify_cash_plan(repair=True)

    def test_explicit_order_credit_with_another_sales_order_line_is_blocked(self):
        order, invoice, payload, events = self._amount_only_fixture()
        other_order = order.copy({'shopify_order_id': 'another-order'})
        credit = self._reviewed_credit(order, invoice)
        credit.invoice_line_ids.sale_line_ids = other_order.order_line[:1]
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'another order'):
                order._build_shopify_cash_plan(repair=True)

    def test_sales_order_line_credit_with_conflicting_explicit_order_is_blocked(self):
        order, invoice, payload, events = self._amount_only_fixture()
        other_order = order.copy({'shopify_order_id': 'another-order'})
        credit = self._reviewed_credit(other_order, invoice)
        credit.invoice_line_ids.sale_line_ids = order.order_line[:1]
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'conflicting Shopify order link'):
                order._build_shopify_cash_plan(repair=True)

    def test_standalone_credit_reversing_other_invoice_is_blocked(self):
        order, invoice, payload, events = self._amount_only_fixture()
        other_invoice = invoice.copy({'name': '/'})
        other_invoice.invoice_line_ids.sale_line_ids = False
        other_invoice.action_post()
        self._reviewed_credit(order, invoice, reversed_entry_id=other_invoice.id)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'reverses an invoice outside'):
                order._build_shopify_cash_plan(repair=True)

    def test_standalone_credit_applied_to_other_invoice_is_blocked(self):
        order, invoice, payload, events = self._amount_only_fixture()
        credit = self._reviewed_credit(order, invoice)
        other_invoice = invoice.copy({'name': '/'})
        other_invoice.invoice_line_ids.sale_line_ids = False
        other_invoice.action_post()
        (credit.line_ids | other_invoice.line_ids).filtered(
            lambda line: line.account_type == 'asset_receivable').reconcile()
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'outside this order'):
                order._build_shopify_cash_plan(repair=True)

    def test_draft_explicit_order_credit_blocks_repair(self):
        order, invoice, payload, events = self._amount_only_fixture()
        self._reviewed_credit(order, invoice, post=False)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'blocked')
            with self.assertRaises(UserError):
                wizard.action_apply()

    def test_duplicate_explicit_order_refund_credits_block_repair(self):
        order, invoice, payload, events = self._amount_only_fixture()
        self._reviewed_credit(order, invoice)
        self._reviewed_credit(order, invoice)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaises(UserError):
                order._build_shopify_cash_plan(repair=True)

    def test_changed_standalone_credit_allocation_invalidates_preview(self):
        order, invoice, payload, events = self._amount_only_fixture()
        credit = self._reviewed_credit(order, invoice)
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            (credit.line_ids | invoice.line_ids).filtered(
                lambda line: line.account_type == 'asset_receivable').reconcile()
            with self.assertRaisesRegex(UserError, 'changed after preview'):
                wizard.action_apply()
            self.assertFalse(order.shopify_payment_audit_ids)

    def test_changed_standalone_credit_order_link_invalidates_preview(self):
        order, invoice, payload, events = self._amount_only_fixture()
        credit = self._reviewed_credit(order, invoice)
        other_order = order.copy({'shopify_order_id': 'another-order'})
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            credit.shopify_refund_order_id = other_order
            with self.assertRaises(UserError):
                wizard.action_apply()
            self.assertFalse(order.shopify_payment_audit_ids)

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

    def _assert_identified_receipt_keeps_shipping_date(self, settled):
        order, invoice, payload, events = self._amount_only_fixture()
        self.gateway.code = 'paypal'
        for event in events:
            event['gateway'] = 'paypal'
        transaction_date = invoice.invoice_date - timedelta(days=2)
        events[0]['processed_at'] = transaction_date.isoformat() + 'T10:00:00-04:00'
        credit = self._reviewed_credit(order, invoice)
        receipt = self._legacy(invoice, 414.95)
        receipt.write({'shopify_instance_id': self.instance.id, 'shopify_order_transaction_id': 'charge'})
        liquidity = receipt._seek_for_lines()[0]
        if settled:
            balance = self.env['account.account'].create({
                'name': 'PayPal Test Balance', 'code': 'CSPPBAL', 'account_type': 'asset_current',
                'reconcile': True, 'company_ids': [Command.set(self.env.company.ids)],
            })
            journal = self.env['account.journal'].create({
                'name': 'PayPal Test Settlement', 'code': 'CSPP', 'type': 'general',
                'company_id': self.env.company.id,
            })
            settlement = self.env['account.move'].create({
                'journal_id': journal.id, 'date': receipt.date,
                'line_ids': [Command.create({
                    'account_id': balance.id, 'debit': receipt.amount,
                }), Command.create({
                    'account_id': self.outstanding.id, 'credit': receipt.amount,
                    'partner_id': self.partner.id,
                })],
            })
            settlement.action_post()
            (liquidity | settlement.line_ids.filtered(
                lambda line: line.account_id == self.outstanding)).reconcile()
        original_move = receipt.move_id
        original_date = receipt.date
        original_items = original_move.line_ids.ids
        original_matches = (liquidity.matched_debit_ids | liquidity.matched_credit_ids).ids
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            move_count = self.env['account.move'].search_count([])
            wizard = self.env['shopify.payment.repair.ept'].browse(order.action_preview_shopify_payments()['res_id'])
            self.assertEqual(wizard.state, 'preview', wizard.preview_html)
            self.assertEqual(self.env['account.move'].search_count([]), move_count)
            planned = wizard.plan_json[0]['events'][0]
            self.assertEqual(planned['payment_id'], receipt.id)
            self.assertEqual(planned['date'], transaction_date.isoformat())
            self.assertEqual(planned['payment_date'], original_date.isoformat())
            self.assertIn('Payment date', wizard.preview_html)
            wizard.action_apply()
            payments = self.env['account.payment'].search([('shopify_cash_order_id', '=', order.id)])
            self.assertEqual(payments.filtered(lambda pay: pay.payment_type == 'inbound'), receipt)
            refund = payments.filtered(lambda pay: pay.payment_type == 'outbound')
            self.assertEqual(len(refund), 1)
            self.assertEqual(refund.amount, 9.95)
            self.assertFalse(refund._seek_for_lines()[0].reconciled)
            self.assertEqual(receipt.move_id, original_move)
            self.assertEqual(receipt.date, original_date)
            self.assertEqual(original_move.date, original_date)
            self.assertEqual(original_move.line_ids.ids, original_items)
            self.assertFalse(original_move.reversal_move_ids)
            self.assertEqual((liquidity.matched_debit_ids | liquidity.matched_credit_ids).ids, original_matches)
            self.assertEqual(liquidity.reconciled, settled)
            self.assertEqual(liquidity.amount_residual_currency, 0 if settled else receipt.amount)
            self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))
            self.assertTrue(credit.currency_id.is_zero(credit.amount_residual))
            self.assertEqual(self.env['account.move'].search_count([]), move_count + 1)
            order._sync_shopify_cash()
            self.assertEqual(self.env['account.move'].search_count([]), move_count + 1)

    def test_identified_receipt_keeps_shipping_date(self):
        self._assert_identified_receipt_keeps_shipping_date(settled=False)

    def test_paypal_settled_receipt_keeps_shipping_date_and_reconciliation(self):
        self._assert_identified_receipt_keeps_shipping_date(settled=True)

    def test_unidentified_receipt_with_different_date_is_not_guessed(self):
        order, invoice, payload, events = self._fixture(gross=True)
        receipt = self._legacy(invoice, 242.10)
        events[0]['processed_at'] = (invoice.invoice_date - timedelta(days=2)).isoformat() + 'T10:00:00-04:00'
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'Existing payments do not match Shopify cash history'):
                order._build_shopify_cash_plan(repair=True)
        self.assertFalse(receipt.shopify_order_transaction_id)
        self.assertFalse(receipt.move_id.reversal_move_ids)
        self.assertFalse(order.shopify_payment_audit_ids)

    def test_identified_receipt_with_different_date_still_checks_amount(self):
        order, invoice, payload, events = self._fixture(gross=True)
        receipt = self._legacy(invoice, 242.11)
        receipt.write({'shopify_instance_id': self.instance.id, 'shopify_order_transaction_id': 'charge'})
        events[0]['processed_at'] = (invoice.invoice_date - timedelta(days=2)).isoformat() + 'T10:00:00-04:00'
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            with self.assertRaisesRegex(UserError, 'inconsistent accounting'):
                order._build_shopify_cash_plan(repair=True)
        self.assertEqual(receipt.amount, 242.11)
        self.assertFalse(receipt.move_id.reversal_move_ids)
        self.assertFalse(order.shopify_payment_audit_ids)

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
        order.company_id.country_id = self.env.ref('base.us')
        group = self.env['account.tax.group'].create({'name': 'Cash Sync Tax',
            'company_id': order.company_id.id, 'country_id': self.env.ref('base.us').id})
        tax = self.env['account.tax'].create({
            'name': 'Cash Sync 10%', 'amount': 10, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'company_id': order.company_id.id,
            'tax_group_id': group.id,
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
