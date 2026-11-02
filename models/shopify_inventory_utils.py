# -*- coding: utf-8 -*-

SHOPIFY_INVENTORY_BATCH_SIZE = 250

INVENTORY_LEVELS_QUERY = """
query InventoryLevels($ids: [ID!]!, $locationId: ID!) {
  location(id: $locationId) { id isActive }
  nodes(ids: $ids) {
    ... on InventoryItem {
      id
      tracked
      variant { id }
      inventoryLevel(locationId: $locationId) {
        location { id }
        quantities(names: ["available"]) { name quantity }
      }
    }
  }
}
"""

INVENTORY_ACTIVATE_MUTATION = """
mutation InventoryActivate($inventoryItemId: ID!, $locationId: ID!, $available: Int!, $idempotencyKey: String!) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId, available: $available)
    @idempotent(key: $idempotencyKey) {
    inventoryLevel { item { id } location { id } }
    userErrors { field message }
  }
}
"""

INVENTORY_SET_QUANTITIES_MUTATION = """
mutation InventorySet($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    inventoryAdjustmentGroup {
      createdAt
    }
    userErrors {
      code
      field
      message
    }
  }
}
"""


def chunked(records, size=SHOPIFY_INVENTORY_BATCH_SIZE):
    """Yield bounded lists without requiring callers to materialize slices."""
    batch = []
    for record in records:
        batch.append(record)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def shopify_gid(resource, numeric_id):
    prefix = "gid://shopify/%s/" % resource
    value = str(numeric_id or "")
    if value.startswith(prefix):
        value = value[len(prefix):]
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("Invalid Shopify %s ID: %s" % (resource, numeric_id))
    return prefix + value


def prepare_inventory_set_input(changes, reference_document_uri):
    """Prepare an absolute inventory update with Odoo as source of truth."""
    return {
        "name": "available",
        "reason": "correction",
        "referenceDocumentUri": reference_document_uri,
        "quantities": [
            {
                "inventoryItemId": shopify_gid(
                    "InventoryItem", change["inventory_item_id"]),
                "locationId": shopify_gid("Location", change["location_id"]),
                "quantity": int(change["quantity"]),
                "changeFromQuantity": change.get("change_from_quantity"),
            }
            for change in changes
        ],
    }


def inventory_user_error_indexes(user_errors):
    """Return failed quantity indexes and errors that apply to the whole batch."""
    failed_indexes = {}
    batch_errors = []
    for error in user_errors or []:
        field = error.get("field") or []
        index = None
        if "quantities" in field:
            position = field.index("quantities") + 1
            if position < len(field):
                try:
                    index = int(field[position])
                except (TypeError, ValueError):
                    index = None
        if index is None:
            batch_errors.append(error)
        else:
            failed_indexes.setdefault(index, []).append(error)
    return failed_indexes, batch_errors
