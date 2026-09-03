# -*- coding: utf-8 -*-


def find_duplicate_match_field(variants, match_by):
    """Return the duplicated field that makes variant matching ambiguous."""
    match_fields = []
    if match_by in ("sku", "sku_or_barcode"):
        match_fields.append("sku")
    if match_by in ("barcode", "sku_or_barcode"):
        match_fields.append("barcode")

    for field_name in match_fields:
        values = [
            variant.get(field_name)
            for variant in (variants or [])
            if variant.get(field_name)
        ]
        if len(values) != len(set(values)):
            return field_name

    return False


def get_relink_search_domains(match_by, sku, barcode, linked_sku, linked_barcode):
    """Return the product.product search domains to try when an already imported Shopify variant
    should be re-linked to another Odoo product.

    An empty list means the currently linked Odoo product still matches the variant (or there is
    nothing to match on), so the link must be kept.
    """
    domains = []
    if match_by in ("sku", "sku_or_barcode") and sku:
        if linked_sku == sku:
            return []
        domains.append([("default_code", "=", sku)])
    if match_by in ("barcode", "sku_or_barcode") and barcode:
        if linked_barcode == barcode:
            return []
        domains.append([("barcode", "=", barcode)])
    return domains
