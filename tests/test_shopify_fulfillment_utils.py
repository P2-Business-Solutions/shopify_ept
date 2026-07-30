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


class Workflow:
    def __init__(
            self, workflow_id, validate_order=False, create_invoice=False,
            register_payment=False):
        self.id = workflow_id
        self.validate_order = validate_order
        self.create_invoice = create_invoice
        self.register_payment = register_payment


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

    def test_all_header_and_line_fulfillment_locations_are_returned(self):
        order_response = {
            "location_id": 301,
            "fulfillments": [{"location_id": 201}],
            "fulfillment_data": [
                {"assigned_location_id": 202},
                {"assigned_location_id": 203},
            ],
        }

        self.assertEqual(
            UTILS.get_order_location_ids(order_response),
            {"201", "202", "203", "301"},
        )

    def test_draft_workflow_wins_when_order_spans_locations(self):
        automatic_workflow = Workflow(
            10, validate_order=True, create_invoice=True,
            register_payment=True)
        draft_workflow = Workflow(20)

        self.assertIs(
            UTILS.select_least_automated_workflow(
                [automatic_workflow, draft_workflow]),
            draft_workflow,
        )

    def test_workflow_selection_prefers_no_invoice_after_confirmation(self):
        confirm_only_workflow = Workflow(10, validate_order=True)
        invoice_workflow = Workflow(
            20, validate_order=True, create_invoice=True)

        self.assertIs(
            UTILS.select_least_automated_workflow(
                [invoice_workflow, confirm_only_workflow]),
            confirm_only_workflow,
        )

    def test_empty_workflow_selection_returns_false(self):
        self.assertFalse(UTILS.select_least_automated_workflow([]))


if __name__ == "__main__":
    unittest.main()
