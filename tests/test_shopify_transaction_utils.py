# -*- coding: utf-8 -*-

import importlib.util
from pathlib import Path
import unittest


UTILS_PATH = (
    Path(__file__).resolve().parents[1] / "models" / "shopify_transaction_utils.py"
)
SPEC = importlib.util.spec_from_file_location("shopify_transaction_utils", UTILS_PATH)
UTILS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UTILS)


class TestShopifyTransactionUtils(unittest.TestCase):

    def test_finds_successful_charge_by_amount(self):
        transactions = [
            {"id": 101, "kind": "sale", "status": "success", "amount": "15.00"},
            {"id": 102, "kind": "capture", "status": "success", "amount": "25.00"},
        ]

        self.assertEqual(
            UTILS.find_transaction_id(transactions, amount=25),
            "102",
        )

    def test_ignores_failed_and_authorization_transactions(self):
        transactions = [
            {"id": 101, "kind": "sale", "status": "failure", "amount": "25.00"},
            {"id": 102, "kind": "authorization", "status": "success", "amount": "25.00"},
        ]

        self.assertEqual(UTILS.find_transaction_id(transactions, amount=25), "")

    def test_excludes_already_linked_transactions(self):
        transactions = [
            {"id": 101, "kind": "sale", "status": "success", "amount": "25.00"},
            {"id": 102, "kind": "capture", "status": "success", "amount": "25.00"},
        ]

        self.assertEqual(
            UTILS.find_transaction_id(
                transactions, amount=25, excluded_ids={"102"}
            ),
            "101",
        )

    def test_does_not_guess_when_amount_does_not_match(self):
        transactions = [
            {"id": 101, "kind": "sale", "status": "success", "amount": "25.00"},
        ]

        self.assertEqual(
            UTILS.find_transaction_id(transactions, amount=20),
            "",
        )

    def test_finds_refund_transaction(self):
        transactions = [
            {"id": 201, "kind": "refund", "status": "success", "amount": "8.50"},
        ]

        self.assertEqual(
            UTILS.find_transaction_id(transactions, kinds=("refund",)),
            "201",
        )


if __name__ == "__main__":
    unittest.main()
