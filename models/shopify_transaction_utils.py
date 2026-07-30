# -*- coding: utf-8 -*-


def normalize_shopify_id(value):
    """Return a stable string representation for a Shopify resource ID."""
    if value in (None, False, ""):
        return ""
    return str(value)


def successful_order_transactions(transactions, kinds=None):
    """Return successful Shopify order transactions of the requested kinds."""
    kinds = set(kinds or ("sale", "capture"))
    return [
        transaction for transaction in (transactions or [])
        if transaction.get("status") == "success" and transaction.get("kind") in kinds
        and normalize_shopify_id(transaction.get("id"))
    ]


def find_transaction_id(transactions, kinds=None, amount=None, excluded_ids=None):
    """Find an unused successful transaction, preferring an exact amount match."""
    excluded_ids = {normalize_shopify_id(value) for value in (excluded_ids or [])}
    candidates = [
        transaction for transaction in successful_order_transactions(transactions, kinds)
        if normalize_shopify_id(transaction.get("id")) not in excluded_ids
    ]
    if amount is not None:
        expected_amount = round(float(amount), 2)
        amount_matches = [
            transaction for transaction in candidates
            if round(float(transaction.get("amount") or 0.0), 2) == expected_amount
        ]
        if not amount_matches:
            return ""
        candidates = amount_matches
    if not candidates:
        return ""
    candidates.sort(
        key=lambda transaction: (
            transaction.get("processed_at") or transaction.get("created_at") or "",
            normalize_shopify_id(transaction.get("id")),
        )
    )
    return normalize_shopify_id(candidates[-1].get("id"))
