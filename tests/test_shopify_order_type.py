# -*- coding: utf-8 -*-

import unittest

try:
    from odoo.tests import TransactionCase, tagged
except ModuleNotFoundError:  # Allow the standalone utility test suite to run.
    TransactionCase = unittest.TestCase

    def tagged(*_tags):
        return lambda test_class: test_class

    ODOO_AVAILABLE = False
else:
    ODOO_AVAILABLE = True


@tagged("post_install", "-at_install")
@unittest.skipUnless(ODOO_AVAILABLE, "Odoo test runtime is not available")
class TestShopifyOrderType(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.env.company.id)],
            limit=1,
        )
        cls.repair_order_type = cls.env["sale.order.type"].create(
            {
                "name": "Shopify Repair Test",
                "code": "SHOPIFY_REPAIR_TEST",
                "company_id": cls.env.company.id,
            }
        )
        cls.mapped_order_type = cls.env["sale.order.type"].create(
            {
                "name": "Shopify Wholesale Test",
                "code": "SHOPIFY_WHOLESALE_TEST",
                "company_id": cls.env.company.id,
            }
        )
        cls.instance = cls.env["shopify.instance.ept"].create(
            {
                "name": "Order Type Test Store",
                "shopify_company_id": cls.env.company.id,
                "shopify_warehouse_id": cls.warehouse.id,
                "shopify_api_key": "test-key",
                "shopify_password": "test-password",
                "shopify_shared_secret": "test-secret",
                "shopify_host": "order-type-test.example.com",
                "shopify_default_order_type_id": cls.repair_order_type.id,
            }
        )
        cls.env["shopify.order.type.mapping.ept"].create(
            {
                "instance_id": cls.instance.id,
                "shopify_tag": "Wholesale",
                "order_type_id": cls.mapped_order_type.id,
            }
        )

    def test_instance_default_is_used_without_matching_tag(self):
        order_type = self.env["sale.order"]._get_shopify_order_type(
            {"tags": "VIP"},
            self.instance,
        )

        self.assertEqual(order_type, self.repair_order_type)

    def test_tag_mapping_takes_precedence_over_instance_default(self):
        order_type = self.env["sale.order"]._get_shopify_order_type(
            {"tags": "Wholesale"},
            self.instance,
        )

        self.assertEqual(order_type, self.mapped_order_type)
