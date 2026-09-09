# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = "account.payment"

    shopify_instance_id = fields.Many2one(
        "shopify.instance.ept", string="Shopify Instance", copy=False, index=True
    )
    shopify_order_transaction_id = fields.Char(
        string="Shopify Order Transaction ID", copy=False, index=True
    )
    shopify_cash_order_id = fields.Many2one('sale.order', string='Shopify Cash Order', copy=False,
                                          index=True, ondelete='restrict', readonly=True)
    shopify_parent_transaction_id = fields.Char(copy=False, readonly=True)
    shopify_cash_kind = fields.Selection([('sale', 'Sale'), ('capture', 'Capture'), ('refund', 'Refund')],
                                        copy=False, readonly=True)
    shopify_cash_gateway = fields.Char(copy=False, readonly=True)
    shopify_cash_timestamp = fields.Char(copy=False, readonly=True)

    _sql_constraints = [
        (
            "shopify_order_transaction_unique",
            "unique(shopify_instance_id, shopify_order_transaction_id)",
            "A Shopify order transaction can only be linked to one payment per instance.",
        )
    ]
