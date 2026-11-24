# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, api

class ResPartner(models.Model):
    _inherit = "res.partner"

    is_shopify_customer = fields.Boolean(string="Is Shopify Customer?", default=False,
                                         help="Used for identified that the customer is imported from Shopify store.")

    @api.model
    def create_shopify_pos_customer(self, order_response, instance, queue_line=None):
        return self.env["shopify.res.partner.ept"].shopify_create_contact_partner(
            order_response.get("customer") or {}, instance, queue_line)

    def create_or_search_tag(self, tag):
        """
        :param tag:
        :return:
        """
        res_partner_category_obj = self.env['res.partner.category']

        exists_tag = res_partner_category_obj.search([('name', '=ilike', tag)], limit=1)

        if not exists_tag:
            exists_tag = res_partner_category_obj.sudo().create({'name': tag})
        return exists_tag.id
