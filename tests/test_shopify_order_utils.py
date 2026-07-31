# -*- coding: utf-8 -*-

import importlib.util
from pathlib import Path
import unittest


UTILS_PATH = (
    Path(__file__).resolve().parents[1] / "models" / "shopify_order_utils.py"
)
SPEC = importlib.util.spec_from_file_location("shopify_order_utils", UTILS_PATH)
UTILS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UTILS)


class TestShopifyOrderUtils(unittest.TestCase):

    def test_removed_order_lines_are_excluded(self):
        lines = [
            {"id": 101, "quantity": 2, "current_quantity": 2},
            {"id": 102, "quantity": 1, "current_quantity": 0},
        ]

        self.assertEqual(
            UTILS.filter_importable_order_lines(lines),
            [lines[0]],
        )

    def test_string_zero_current_quantity_is_removed(self):
        self.assertTrue(
            UTILS.is_removed_order_line({"current_quantity": "0"})
        )

    def test_payloads_without_current_quantity_remain_importable(self):
        line = {"id": 101, "quantity": 1}

        self.assertEqual(
            UTILS.filter_importable_order_lines([line]),
            [line],
        )

    def test_missing_lines_are_handled(self):
        self.assertEqual(UTILS.filter_importable_order_lines(None), [])

    def test_shopify_tags_are_normalized(self):
        self.assertEqual(
            UTILS.normalize_shopify_tags(" Wholesale,VIP , wholesale "),
            {"wholesale", "vip"},
        )

    def test_first_configured_matching_tag_wins(self):
        self.assertEqual(
            UTILS.find_matching_shopify_tag(
                "Wholesale, VIP",
                ["vip", "wholesale"],
            ),
            "vip",
        )

    def test_order_type_tag_matching_is_exact(self):
        self.assertFalse(
            UTILS.find_matching_shopify_tag(
                "Wholesale Customer",
                ["Wholesale"],
            )
        )

    def test_missing_shopify_tags_do_not_match(self):
        self.assertFalse(
            UTILS.find_matching_shopify_tag(None, ["Wholesale"])
        )

    def test_discount_codes_preserve_shopify_order_and_remove_duplicates(self):
        order_response = {
            "discount_codes": [{"code": "EMPLOYEE20"}],
            "discount_applications": [
                {"code": "employee20"},
                {"code": "VIP50"},
            ],
        }

        self.assertEqual(
            UTILS.get_shopify_discount_codes(
                order_response, "VIP50,LEGACY10"),
            ["EMPLOYEE20", "VIP50", "LEGACY10"],
        )

    def test_discount_codes_fall_back_to_stored_order_value(self):
        self.assertEqual(
            UTILS.get_shopify_discount_codes({}, "EMPLOYEE20, VIP50"),
            ["EMPLOYEE20", "VIP50"],
        )

    def test_discount_applications_preserve_order_and_all_attributes(self):
        applications = [
            {
                "type": "automatic",
                "allocation_method": "across",
                "target_type": "line_item",
                "title": "BOGO",
            },
            {
                "type": "discount_code",
                "code": "VIP50",
                "value": "50.0",
                "value_type": "percentage",
            },
        ]

        retained = UTILS.get_shopify_discount_applications(
            {"discount_applications": applications}
        )

        self.assertEqual(retained, applications)
        self.assertIsNot(retained, applications)
        self.assertIsNot(retained[0], applications[0])

    def test_line_discount_allocations_preserve_stacking_and_money_sets(self):
        allocations = [
            {
                "amount": "10.00",
                "amount_set": {
                    "shop_money": {"amount": "10.00", "currency_code": "USD"},
                    "presentment_money": {
                        "amount": "13.50",
                        "currency_code": "CAD",
                    },
                },
                "discount_application_index": 0,
            },
            {
                "amount": "5.00",
                "amount_set": {
                    "shop_money": {"amount": "5.00", "currency_code": "USD"},
                    "presentment_money": {
                        "amount": "6.75",
                        "currency_code": "CAD",
                    },
                },
                "discount_application_index": 1,
                "future_shopify_attribute": "preserve-me",
            },
        ]

        retained = UTILS.get_shopify_line_discount_allocations(
            {"discount_allocations": allocations}
        )

        self.assertEqual(retained, allocations)
        self.assertIsNot(retained, allocations)
        self.assertIsNot(retained[0]["amount_set"], allocations[0]["amount_set"])

    def test_missing_discount_source_data_is_normalized_to_empty_lists(self):
        self.assertEqual(UTILS.get_shopify_discount_applications({}), [])
        self.assertEqual(UTILS.get_shopify_line_discount_allocations({}), [])

    def test_allocations_are_mapped_for_product_and_shipping_lines(self):
        order_response = {
            "line_items": [
                {
                    "id": 101,
                    "discount_allocations": [
                        {"amount": "20.00", "discount_application_index": 0}
                    ],
                },
                {"id": 102},
            ],
            "shipping_lines": [
                {"id": 201, "discount_allocations": []},
            ],
        }

        self.assertEqual(
            UTILS.get_shopify_discount_allocations_by_line_id(order_response),
            {
                "101": [
                    {"amount": "20.00", "discount_application_index": 0}
                ],
                "201": [],
            },
        )

    def test_configured_shopify_fiscal_position_overrides_order_default(self):
        self.assertEqual(
            UTILS.get_shopify_order_fiscal_position_vals(42),
            {"fiscal_position_id": 42},
        )

    def test_missing_shopify_fiscal_position_preserves_odoo_behavior(self):
        self.assertEqual(
            UTILS.get_shopify_order_fiscal_position_vals(False),
            {},
        )


if __name__ == "__main__":
    unittest.main()
