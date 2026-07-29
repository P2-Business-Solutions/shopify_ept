# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ShopifyOrderTypeMapping(models.Model):
    _name = "shopify.order.type.mapping.ept"
    _description = "Shopify Order Tag to Order Type Mapping"
    _order = "sequence, id"
    _check_company_auto = True

    sequence = fields.Integer(
        default=10,
        help="When an order has multiple mapped tags, the lowest sequence wins.",
    )
    instance_id = fields.Many2one(
        "shopify.instance.ept",
        required=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        "res.company",
        related="instance_id.shopify_company_id",
        store=True,
        readonly=True,
    )
    shopify_tag = fields.Char(
        string="Shopify Tag",
        required=True,
        help="Exact Shopify order tag to match. Matching ignores case and surrounding spaces.",
    )
    order_type_id = fields.Many2one(
        "sale.order.type",
        string="Order Type",
        required=True,
        ondelete="restrict",
        check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )

    @api.constrains("instance_id", "shopify_tag")
    def _check_unique_normalized_tag(self):
        for mapping in self:
            normalized_tag = (mapping.shopify_tag or "").strip().casefold()
            if not normalized_tag:
                raise ValidationError(_("The Shopify tag cannot be empty."))

            other_mappings = self.search([
                ("instance_id", "=", mapping.instance_id.id),
                ("id", "!=", mapping.id),
            ])
            if any(
                (other.shopify_tag or "").strip().casefold() == normalized_tag
                for other in other_mappings
            ):
                raise ValidationError(
                    _("A mapping for this Shopify tag already exists on the instance.")
                )
