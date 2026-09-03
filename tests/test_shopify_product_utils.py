# -*- coding: utf-8 -*-

import importlib.util
from pathlib import Path
import unittest


UTILS_PATH = (
    Path(__file__).resolve().parents[1] / "models" / "shopify_product_utils.py"
)
SPEC = importlib.util.spec_from_file_location("shopify_product_utils", UTILS_PATH)
UTILS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UTILS)


class TestShopifyProductUtils(unittest.TestCase):

    def setUp(self):
        self.variants = [
            {"id": 101, "sku": "KEEPER", "barcode": "100000000001"},
            {"id": 102, "sku": "KEEPER", "barcode": "100000000002"},
        ]

    def test_barcode_matching_allows_duplicate_skus(self):
        self.assertFalse(
            UTILS.find_duplicate_match_field(self.variants, "barcode")
        )

    def test_barcode_matching_rejects_duplicate_barcodes(self):
        self.variants[1]["barcode"] = self.variants[0]["barcode"]

        self.assertEqual(
            UTILS.find_duplicate_match_field(self.variants, "barcode"),
            "barcode",
        )

    def test_sku_matching_rejects_duplicate_skus(self):
        self.assertEqual(
            UTILS.find_duplicate_match_field(self.variants, "sku"),
            "sku",
        )

    def test_sku_matching_ignores_duplicate_barcodes(self):
        self.variants[1]["sku"] = "KEEPER-2"
        self.variants[1]["barcode"] = self.variants[0]["barcode"]

        self.assertFalse(
            UTILS.find_duplicate_match_field(self.variants, "sku")
        )

    def test_combined_matching_keeps_sku_first_validation(self):
        self.assertEqual(
            UTILS.find_duplicate_match_field(self.variants, "sku_or_barcode"),
            "sku",
        )


if __name__ == "__main__":
    unittest.main()


class TestRelinkSearchDomains(unittest.TestCase):

    def test_keeps_link_when_barcode_still_matches(self):
        self.assertEqual(
            UTILS.get_relink_search_domains("barcode", "SKU-A", "111", "SKU-B", "111"), [])

    def test_barcode_changed_searches_by_new_barcode_only(self):
        self.assertEqual(
            UTILS.get_relink_search_domains("barcode", "SKU-A", "222", "SKU-A", "111"),
            [[("barcode", "=", "222")]])

    def test_sku_changed_searches_by_new_sku(self):
        self.assertEqual(
            UTILS.get_relink_search_domains("sku", "SKU-NEW", "111", "SKU-OLD", "111"),
            [[("default_code", "=", "SKU-NEW")]])

    def test_sku_or_barcode_keeps_link_when_either_matches(self):
        self.assertEqual(
            UTILS.get_relink_search_domains("sku_or_barcode", "SKU-NEW", "111", "SKU-OLD", "111"), [])
        self.assertEqual(
            UTILS.get_relink_search_domains("sku_or_barcode", "SKU-A", "999", "SKU-A", "111"), [])

    def test_sku_or_barcode_tries_sku_then_barcode(self):
        self.assertEqual(
            UTILS.get_relink_search_domains("sku_or_barcode", "SKU-NEW", "222", "SKU-OLD", "111"),
            [[("default_code", "=", "SKU-NEW")], [("barcode", "=", "222")]])

    def test_missing_identifier_keeps_link(self):
        self.assertEqual(UTILS.get_relink_search_domains("barcode", "SKU-A", "", "SKU-A", "111"), [])
        self.assertEqual(UTILS.get_relink_search_domains("sku", None, "111", "SKU-A", "111"), [])
