# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    shopify_auto_workflow_id = fields.Many2one(
        "sale.workflow.process.ept",
        string="Shopify Order Import Workflow",
        help=(
            "Overrides the payment gateway and financial-status workflow while an "
            "unfulfilled Shopify order assigned to this warehouse is imported or "
            "updated. Once Shopify reports fulfillment, the normal financial-status "
            "workflow resumes so invoices and payments can be processed."
        ),
    )
