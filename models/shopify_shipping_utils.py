"""Stable shipping identifiers shared by configuration and order imports."""
import json


def shipping_mapping_key(source, match_on, value):
    """Keep service codes exact; customer-facing names ignore letter case."""
    value = (value or "").strip()
    if match_on == "title":
        value = value.casefold()
    return json.dumps([(source or "").strip(), match_on, value])


def shipping_mapping_keys(line):
    """Code before name, with a source-specific rule before an unrestricted one."""
    source = (line.get("source") or "").strip()
    keys = []
    for match_on, field in (("code", "code"), ("title", "title")):
        value = (line.get(field) or "").strip()
        if not value:
            continue
        if source:
            keys.append(shipping_mapping_key(source, match_on, value))
        keys.append(shipping_mapping_key(False, match_on, value))
    return keys
