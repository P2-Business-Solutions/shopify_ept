# -*- coding: utf-8 -*-

import importlib.util
from pathlib import Path
import unittest


UTILS_PATH = (
    Path(__file__).resolve().parents[1] / "models" / "shopify_fulfillment_utils.py"
)
SPEC = importlib.util.spec_from_file_location("shopify_fulfillment_utils", UTILS_PATH)
UTILS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UTILS)


class ShopifyResource:
    def __init__(self, values):
        self.values = values

    def to_dict(self):
        return self.values


class TestShopifyFulfillmentUtils(unittest.TestCase):

    def test_normalize_fulfillment_orders_converts_shopify_resources(self):
        fulfillment_orders = [
            ShopifyResource({"id": 101, "assigned_location_id": 201}),
            {"id": 102, "assigned_location_id": 201},
        ]

        self.assertEqual(
            UTILS.normalize_fulfillment_orders(fulfillment_orders),
            [
                {"id": 101, "assigned_location_id": 201},
                {"id": 102, "assigned_location_id": 201},
            ],
        )

    def test_single_assigned_location_is_used_for_order_header(self):
        order_response = {
            "location_id": None,
            "fulfillments": [],
            "fulfillment_data": [
                {"id": 101, "assigned_location_id": 201},
                {"id": 102, "assigned_location_id": 201},
            ],
        }

        self.assertEqual(
            UTILS.get_single_order_location_id(order_response),
            "201",
        )

    def test_split_fulfillment_locations_do_not_set_ambiguous_header_location(self):
        order_response = {
            "location_id": None,
            "fulfillments": [],
            "fulfillment_data": [
                {"id": 101, "assigned_location_id": 201},
                {"id": 102, "assigned_location_id": 202},
            ],
        }

        self.assertFalse(
            UTILS.get_single_order_location_id(order_response)
        )

    def test_empty_fulfillment_orders_do_not_set_header_location(self):
        self.assertFalse(UTILS.get_single_order_location_id({}))

    def test_order_location_and_assigned_location_must_not_conflict(self):
        order_response = {
            "fulfillments": [{"location_id": 201}],
            "fulfillment_data": [{"assigned_location_id": 202}],
        }

        self.assertFalse(UTILS.get_single_order_location_id(order_response))

    def test_pos_order_location_takes_precedence(self):
        order_response = {
            "location_id": 301,
            "fulfillments": [{"location_id": 201}],
            "fulfillment_data": [{"assigned_location_id": 202}],
        }

        self.assertEqual(
            UTILS.get_single_order_location_id(order_response),
            "301",
        )


if __name__ == "__main__":
    unittest.main()
