# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from copy import deepcopy


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


def get_shopify_discount_codes(order_response, stored_codes=None):
    """Return Shopify discount codes in stable order without duplicates.

    Shopify normally exposes codes in ``discount_codes``. The discount
    applications and value stored on the sale order are fallbacks for webhook
    payloads that omit that collection.
    """
    codes = []
    response = order_response or {}

    for discount in response.get("discount_codes") or []:
        code = discount.get("code") if isinstance(discount, dict) else discount
        if code:
            codes.append(str(code).strip())

    for application in response.get("discount_applications") or []:
        code = application.get("code") if isinstance(application, dict) else False
        if code:
            codes.append(str(code).strip())

    if stored_codes:
        codes.extend(code.strip() for code in str(stored_codes).split(","))

    unique_codes = []
    seen = set()
    for code in filter(None, codes):
        normalized_code = code.casefold()
        if normalized_code not in seen:
            unique_codes.append(code)
            seen.add(normalized_code)
    return unique_codes


def get_shopify_discount_applications(order_response):
    """Return an independent copy of Shopify's ordered applications.

    REST discount allocations reference this collection by
    ``discount_application_index``. The order and every application attribute
    must therefore be retained exactly as Shopify supplied them.
    """
    response = order_response or {}
    return deepcopy(response.get("discount_applications") or [])


def get_shopify_line_discount_allocations(line):
    """Return an independent copy of a Shopify line's raw allocations.

    Keep the complete payload instead of selecting known keys so new Shopify
    attributes, stacked discounts, and both shop/presentment currency amounts
    remain available to downstream tax integrations.
    """
    return deepcopy((line or {}).get("discount_allocations") or [])


def get_shopify_discount_allocations_by_line_id(order_response):
    """Map Shopify product/shipping line IDs to supplied allocation arrays.

    Lines that omit ``discount_allocations`` are intentionally excluded so a
    partial webhook cannot erase a complete snapshot retained earlier. An
    explicitly supplied empty array is included and therefore clears stale
    allocations after an order edit.
    """
    response = order_response or {}
    source_lines = (
        (response.get("line_items") or [])
        + (response.get("shipping_lines") or [])
    )
    return {
        str(line.get("id")): get_shopify_line_discount_allocations(line)
        for line in source_lines
        if line.get("id") is not None and "discount_allocations" in line
    }


def get_shopify_order_fiscal_position_vals(fiscal_position_id):
    """Return an explicit order override only when one is configured.

    Omitting the key when no Shopify default exists preserves Odoo's normal
    partner-based fiscal-position computation.
    """
    if not fiscal_position_id:
        return {}
    return {"fiscal_position_id": fiscal_position_id}


def get_shopify_warehouse_hold_vals(hold_reason_id, hold_note=None):
    """Prepare the sales-order fields used by ``so_delivery_hold``.

    Keeping these technical field names in one helper makes the optional
    warehouse configuration explicit and prevents unconfigured warehouses from
    changing an order's existing hold state.
    """
    if not hold_reason_id:
        return {}
    return {
        "hold_reason_id": hold_reason_id,
        "hold_note": hold_note or False,
    }
