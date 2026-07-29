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


if __name__ == "__main__":
    unittest.main()
