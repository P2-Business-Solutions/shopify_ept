# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    shopify_order_hold_reason_id = fields.Many2one(
        "sale.order.hold.reason",
        string="Shopify Order Hold Reason",
        ondelete="restrict",
        help=(
            "Automatically places imported Shopify orders assigned to this "
            "warehouse on delivery hold. The sales order can still be confirmed, "
            "invoiced, and paid, but its open outbound deliveries cannot be "
            "reserved or validated until the hold is released."
        ),
    )
    shopify_order_hold_note = fields.Text(
        string="Shopify Order Hold Note",
        help="Optional note copied to the Shopify sales order delivery hold.",
    )
