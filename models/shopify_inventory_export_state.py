# -*- coding: utf-8 -*-

from odoo import fields, models


class ShopifyInventoryExportState(models.Model):
    _name = "shopify.inventory.export.state.ept"
    _description = "Last Successful Shopify Inventory Export"
    _rec_name = "shopify_product_id"

    shopify_instance_id = fields.Many2one(
        "shopify.instance.ept", required=True, ondelete="cascade", index=True,
    )
    shopify_product_id = fields.Many2one(
        "shopify.product.product.ept", required=True, ondelete="cascade", index=True,
    )
    inventory_item_id = fields.Char(required=True, index=True)
    location_id = fields.Char(required=True, index=True)
    quantity = fields.Integer(required=True)
    last_exported_at = fields.Datetime(required=True, default=fields.Datetime.now)

    _sql_constraints = [
        (
            "shopify_inventory_export_state_unique",
            "unique(shopify_instance_id, shopify_product_id, location_id)",
            "Only one inventory export state may exist per Shopify product and location.",
        ),
    ]
