# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class ShopifyDiscountCodeConfig(models.Model):
    _name = "shopify.discount.code.config.ept"
    _description = "Shopify Discount Code Analytic Configuration"

    instance_id = fields.Many2one("shopify.instance.ept", required=True, ondelete="cascade")
    discount_code = fields.Char("Discount Code Prefix", required=True,
                                help="Prefix to match against Shopify discount codes (case-insensitive). "
                                     "For example, 'EMPLOYEE' will match 'EMPLOYEE10', 'EMPLOYEE20', etc. "
                                     "An exact code like 'SUMMER20' will only match that specific code. "
                                     "If multiple prefixes match, the longest (most specific) match wins.")
    analytic_account_id = fields.Many2one("account.analytic.account",
                                          string="Analytic Account", required=True,
                                          help="Analytic account for discount lines and free-product COGS entries "
                                               "matching this discount code prefix.")

    _sql_constraints = [
        ('unique_code_per_instance', 'unique(instance_id, discount_code)',
         'Discount code must be unique per Shopify instance.')
    ]
