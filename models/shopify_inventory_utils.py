# -*- coding: utf-8 -*-

SHOPIFY_INVENTORY_BATCH_SIZE = 250

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
    return "gid://shopify/%s/%s" % (resource, numeric_id)


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
                "changeFromQuantity": None,
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
