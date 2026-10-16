"""Database coverage for shipping mapping constraints, access, and sale lines."""
import unittest
import json

try:
    from odoo.exceptions import AccessError, UserError, ValidationError
    from odoo.tests import TransactionCase, tagged
    from odoo.tests.common import new_test_user
    from odoo.tools import mute_logger
    from psycopg2 import IntegrityError
except ModuleNotFoundError:
    TransactionCase = unittest.TestCase
    ODOO_AVAILABLE = False

    def tagged(*_tags):
        return lambda test_class: test_class
else:
    ODOO_AVAILABLE = True


@tagged("post_install", "-at_install")
@unittest.skipUnless(ODOO_AVAILABLE, "Odoo test runtime is not available")
class TestShopifyShippingMappingORM(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env["product.product"].create({"name": "Mapped Shipping", "type": "service"})
        cls.carrier = cls.env["delivery.carrier"].create({
            "name": "Mapped UPS Ground", "product_id": cls.product.id,
            "delivery_type": "fixed", "shopify_tracking_company": "UPS",
        })
        cls.instance = cls.env["shopify.instance.ept"].create({
            "name": "Shipping Mapping Test", "shopify_company_id": cls.env.company.id,
            "shopify_warehouse_id": cls.env["stock.warehouse"].search([
                ("company_id", "=", cls.env.company.id),
            ], limit=1).id,
            "shopify_api_key": "test-key", "shopify_password": "test-password",
            "shopify_shared_secret": "test-secret", "shopify_host": "shipping-test.example.com",
            "shipping_product_id": cls.product.id,
        })
        cls.mapping_model = cls.env["shopify.shipping.mapping.ept"]
        cls.warranty_type, cls.repair_type = cls.env["sale.order.type"].create([
            {"name": "Shipping Warranty Test", "code": "SHIP_WARRANTY", "company_id": cls.env.company.id},
            {"name": "Shipping Repair Test", "code": "SHIP_REPAIR", "company_id": cls.env.company.id},
        ])
        cls.warranty_carrier = cls.carrier.copy({"name": "Warranty Shipping Test"})
        cls.repair_carrier = cls.carrier.copy({"name": "Repair Shipping Test"})

    def order_type_mapping(self, tag="Warranty", order_type=None, carrier=None):
        return self.env["shopify.order.type.mapping.ept"].create({
            "instance_id": self.instance.id, "shopify_tag": tag,
            "order_type_id": (order_type or self.warranty_type).id,
            "delivery_carrier_id": (carrier or self.warranty_carrier).id,
        })

    def mapping(self, **values):
        return self.mapping_model.create({
            "instance_id": self.instance.id, "carrier_id": self.carrier.id,
            "match_on": "title", "match_value": "Free Shipping", **values,
        })

    def test_real_recordset_shares_one_carrier_across_shipping_sources(self):
        self.mapping()
        before = self.carrier.read(["shopify_source", "shopify_code", "write_date"])
        for line in (
            {"source": "shopify", "code": "Free Shipping", "title": "Free Shipping"},
            {"source": "test-partner-source", "code": "different", "title": "FREE SHIPPING"},
            {"title": "Free Shipping"},
        ):
            self.assertEqual(self.env["delivery.carrier"].shopify_search_create_delivery_carrier(
                line, self.instance), self.carrier)
        self.assertEqual(self.carrier.read(["shopify_source", "shopify_code", "write_date"]), before)

    def test_database_rejects_duplicate_wildcard_rules_after_normalization(self):
        self.mapping()
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError):
            self.mapping(match_value=" free shipping ", source=" ")
            self.env.flush_all()

    def test_archived_mapping_keeps_its_key_for_reactivation(self):
        self.mapping(active=False)
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError):
            self.mapping()
            self.env.flush_all()

    def test_multiple_source_specific_rules_can_share_a_delivery_method(self):
        self.mapping(source="shopify")
        self.mapping(source="test-partner-source")
        self.assertEqual(len(self.carrier.shopify_shipping_mapping_ids), 2)

    def test_whitespace_match_cannot_route_every_shipping_option(self):
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.mapping(match_value="  ")

    def test_archived_method_is_rejected_even_with_active_test_disabled(self):
        self.mapping()
        self.carrier.active = False
        with self.assertRaisesRegex(UserError, "archived"):
            self.env["delivery.carrier"].with_context(active_test=False).shopify_search_create_delivery_carrier(
                {"title": "Free Shipping"}, self.instance)

    def test_disabled_alias_is_not_used_with_active_test_disabled(self):
        self.mapping(active=False)
        with self.assertRaisesRegex(UserError, "No delivery method"):
            self.env["delivery.carrier"].with_context(active_test=False).shopify_search_create_delivery_carrier(
                {"title": "Free Shipping"}, self.instance)

    def test_different_company_carrier_cannot_be_configured(self):
        company = self.env["res.company"].create({"name": "Other Shipping Company"})
        other_carrier = self.carrier.copy({"company_id": company.id})
        with self.assertRaises(UserError), self.cr.savepoint():
            self.mapping(carrier_id=other_carrier.id)

    def test_user_can_read_but_cannot_edit_mappings(self):
        mapping = self.mapping()
        user = new_test_user(self.env, login="shipping_mapping_reader", groups="base.group_user")
        self.assertEqual(mapping.with_user(user).carrier_id, self.carrier)
        with self.assertRaises(AccessError):
            mapping.with_user(user).write({"match_value": "Express"})

    def test_zero_charge_shipping_line_uses_the_selected_carrier_product(self):
        self.mapping()
        partner = self.env["res.partner"].create({"name": "Shipping Test Customer"})
        order = self.env["sale.order"].create({
            "partner_id": partner.id, "shopify_instance_id": self.instance.id,
        })
        order.create_shopify_shipping_lines({
            "currency": self.env.company.currency_id.name,
            "shipping_lines": [{"id": 123, "title": "Free Shipping", "price": "0.00"}],
        }, self.instance)
        self.assertEqual(order.carrier_id, self.carrier)
        delivery_line = order.order_line.filtered("is_delivery")
        self.assertEqual(len(delivery_line), 1)
        self.assertEqual(delivery_line.product_id, self.product)
        self.assertEqual(delivery_line.price_unit, 0)
        self.assertEqual(delivery_line.shopify_line_id, "123")

    def test_free_shipping_refresh_replaces_an_uninvoiced_generic_product_once(self):
        self.mapping()
        partner = self.env["res.partner"].create({"name": "Shipping Refresh Customer"})
        generic_product = self.product.copy({"name": "Generic Shipping"})
        order = self.env["sale.order"].create({
            "partner_id": partner.id, "shopify_instance_id": self.instance.id,
        })
        delivery_line = self.env["sale.order.line"].create({
            "order_id": order.id, "product_id": generic_product.id,
            "name": "Generic Shipping", "is_delivery": True,
            "product_uom_qty": 1, "price_unit": 0, "shopify_line_id": "123",
        })
        payload = {"shipping_lines": [{"id": 123, "title": "Free Shipping", "price": "0.00"}]}
        order.update_shopify_shipping_lines_ept(self.instance, payload)
        self.assertEqual(order.carrier_id, self.carrier)
        self.assertEqual(delivery_line.product_id, self.product)
        self.assertEqual(delivery_line.price_unit, 0)
        self.assertFalse(order.update_shopify_shipping_lines_ept(self.instance, payload))
        self.assertEqual(len(order.order_line.filtered("is_delivery")), 1)

    def test_unmapped_import_keeps_payload_and_creates_no_partial_sales_order(self):
        payload = {"name": "#SHIPPING-UNMAPPED", "shipping_lines": [{
            "source": "test-partner-source", "code": "Unknown", "title": "Unmapped Test Shipping",
        }]}
        queue = self.env["shopify.order.data.queue.ept"].create({
            "shopify_instance_id": self.instance.id, "queue_type": "unshipped", "created_by": "import",
        })
        queue_line = self.env["shopify.order.data.queue.line.ept"].create({
            "shopify_order_data_queue_id": queue.id, "shopify_instance_id": self.instance.id,
            "shopify_order_id": "999000123", "name": payload["name"], "order_data": json.dumps(payload),
        })
        orders = self.env["sale.order"]
        before = orders.search_count([])
        self.assertFalse(orders.shopify_create_order(
            self.instance, False, False, False, queue_line, payload, [], "SHIPPING-UNMAPPED"))
        self.assertEqual(orders.search_count([]), before)
        self.assertEqual(queue_line.state, "failed")
        self.assertEqual(json.loads(queue_line.order_data), payload)
        self.assertIn("Shipping Mappings", queue_line.shopify_order_common_log_lines_ids.mapped("message")[0])

    def test_existing_tag_mappings_route_shared_free_shipping_by_resolved_type(self):
        self.mapping()
        self.order_type_mapping()
        self.order_type_mapping("Repair", self.repair_type, self.repair_carrier)
        orders = self.env["sale.order"]
        for tags, carrier in (("Warranty", self.warranty_carrier), ("Repair", self.repair_carrier),
                              ("Unmapped Tag", self.carrier)):
            for source in ("shopify", "test-partner-source", None):
                with self.subTest(tags=tags, source=source):
                    result = orders._shopify_resolve_shipping_lines({
                        "tags": tags, "shipping_lines": [{"source": source, "title": "Free Shipping"}],
                    }, self.instance)
                    self.assertEqual(result[0][1], carrier)

    def test_tag_sequence_still_controls_order_type_and_delivery_method_together(self):
        warranty = self.order_type_mapping()
        repair = self.order_type_mapping("Repair", self.repair_type, self.repair_carrier)
        warranty.sequence, repair.sequence = 20, 5
        result = self.env["sale.order"]._shopify_resolve_shipping_lines({
            "tags": "Warranty, Repair", "shipping_lines": [{"title": "Free Shipping"}],
        }, self.instance)
        self.assertEqual(result[0][1], self.repair_carrier)

    def test_multiple_tags_for_same_type_can_share_one_method(self):
        self.order_type_mapping()
        second = self.order_type_mapping("Warranty Alternate")
        second.delivery_carrier_id = False
        self.assertEqual(self.env["shopify.order.type.mapping.ept"]._get_delivery_carrier(
            self.instance, self.warranty_type), self.warranty_carrier)

    def test_different_methods_for_one_order_type_are_rejected(self):
        self.order_type_mapping()
        with self.assertRaises(ValidationError):
            self.order_type_mapping("Warranty Alternate", carrier=self.repair_carrier)

    def test_no_type_method_uses_general_shipping_mapping(self):
        self.mapping()
        mapping = self.order_type_mapping()
        mapping.delivery_carrier_id = False
        result = self.env["sale.order"]._shopify_resolve_shipping_lines({
            "tags": "Warranty", "shipping_lines": [{"title": "Free Shipping"}],
        }, self.instance)
        self.assertEqual(result[0][1], self.carrier)

    def test_archived_type_method_never_falls_through_to_retail_shipping(self):
        self.mapping()
        self.order_type_mapping()
        self.warranty_carrier.active = False
        with self.assertRaisesRegex(UserError, "archived"):
            self.env["sale.order"]._shopify_resolve_shipping_lines({
                "tags": "Warranty", "shipping_lines": [{"title": "Free Shipping"}],
            }, self.instance)

    def test_existing_order_refresh_uses_saved_type_and_keeps_free_shipping(self):
        self.mapping()
        self.order_type_mapping()
        self.order_type_mapping("Repair", self.repair_type, self.repair_carrier)
        partner = self.env["res.partner"].create({"name": "Order Type Shipping Customer"})
        order = self.env["sale.order"].create({
            "partner_id": partner.id, "shopify_instance_id": self.instance.id,
            "order_type_id": self.repair_type.id,
        })
        payload = {"tags": "Warranty", "shipping_lines": [{"id": 123, "title": "Free Shipping", "price": "0.00"}]}
        order.create_shopify_shipping_lines(payload, self.instance)
        self.assertEqual(order.carrier_id, self.repair_carrier)
        self.assertEqual(order.order_type_id, self.repair_type)
        self.assertEqual(order.order_line.filtered("is_delivery").price_unit, 0)
        order.order_type_id = self.warranty_type
        order.update_shopify_shipping_lines_ept(self.instance, payload)
        self.assertEqual(order.carrier_id, self.warranty_carrier)
        self.assertEqual(order.order_line.filtered("is_delivery").price_unit, 0)

    def test_type_method_cannot_belong_to_another_company(self):
        company = self.env["res.company"].create({"name": "Other Type Shipping Company"})
        other_carrier = self.carrier.copy({"company_id": company.id})
        with self.assertRaises(UserError):
            self.order_type_mapping(carrier=other_carrier)

    def test_operational_user_can_read_type_method_without_editing_tag_rules(self):
        mapping = self.order_type_mapping()
        user = new_test_user(self.env, login="order_type_shipping_reader", groups="stock.group_stock_user")
        self.assertEqual(self.env["shopify.order.type.mapping.ept"].with_user(user)._get_delivery_carrier(
            self.instance, self.warranty_type), self.warranty_carrier)
        with self.assertRaises(AccessError):
            mapping.with_user(user).write({"delivery_carrier_id": self.repair_carrier.id})
