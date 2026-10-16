"""Shipping resolution, import failures, and refresh behavior without an Odoo DB."""
import ast
from datetime import datetime
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "shopify_shipping_utils", ROOT / "models" / "shopify_shipping_utils.py",
)
UTILS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UTILS)


def translate(message, *args, **kwargs):
    return message % (kwargs or args) if args or kwargs else message


def load_methods(filename, names):
    tree = ast.parse((ROOT / "models" / filename).read_text())
    model = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods = [node for node in model.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for method in methods:
        method.decorator_list = []
    namespace = {
        "UserError": ValueError, "_": translate, "datetime": datetime,
        "_logger": logging.getLogger(__name__),
        "shipping_mapping_keys": UTILS.shipping_mapping_keys,
        "float_is_zero": lambda value, precision_digits: round(value, precision_digits) == 0,
        "float_compare": lambda first, second, precision_digits: round(first - second, precision_digits),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
                 filename, "exec"), namespace)
    return {name: namespace[name] for name in names}


class Records(list):
    def __getitem__(self, key):
        result = super().__getitem__(key)
        return Records(result) if isinstance(key, slice) else result

    def filtered(self, predicate):
        if isinstance(predicate, str):
            return Records(row for row in self if getattr(row, predicate))
        return Records(row for row in self if predicate(row))

    def write(self, values):
        for row in self:
            row.write(values)
        return True

    def mapped(self, name):
        result = Records()
        for row in self:
            for value in getattr(row, name):
                if value not in result:
                    result.append(value)
        return result

    def __getattr__(self, name):
        if name == "carrier_id":
            result = Records()
            for row in self:
                for carrier in row.carrier_id:
                    if carrier not in result:
                        result.append(carrier)
            return result
        if len(self) != 1:
            raise AssertionError("Expected a singleton for %s" % name)
        return getattr(self[0], name)


Carrier = type("Carrier", (), load_methods("delivery_carrier.py", {
    "shopify_search_create_delivery_carrier", "_shopify_validate_carrier",
    "search_carrier_for_webhook_fulfillment",
}))
Carrier.__len__ = lambda self: 0
Mapping = type("Mapping", (), load_methods("shopify_shipping_mapping.py", {"_find_carrier"}))
OrderTypes = type("OrderTypes", (), load_methods("shopify_order_type_mapping.py", {"_get_delivery_carrier"}))
Order = type("Order", (), load_methods("sale_order.py", {
    "_shopify_resolve_shipping_lines", "shopify_create_order", "create_shopify_shipping_lines",
    "update_shopify_shipping_lines_ept",
}))
Order.__bool__ = lambda self: getattr(self, "is_existing", True)


