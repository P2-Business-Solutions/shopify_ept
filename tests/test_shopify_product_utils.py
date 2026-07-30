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
