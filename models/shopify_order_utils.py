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


def normalize_shopify_tags(tags):
    """Return normalized Shopify order tags for exact comparisons."""
    if isinstance(tags, str):
        tags = tags.split(",")
    return {
        str(tag).strip().casefold()
        for tag in (tags or [])
        if str(tag).strip()
    }


def find_matching_shopify_tag(tags, configured_tags):
    """Return the first configured tag present on the Shopify order."""
    normalized_tags = normalize_shopify_tags(tags)
    return next(
        (
            configured_tag
            for configured_tag in (configured_tags or [])
            if str(configured_tag).strip().casefold() in normalized_tags
        ),
        False,
    )