class TestShopifyShippingMapping(unittest.TestCase):
    def setUp(self):
        self.company = SimpleNamespace(id=1)
        self.warranty = SimpleNamespace(id=41, display_name="Warranty")
        self.repair = SimpleNamespace(id=42, display_name="Repair")
        self.instance = SimpleNamespace(
            id=10, name="Test Store", shopify_company_id=self.company,
            shipping_product_id=SimpleNamespace(id=100, name="Shipping"), order_visible_currency=False,
        )
        self.ups = self.carrier(6, "UPS Ground", "UPS")
        self.fedex = self.carrier(69, "FedEx Warranty", "FedEx")
        self.carriers = Carrier()
        self.carriers.search = Mock(return_value=Records())
        self.carriers.create = Mock(side_effect=AssertionError("Do not create delivery methods"))
        self.mappings = Mapping()
        self.order_types = OrderTypes()
        self.rows = []
        self.type_rows = []
        self.mappings.search = Mock(side_effect=self.search_mappings)
        self.order_types.search = Mock(side_effect=lambda domain: self.search_rows(self.type_rows, domain))
        self.log = Mock()
        self.env = {"delivery.carrier": self.carriers, "shopify.shipping.mapping.ept": self.mappings,
                    "shopify.order.type.mapping.ept": self.order_types, "common.log.lines.ept": self.log}
        self.carriers.env = self.mappings.env = self.order_types.env = self.env
        self.line = {"id": 123, "source": "shopify", "code": "Free Shipping",
                     "title": "Free Shipping", "price": "0.00"}

    def carrier(self, identity, name, company_name):
        return Records([SimpleNamespace(
            id=identity, name=name, active=True, company_id=False,
            shopify_tracking_company=company_name,
            product_id=SimpleNamespace(id=200 + identity, name=name),
        )])

    def search_mappings(self, domain):
        return self.search_rows(self.rows, domain)

    def search_rows(self, rows, domain):
        return Records(row for row in rows if all(
            getattr(row, field) in value if operator == "in" else getattr(row, field) == value
            for field, operator, value in domain
        ))

    def mapping(self, carrier=None, source=None, match_on="code", value="Free Shipping", instance_id=10,
                order_type=None):
        if order_type:
            row = SimpleNamespace(instance_id=instance_id, order_type_id=order_type.id,
                                  delivery_carrier_id=carrier or self.ups)
            self.type_rows.append(row)
            return row
        row = SimpleNamespace(
            instance_id=instance_id, active=True, carrier_id=carrier or self.ups,
            mapping_key=UTILS.shipping_mapping_key(source, match_on, value),
        )
        self.rows.append(row)
        return row

    def resolve(self, order_type=None, **changes):
        return self.carriers.shopify_search_create_delivery_carrier(
            dict(self.line, **changes), self.instance, order_type=order_type,
        )

    def order(self, order_type=None):
        order = Order()
        order.env = self.env
        order.name = "#144726"
        order.order_type_id = order_type or False
        order._get_shopify_order_type = Mock(return_value=order_type or False)
        order.ensure_one = Mock()
        order.write = Mock()
        order.shopify_create_sale_order_line = Mock()
        order._get_shopify_discount_allocation_amount = Mock(return_value=0)
        order._apply_discount_code_analytic_account = Mock()
        order.message_post = Mock()
        return order

    def test_multiple_explicit_sources_share_one_carrier(self):
        self.mapping(source="shopify")
        self.mapping(source="external-partner")
        self.assertEqual(self.resolve(), self.ups)
        self.assertEqual(self.resolve(source="external-partner"), self.ups)
        self.carriers.search.assert_not_called()

    def test_same_free_shipping_option_routes_by_order_type(self):
        self.mapping()
        self.mapping(carrier=self.fedex, order_type=self.warranty)
        repair_carrier = self.carrier(70, "UPS Ground Repair", "UPS")
        self.mapping(carrier=repair_carrier, order_type=self.repair)
        self.assertEqual(self.resolve(), self.ups)
        self.assertEqual(self.resolve(order_type=self.warranty), self.fedex)
        self.assertEqual(self.resolve(order_type=self.repair), repair_carrier)

    def test_type_specific_name_overrides_a_general_code_rule(self):
        self.mapping(source="shopify")
        self.mapping(carrier=self.fedex, match_on="title", order_type=self.warranty)
        self.assertEqual(self.resolve(order_type=self.warranty), self.fedex)
        self.assertEqual(self.resolve(order_type=self.repair), self.ups)

    def test_multiple_tags_for_one_order_type_share_a_carrier_across_sources(self):
        self.mapping(order_type=self.warranty)
        self.mapping(order_type=self.warranty)
        self.assertEqual(self.resolve(order_type=self.warranty), self.ups)
        self.assertEqual(self.resolve(order_type=self.warranty, source="partner"), self.ups)

    def test_conflicting_type_methods_are_detected_at_resolution(self):
        self.mapping(order_type=self.warranty)
        self.mapping(carrier=self.fedex, order_type=self.warranty)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.resolve(order_type=self.warranty)

    def test_order_type_with_no_carrier_uses_general_shipping_mapping(self):
        self.mapping()
        self.type_rows.append(SimpleNamespace(instance_id=10, order_type_id=self.warranty.id,
                                             delivery_carrier_id=Records()))
        self.assertEqual(self.resolve(order_type=self.warranty), self.ups)

    def test_type_delivery_method_is_scoped_to_its_shopify_instance(self):
        self.mapping()
        self.mapping(carrier=self.fedex, order_type=self.warranty, instance_id=11)
        self.assertEqual(self.resolve(order_type=self.warranty), self.ups)

    def test_other_order_type_rules_do_not_leak_into_general_or_unmatched_orders(self):
        self.mapping(carrier=self.fedex, order_type=self.warranty)
        for order_type in (None, self.repair):
            with self.subTest(order_type=order_type), self.assertRaisesRegex(ValueError, "No delivery method"):
                self.resolve(order_type=order_type)

    def test_type_specific_archived_target_blocks_instead_of_using_retail_fallback(self):
        self.mapping()
        self.mapping(carrier=self.fedex, order_type=self.warranty)
        self.fedex[0].active = False
        with self.assertRaisesRegex(ValueError, "archived"):
            self.resolve(order_type=self.warranty)

    def test_untyped_keys_keep_the_previous_stored_format(self):
        self.assertEqual(UTILS.shipping_mapping_key("shopify", "title", "Free Shipping"),
                         '["shopify", "title", "free shipping"]')

    def test_new_order_preflight_resolves_order_type_from_payload(self):
        self.mapping()
        self.mapping(carrier=self.fedex, order_type=self.warranty)
        orders = self.order(self.warranty)
        orders.is_existing = False
        payload = {"tags": "Warranty", "shipping_lines": [self.line]}
        result = orders._shopify_resolve_shipping_lines(payload, self.instance)
        self.assertEqual(result[0][1], self.fedex)
        orders._get_shopify_order_type.assert_called_once_with(payload, self.instance)

    def test_existing_order_respects_its_saved_type_without_reclassifying_from_tags(self):
        self.mapping(carrier=self.fedex, order_type=self.repair)
        self.mapping(order_type=self.warranty)
        order = self.order(self.repair)
        result = order._shopify_resolve_shipping_lines({
            "tags": "Warranty", "shipping_lines": [self.line],
        }, self.instance)
        self.assertEqual(result[0][1], self.fedex)
        order._get_shopify_order_type.assert_not_called()

    def test_fulfillment_fallback_receives_order_type(self):
        self.mapping(match_on="title")
        self.mapping(match_on="title", carrier=self.fedex, order_type=self.warranty)
        result = self.carriers.search_carrier_for_webhook_fulfillment(
            self.instance, {"service": "Free Shipping"}, order_type=self.warranty,
        )
        self.assertEqual(result, self.fedex)

    def test_shipping_name_without_source_handles_different_codes_and_missing_identifiers(self):
        self.mapping(match_on="title")
        for source, code in (("shopify", "Free Shipping"), ("partner", "partner-ground"), (None, None)):
            with self.subTest(source=source, code=code):
                self.assertEqual(self.resolve(source=source, code=code), self.ups)

    def test_code_and_source_specificity_have_documented_priority(self):
        self.mapping(match_on="title", carrier=self.fedex, source="shopify")
        self.mapping()
        self.assertEqual(self.resolve(), self.ups)
        self.mapping(source="shopify", carrier=self.fedex)
        self.assertEqual(self.resolve(), self.fedex)
        self.assertEqual(self.resolve(source="partner"), self.ups)

    def test_source_specific_name_overrides_global_name(self):
        self.mapping(match_on="title")
        self.mapping(match_on="title", source="partner", carrier=self.fedex)
        self.assertEqual(self.resolve(source="partner"), self.fedex)
        self.assertEqual(self.resolve(), self.ups)

    def test_names_ignore_case_and_spaces_but_codes_and_sources_stay_exact(self):
        self.mapping(match_on="title", value=" FREE SHIPPING ")
        self.assertEqual(self.resolve(title="free shipping"), self.ups)
        self.rows.clear()
        self.mapping(source="shopify", value="Free Shipping")
        self.assertEqual(self.resolve(source=" shopify ", code=" Free Shipping "), self.ups)
        with self.assertRaisesRegex(ValueError, "No delivery method"):
            self.resolve(code="free shipping")
        with self.assertRaisesRegex(ValueError, "No delivery method"):
            self.resolve(source="Shopify")

    def test_mappings_are_scoped_to_instance_and_active_state(self):
        self.mapping(instance_id=11)
        disabled = self.mapping()
        disabled.active = False
        with self.assertRaisesRegex(ValueError, "No delivery method"):
            self.resolve()

    def test_archived_target_never_falls_through_to_another_service(self):
        self.mapping()
        self.mapping(source="shopify", carrier=self.fedex)
        self.fedex[0].active = False
        with self.assertRaisesRegex(ValueError, "archived"):
            self.resolve()

    def test_company_mismatch_blocks_a_stale_mapping(self):
        self.mapping()
        self.ups[0].company_id = SimpleNamespace(id=2)
        with self.assertRaisesRegex(ValueError, "another company"):
            self.resolve()

    def test_explicit_mapping_overrides_conflicting_legacy_fields_without_writes(self):
        self.mapping(match_on="title")
        self.carriers.search.return_value = Records(self.ups + self.fedex)
        self.assertEqual(self.resolve(), self.ups)
        self.carriers.search.assert_not_called()
        self.carriers.create.assert_not_called()

    def test_unambiguous_legacy_mapping_is_supported_with_company_and_active_scope(self):
        self.carriers.search.return_value = self.ups
        self.assertEqual(self.resolve(), self.ups)
        domain = self.carriers.search.call_args.args[0]
        self.assertIn(("active", "=", True), domain)
        self.assertIn(("company_id", "in", [1, False]), domain)
        self.assertNotIn("limit", self.carriers.search.call_args.kwargs)

    def test_legacy_tracking_company_remains_a_fallback_for_exact_shipping_code(self):
        self.carriers.search.side_effect = [Records(), self.ups]
        self.assertEqual(self.resolve(code="UPS"), self.ups)
        self.assertIn(("shopify_tracking_company", "=", "UPS"), self.carriers.search.call_args.args[0])

    def test_legacy_duplicates_are_reported_with_method_names(self):
        self.carriers.search.return_value = Records(self.ups + self.fedex)
        with self.assertRaisesRegex(ValueError, "Conflicting.*UPS Ground.*FedEx Warranty"):
            self.resolve()

    def test_unmapped_option_does_not_fall_back_to_name_or_create_a_carrier(self):
        with self.assertRaisesRegex(ValueError, "source='shopify'.*code='Free Shipping'.*name='Free Shipping'"):
            self.resolve()
        self.carriers.create.assert_not_called()
        self.assertTrue(all("name" not in str(call.args[0]) for call in self.carriers.search.call_args_list))

    def test_unmapped_import_fails_before_order_creation_and_preserves_queue_payload(self):
        order = self.order()
        order.create = Mock(side_effect=AssertionError("No partial order"))
        queue = SimpleNamespace(id=42, order_data="original payload", write=Mock())
        result = order.shopify_create_order(
            self.instance, None, None, None, queue,
            {"name": "#144726", "shipping_lines": [self.line]}, [], 144726,
        )
        self.assertFalse(result)
        order.create.assert_not_called()
        self.assertEqual(queue.write.call_args.args[0]["state"], "failed")
        self.assertNotIn("order_data", queue.write.call_args.args[0])
        self.assertEqual(queue.order_data, "original payload")
        self.assertIn("Shipping Mappings", self.log.create_common_log_line_ept.call_args.kwargs["message"])

    def test_free_shipping_uses_carrier_product_and_keeps_zero_price(self):
        self.mapping(match_on="title")
        order = self.order()
        order.create_shopify_shipping_lines({"shipping_lines": [self.line]}, self.instance)
        order.write.assert_called_once_with({"carrier_id": self.ups.id})
        args = order.shopify_create_sale_order_line.call_args.args
        self.assertEqual(args[1], self.ups.product_id)
        self.assertEqual(float(args[4]), 0)

    def test_all_shipping_options_are_validated_before_any_line_is_created(self):
        self.mapping()
        order = self.order()
        with self.assertRaises(ValueError):
            order.create_shopify_shipping_lines({"shipping_lines": [
                self.line, dict(self.line, code="Unknown", title="Unknown"),
            ]}, self.instance)
        order.write.assert_not_called()
        order.shopify_create_sale_order_line.assert_not_called()

    def test_manual_refresh_also_validates_all_options_before_changing_carrier(self):
        self.mapping()
        order = self.order()
        order.carrier_id = self.fedex
        order.order_line = Records()
        with self.assertRaises(ValueError):
            order.update_shopify_shipping_lines_ept(self.instance, {"shipping_lines": [
                self.line, dict(self.line, code="Unknown", title="Unknown"),
            ]})
        self.assertIs(order.carrier_id, self.fedex)

    def refresh_order(self, invoiced=0):
        self.mapping(match_on="title")
        order = self.order()
        order.carrier_id = self.fedex
        line = SimpleNamespace(
            is_delivery=True, shopify_line_id="123", qty_invoiced=invoiced,
            price_unit=0, product_id=self.fedex.product_id, write=Mock(),
        )
        order.order_line = Records([line])
        pickings = Records(SimpleNamespace(
            state=state, location_dest_id=SimpleNamespace(usage=usage), write=Mock(),
        ) for state, usage in (("assigned", "customer"), ("done", "customer"),
                              ("cancel", "customer"), ("assigned", "internal")))
        order.picking_ids = pickings
        return order, line, pickings

    def test_refresh_corrects_free_product_and_only_updates_open_outbound_deliveries(self):
        order, line, pickings = self.refresh_order()
        self.assertTrue(order.update_shopify_shipping_lines_ept(
            self.instance, {"shipping_lines": [self.line]},
        ))
        self.assertEqual(order.carrier_id, self.ups)
        line.write.assert_any_call({"price_unit": 0.0, "product_id": self.ups.product_id.id,
                                    "shopify_line_id": "123"})
        pickings[0].write.assert_called_once_with({"carrier_id": self.ups.id})
        for picking in pickings[1:]:
            picking.write.assert_not_called()

    def test_refresh_preserves_an_invoiced_delivery_product(self):
        order, line, _pickings = self.refresh_order(invoiced=1)
        order.update_shopify_shipping_lines_ept(self.instance, {"shipping_lines": [self.line]})
        line.write.assert_called_once_with({"name": "Free Shipping"})
        self.assertEqual(line.product_id, self.fedex.product_id)

    def test_refresh_is_repeatable_when_carrier_product_and_price_already_match(self):
        order, line, pickings = self.refresh_order()
        order.carrier_id = self.ups
        line.product_id = self.ups.product_id
        for _ in range(2):
            self.assertFalse(order.update_shopify_shipping_lines_ept(
                self.instance, {"shipping_lines": [self.line]},
            ))
        order.shopify_create_sale_order_line.assert_not_called()
        for picking in pickings:
            picking.write.assert_not_called()

    def test_fulfillment_preserves_selected_ups_service(self):
        carrier = self.carriers.search_carrier_for_webhook_fulfillment(
            self.instance, {"tracking_company": "UPS", "service": "manual"}, self.ups,
        )
        self.assertIs(carrier, self.ups)
        self.carriers.search.assert_not_called()

    def test_fulfillment_without_tracking_preserves_selected_service(self):
        self.assertIs(self.carriers.search_carrier_for_webhook_fulfillment(
            self.instance, {}, self.ups,
        ), self.ups)

    def test_fulfillment_cannot_choose_between_multiple_ups_services(self):
        self.carriers.search.return_value = Records(self.ups + self.carrier(75, "UPS Ground Saver", "UPS"))
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.carriers.search_carrier_for_webhook_fulfillment(self.instance, {"tracking_company": "UPS"})

    def test_unknown_fulfillment_service_does_not_create_delivery_methods(self):
        self.assertFalse(self.carriers.search_carrier_for_webhook_fulfillment(
            self.instance, {"service": "manual"},
        ))
        self.carriers.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
