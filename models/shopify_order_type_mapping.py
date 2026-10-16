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
    delivery_carrier_id = fields.Many2one(
        "delivery.carrier", string="Delivery Method", ondelete="restrict", check_company=True,
        domain="[('active', '=', True), '|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Optional delivery method for this order type on this Shopify instance. "
             "Overrides shipping-option mappings on import and manual refresh, while keeping Shopify's shipping charge. "
             "All tags mapped to the same order type share one delivery method. Leave all of them blank "
             "to use the general shipping mappings.",
    )

    @api.constrains("instance_id", "order_type_id", "delivery_carrier_id")
    def _check_order_type_delivery_method(self):
        for mapping in self:
            carriers = self.search([
                ("instance_id", "=", mapping.instance_id.id),
                ("order_type_id", "=", mapping.order_type_id.id),
            ]).mapped("delivery_carrier_id")
            if len(carriers) > 1:
                raise ValidationError(_(
                    "Choose one delivery method for order type %(order_type)s on this Shopify instance. "
                    "All tags mapped to that order type must use the same method (or leave it blank).",
                    order_type=mapping.order_type_id.display_name,
                ))

    @api.model
    def _get_delivery_carrier(self, instance, order_type):
        carriers = self.env["delivery.carrier"]
        if order_type:
            carriers = self.search([
                ("instance_id", "=", instance.id), ("order_type_id", "=", order_type.id),
            ]).mapped("delivery_carrier_id")
        if carriers:
            return self.env["delivery.carrier"]._shopify_validate_carrier(
                carriers, instance, configuration=_("Order Type Mapping"))
        return carriers

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
