"""Shopify orders preserve the Odoo work account and use a store-scoped customer."""

import json
from unittest.mock import patch

try:
    from .test_shopify_payout_generation import ODOO_AVAILABLE, PayoutTestCase
except ImportError:
    from test_shopify_payout_generation import ODOO_AVAILABLE, PayoutTestCase
if ODOO_AVAILABLE:
    from odoo.tools import mute_logger


class TestShopifyCustomerIdentity(PayoutTestCase):
    def setUp(self):
        super().setUp()
        self.mappings = self.env['shopify.res.partner.ept']
        self.partners = self.env['res.partner']
        # A standard many2one stands in for the optional Studio customer channel.
        self.ecom, self.work = self.env['res.partner.industry'].create([
            {'name': 'Ecom Channel'}, {'name': 'Work Channel'}])
        channel = patch.object(type(self.mappings), '_get_ecom_customer_channel_vals',
                               return_value={'industry_id': self.ecom.id})
        channel.start()
        self.addCleanup(channel.stop)
        self.email = 'shopper@work.example'
        self.company = self.partners.create(dict(name='Work Company', is_company=True, industry_id=self.work.id))
        self.work_contact = self.partners.create(dict(name='Jane Shopper', email=self.email,
            phone='2125550123', street='19 Work Street', parent_id=self.company.id,
            industry_id=self.work.id, type='invoice'))
        self.address = dict(first_name='Jane', last_name='Shopper', address1='19 Work Street')
        self.customer = dict(id=101, first_name='Jane', last_name='Shopper', email=self.email,
                             phone=self.work_contact.phone, default_address=self.address)

    def _order_partners(self, customer, pos=False, instance=None, line=None, addresses=None):
        return self.env['sale.order'].prepare_shopify_customer_and_addresses(
            dict(customer=customer, **(addresses or {})), pos, instance or self.instance, line)

    def test_work_email_and_addresses_stay_separate_on_repeat_orders(self):
        addresses = dict(billing_address=self.address, shipping_address=self.address)
        partner, shipping, billing = self._order_partners(self.customer, addresses=addresses)

        self.assertEqual(partner.email, self.work_contact.email)
        self.assertNotEqual(partner, self.work_contact)
        self.assertFalse(partner.parent_id)
        self.assertEqual(partner.industry_id, self.ecom)
        self.assertEqual(shipping.parent_id | billing.parent_id, partner)
        self.assertEqual((shipping.type, billing.type), ('delivery', 'invoice'))
        self.assertEqual(self.work_contact.parent_id, self.company)
        self.assertEqual(self.work_contact.type, 'invoice')
        self.assertEqual((self.company.industry_id, self.work_contact.industry_id), (self.work, self.work))
        self.assertFalse(self.work_contact.is_shopify_customer)

        for customer in ({'id': '101'}, {'id': 101, 'email': 'different@example.com'}):
            self.assertEqual(self._order_partners(customer, addresses=addresses), (partner, shipping, billing))
        self.assertEqual(len(self.mappings._find_shopify_customer_mapping('101', self.instance.id)), 1)

    def test_customer_queue_web_and_pos_share_the_mapping_but_other_ids_and_stores_do_not(self):
        line = self.env['shopify.customer.data.queue.line.ept'].create(dict(
            shopify_instance_id=self.instance.id, shopify_synced_customer_data=json.dumps(self.customer)))
        line.customer_queue_commit_and_process(line.synced_customer_queue_id, self.instance)
        self.assertEqual(line.state, 'done')
        partner = self.mappings.search_shopify_partner('101', self.instance.id)
        self.assertEqual(self._order_partners(self.customer)[0], partner)
        self.assertEqual(self._order_partners({'id': 101}, pos=True)[0], partner)
        other_id = self._order_partners(dict(self.customer, id=102))[0]
        other_instance = self.env['shopify.instance.ept'].create(dict(name='Other Shopify Store',
            shopify_company_id=self.instance.shopify_company_id.id,
            shopify_warehouse_id=self.instance.shopify_warehouse_id.id,
            shopify_host='other-test-store.example.com', shopify_api_key='test-key',
            shopify_password='test-password', shopify_shared_secret='test-secret'))
        other_store = self._order_partners(self.customer, pos=True, instance=other_instance)[0]
        self.assertEqual(len(partner | other_id | other_store), 3)
        self.assertEqual((partner | other_id | other_store).mapped('email'), [self.email] * 3)

    def test_known_work_mapping_is_reported_for_review(self):
        self.work_contact.industry_id = self.ecom
        legacy = self.mappings.create(dict(shopify_customer_id='101', shopify_instance_id=self.instance.id,
                                           partner_id=self.work_contact.id))
        line = self.env['shopify.order.data.queue.line.ept'].create(dict(
            shopify_instance_id=self.instance.id, shopify_order_id='order-101'))
        self.assertEqual(self._order_partners(self.customer, pos=True, line=line), (False, False, False))
        self.assertEqual(line.state, 'failed')
        self.assertIn('Review the customer mapping', line.shopify_order_common_log_lines_ids.message)
        self.assertEqual(legacy.partner_id, self.work_contact)
        self.assertEqual(self.company.industry_id, self.work)

    def test_company_name_does_not_link_to_the_work_company(self):
        self.instance.import_customer_as_company = True
        address = dict(self.address, company=self.company.name)
        customer = dict(self.customer, default_address=address)
        addresses = dict(billing_address=address, shipping_address=address)
        partner, shipping, billing = self._order_partners(customer, addresses=addresses)

        self.assertTrue(partner.is_company)
        self.assertNotEqual(partner, self.company)
        self.assertEqual(partner.industry_id, self.ecom)
        self.assertEqual(partner.name, self.company.name)
        self.assertEqual(partner, self.mappings.search_shopify_partner('101', self.instance.id).parent_id)
        self.assertEqual(shipping.parent_id | billing.parent_id, partner)
        self.assertEqual(self._order_partners(customer, addresses=addresses), (partner, shipping, billing))
        self.assertEqual(self.company.industry_id, self.work)

    def test_duplicate_insert_does_not_create_an_orphan_customer(self):
        partner = self._order_partners(self.customer)[0]
        count = self.partners.search_count([('email', '=', self.email)])
        with mute_logger('odoo.sql_db'), \
                patch.object(type(self.mappings), '_find_shopify_customer_mapping', return_value=self.mappings):
            self.assertFalse(self.mappings.shopify_create_contact_partner(self.customer, self.instance))
        self.assertEqual(self.partners.search_count([('email', '=', self.email)]), count)
        self.assertEqual(self._order_partners(self.customer)[0], partner)

    def test_missing_id_does_not_create_a_shared_empty_mapping(self):
        self.assertFalse(self.mappings.shopify_create_contact_partner(dict(self.customer, id=None), self.instance))
        self.assertFalse(self.mappings.search([('shopify_instance_id', '=', self.instance.id)]))
