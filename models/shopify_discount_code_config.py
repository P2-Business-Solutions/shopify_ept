# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class ShopifyDiscountCodeConfig(models.Model):
    _name = "shopify.discount.code.config.ept"
    _description = "Shopify Discount Code Analytic Configuration"

    instance_id = fields.Many2one("shopify.instance.ept", required=True, ondelete="cascade")
    discount_code = fields.Char("Discount Code", required=True,
                                help="Exact Shopify discount code (case-insensitive match)")
    analytic_account_id = fields.Many2one("account.analytic.account",
                                          string="Analytic Account", required=True,
                                          help="Analytic account for discount lines and free-product COGS entries "
                                               "using this discount code.")

    _sql_constraints = [
        ('unique_code_per_instance', 'unique(instance_id, discount_code)',
         'Discount code must be unique per Shopify instance.')
    ]
