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


if __name__ == "__main__":
    unittest.main()
