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
