# -*- coding: utf-8 -*-

import importlib.util
from pathlib import Path
import unittest

UTILS_PATH = (
    Path(__file__).resolve().parents[1] / "models" / "shopify_inventory_utils.py"
)
SPEC = importlib.util.spec_from_file_location("shopify_inventory_utils", UTILS_PATH)
UTILS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UTILS)


class TestShopifyInventoryUtils(unittest.TestCase):

    def test_chunked_uses_shopify_input_limit(self):
        batches = list(UTILS.chunked(range(501)))

        self.assertEqual([len(batch) for batch in batches], [250, 250, 1])

    def test_prepare_inventory_set_input(self):
        result = UTILS.prepare_inventory_set_input([{
            "inventory_item_id": "123",
            "location_id": "456",
            "quantity": -2,
        }], "odoo://shopify/inventory-export/7")

        self.assertEqual(result["name"], "available")
        self.assertEqual(result["quantities"], [{
            "inventoryItemId": "gid://shopify/InventoryItem/123",
            "locationId": "gid://shopify/Location/456",
            "quantity": -2,
            "changeFromQuantity": None,
        }])

    def test_mutation_uses_shopify_idempotency_directive(self):
        self.assertIn("$idempotencyKey: String!", UTILS.INVENTORY_SET_QUANTITIES_MUTATION)
        self.assertIn("@idempotent(key: $idempotencyKey)", UTILS.INVENTORY_SET_QUANTITIES_MUTATION)

    def test_inventory_user_errors_are_mapped_to_quantity_indexes(self):
        indexed, batch = UTILS.inventory_user_error_indexes([
            {
                "field": ["input", "quantities", "1", "quantity"],
                "message": "Invalid quantity",
            },
            {
                "field": ["input", "reason"],
                "message": "Invalid reason",
            },
        ])

        self.assertEqual(indexed[1][0]["message"], "Invalid quantity")
        self.assertEqual(batch[0]["message"], "Invalid reason")
