# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.


def is_removed_order_line(line):
    """Return whether Shopify reports an order line as fully removed."""
    current_quantity = line.get("current_quantity")
    if current_quantity is None:
        return False

    try:
        return float(current_quantity) <= 0
    except (TypeError, ValueError):
        return False


def filter_importable_order_lines(lines):
    """Exclude line items that remain in Shopify payloads after removal."""
    return [
        line for line in (lines or [])
        if not is_removed_order_line(line)
    ]
