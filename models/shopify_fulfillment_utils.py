# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.


def normalize_fulfillment_orders(fulfillment_orders):
    """Convert Shopify resources to serializable fulfillment-order dictionaries."""
    return [
        fulfillment_order.to_dict()
        if hasattr(fulfillment_order, "to_dict") else fulfillment_order
        for fulfillment_order in (fulfillment_orders or [])
    ]


def get_single_assigned_location_id(fulfillment_orders):
    """Return the assigned location when every fulfillment order uses one location."""
    assigned_location_ids = {
        str(fulfillment_order.get("assigned_location_id"))
        for fulfillment_order in (fulfillment_orders or [])
        if fulfillment_order.get("assigned_location_id")
    }
    return assigned_location_ids.pop() if len(assigned_location_ids) == 1 else False


def get_order_location_ids(order_response):
    """Return every Shopify location represented by an order."""
    order_response = order_response or {}
    location_ids = set()

    if order_response.get("location_id"):
        location_ids.add(str(order_response["location_id"]))

    location_ids.update(
        str(fulfillment["location_id"])
        for fulfillment in order_response.get("fulfillments", [])
        if fulfillment.get("location_id")
    )
    location_ids.update(
        str(fulfillment_order["assigned_location_id"])
        for fulfillment_order in order_response.get("fulfillment_data", [])
        if fulfillment_order.get("assigned_location_id")
    )
    return location_ids


def get_single_order_location_id(order_response):
    """Return the one unambiguous Shopify location represented by an order."""
    if order_response.get("location_id"):
        return str(order_response["location_id"])

    fulfillment_location_ids = get_order_location_ids(order_response)
    assigned_location_id = get_single_assigned_location_id(
        order_response.get("fulfillment_data", []))
    if assigned_location_id:
        fulfillment_location_ids.add(assigned_location_id)

    return (
        fulfillment_location_ids.pop()
        if len(fulfillment_location_ids) == 1 else False
    )


def select_least_automated_workflow(workflows):
    """Choose the safest workflow when an order spans configured warehouses."""
    workflows = list(workflows or [])
    if not workflows:
        return False

    return min(
        workflows,
        key=lambda workflow: (
            bool(workflow.validate_order),
            bool(workflow.create_invoice),
            bool(workflow.register_payment),
            workflow.id,
        ),
    )
