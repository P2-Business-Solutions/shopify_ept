"""Map multiple Shopify shipping options to one operational delivery method."""
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .shopify_shipping_utils import shipping_mapping_key, shipping_mapping_keys


class ShopifyShippingMapping(models.Model):
    _name = "shopify.shipping.mapping.ept"
    _description = "Shopify Shipping Mapping"
    _rec_name = "match_value"
    _order = "instance_id, match_on, match_value, id"
    _check_company_auto = True

    active = fields.Boolean(default=True)
    instance_id = fields.Many2one("shopify.instance.ept", required=True, ondelete="cascade")
    company_id = fields.Many2one(
        "res.company", related="instance_id.shopify_company_id", store=True, readonly=True,
    )
    carrier_id = fields.Many2one(
        "delivery.carrier", string="Delivery Method", required=True, ondelete="restrict",
        check_company=True,
        domain="[('active', '=', True), '|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    source = fields.Char(
        string="Shipping Source",
        help="Optional exact shipping-line source. Leave blank to accept this option from any "
             "source, including regular Shopify and Collective. This is separate from the order's sales channel.",
    )
    match_on = fields.Selection(
        [("code", "Shipping Code"), ("title", "Shipping Name")],
        string="Match Using", required=True, default="code",
    )
    match_value = fields.Char(
        string="Shipping Code / Name", required=True,
        help="Code: exact shipping-line code. Name: shipping-line title, ignoring letter case. "
             "Surrounding spaces are ignored. For example, Free Shipping.",
    )
    mapping_key = fields.Char(compute="_compute_mapping_key", store=True)

    _sql_constraints = [
        ("unique_shipping_option", "unique(instance_id, mapping_key)",
         "This shipping source and code/name already has a mapping for this instance. "
         "Edit or reactivate the existing mapping, including archived mappings."),
    ]

    @api.depends("source", "match_on", "match_value")
    def _compute_mapping_key(self):
        for mapping in self:
            mapping.mapping_key = shipping_mapping_key(
                mapping.source, mapping.match_on, mapping.match_value,
            )

    @api.constrains("match_value")
    def _check_match_value(self):
        if any(not (mapping.match_value or "").strip() for mapping in self):
            raise ValidationError(_("Enter a shipping code or name to match."))

    @api.model
    def _find_carrier(self, line, instance):
        keys = shipping_mapping_keys(line)
        mappings = self.search([
            ("instance_id", "=", instance.id), ("active", "=", True),
            ("mapping_key", "in", keys),
        ])
        carriers = self.env["delivery.carrier"]
        for key in keys:
            matches = mappings.filtered(lambda mapping: mapping.mapping_key == key)
            if matches:
                # Validate archived targets too: a specific rule must never silently
                # fall through to a different service when its target is archived.
                return carriers._shopify_validate_carrier(matches.carrier_id, instance)
        return carriers
