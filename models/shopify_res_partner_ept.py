# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
from psycopg2.errors import UniqueViolation

from odoo import _, api, Command, fields, models, tools


class ShopifyResPartnerEpt(models.Model):
    _name = "shopify.res.partner.ept"
    _description = "Shopify Res Partner"

    partner_id = fields.Many2one("res.partner", ondelete="cascade")
    shopify_instance_id = fields.Many2one("shopify.instance.ept", "Instances")
    shopify_customer_id = fields.Char(index=True)
    # Enforce uniqueness on new imports without rewriting ambiguous legacy mappings.
    import_managed = fields.Boolean(copy=False, readonly=True)

    def init(self):
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS shopify_res_partner_ept_import_identity_uniq
            ON shopify_res_partner_ept (shopify_instance_id, shopify_customer_id)
            WHERE import_managed
        """)

    @api.model
    @tools.ormcache()
    def _get_ecom_customer_channel_id(self):
        """Resolve the Studio customer channel used for Shopify customers."""
        partner_obj = self.env["res.partner"]
        customer_channel_field = partner_obj._fields.get("x_studio_customer_channel")
        if not customer_channel_field or customer_channel_field.type != "many2one":
            return False

        customer_channel_obj = self.env[customer_channel_field.comodel_name]
        customer_channel_name_field = customer_channel_obj._rec_name
        domain = [(customer_channel_name_field, "=ilike", "Ecom")]
        if "x_active" in customer_channel_obj._fields:
            domain.append(("x_active", "=", True))

        return customer_channel_obj.sudo().search(domain, limit=1).id

    @api.model
    def _get_ecom_customer_channel_vals(self):
        customer_channel_id = self._get_ecom_customer_channel_id()
        return {"x_studio_customer_channel": customer_channel_id} if customer_channel_id else {}

    def _log_customer_import_failure(self, instance, queue_line, message):
        order_line = queue_line if self.env.context.get("order_data_queue") else False
        customer_line = queue_line if self.env.context.get("customer_data_queue") else False
        self.env["common.log.lines.ept"].create_common_log_line_ept(
            shopify_instance_id=instance.id, module="shopify_ept", model_name="res.partner",
            message=message, shopify_order_data_queue_line_id=order_line.id if order_line else False,
            shopify_customer_data_queue_line_id=customer_line.id if customer_line else False,
            order_ref=order_line.shopify_order_id if order_line else False,
        )
        return False

    def _accepts_shopify_orders(self, partner, instance, channel_vals):
        commercial = partner.commercial_partner_id
        company = instance.shopify_company_id
        if (not partner.active or not commercial.active
                or (partner | commercial).company_id - company):
            return False
        return all(commercial[field].id == channel_id and partner[field].id in (False, channel_id)
                   for field, channel_id in channel_vals.items())

    def shopify_create_contact_partner(self, vals, instance, queue_line=None, *, historical_partner=None):
        """Resolve the Shopify identity before using any customer details to create a contact."""
        customer_id = str(vals.get("id") or "").strip()
        if not customer_id:
            return self._log_customer_import_failure(instance, queue_line, _(
                "Shopify did not provide a customer ID. Verify the customer in Shopify and retry the import."))

        partner_obj = self.env["res.partner"]
        channel_vals = self._get_ecom_customer_channel_vals()
        mappings = self._find_shopify_customer_mapping(customer_id, instance.id)
        if mappings:
            if len(mappings) != 1 or not self._accepts_shopify_orders(mappings.partner_id, instance, channel_vals):
                return self._log_customer_import_failure(instance, queue_line, _(
                    "Shopify customer %(customer)s has an ambiguous, inactive, other-company, or non-Ecom mapping. "
                    "Review the customer mapping before retrying this import.", customer=customer_id))
            partner = mappings.partner_id
            if "tags" in vals:
                tag_ids = [partner_obj.create_or_search_tag(tag) for tag in (vals.get("tags") or "").split(",") if tag]
                partner.category_id = [Command.set(tag_ids)]
            return partner

        email = vals.get("email") or False
        phone = vals.get("phone") or False
        name = " ".join(filter(None, (vals.get("first_name"), vals.get("last_name")))).strip() or email or phone
        if not name and not historical_partner:
            return self._log_customer_import_failure(instance, queue_line, _(
                "Shopify did not provide a name, email, or phone for customer %(customer)s. "
                "Verify the customer in Shopify and retry the import.", customer=customer_id))

        try:
            with self.env.cr.savepoint():
                mapping = self.create({
                    "shopify_instance_id": instance.id,
                    "shopify_customer_id": customer_id,
                    "import_managed": True,
                })
                mapping.partner_id = historical_partner or self._create_shopify_contact(
                    vals, instance, name, email, phone, channel_vals)
        except UniqueViolation as error:
            if error.diag.constraint_name != "shopify_res_partner_ept_import_identity_uniq":
                raise
            return self._log_customer_import_failure(instance, queue_line, _(
                "Shopify customer %(customer)s was mapped by another import. Retry to use the new mapping.",
                customer=customer_id))
        return mapping.partner_id

    def _create_shopify_contact(self, vals, instance, name, email, phone, channel_vals):
        partner_obj = self.env["res.partner"]
        address = vals.get("default_address") or {}
        tag_ids = [partner_obj.create_or_search_tag(tag) for tag in (vals.get("tags") or "").split(",") if tag]
        partner_vals = self.shopify_prepare_partner_vals(address, instance)
        partner_vals.update(channel_vals, name=name, email=email, customer_rank=1, is_shopify_customer=True,
                            type="contact", category_id=[Command.set(tag_ids)],
                            phone=partner_vals.get("phone") or phone)
        if instance.import_customer_as_company and address.get("company"):
            company = partner_obj.create(dict(channel_vals, name=address["company"], email=email,
                                              is_company=True, customer_rank=1, is_shopify_customer=True))
            partner_vals["parent_id"] = company.id
        return partner_obj.create(partner_vals)

    def _find_shopify_customer_mapping(self, customer_id, instance_id):
        return self.search([("shopify_customer_id", "=", customer_id),
                            ("shopify_instance_id", "=", instance_id)], limit=2)

    def search_shopify_partner(self, shopify_customer_id, shopify_instance_id):
        """ This method is used to search the shopify partner.
            :param shopify_customer_id: Id of shopify customer which receive from customer response.
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 27 October 2020 .
            Task_id: 167537
        """
        if shopify_customer_id:
            mappings = self._find_shopify_customer_mapping(str(shopify_customer_id), shopify_instance_id)
            if len(mappings) == 1:
                return mappings.partner_id
        return False

    @api.model
    def shopify_create_or_update_address(self, instance, shopify_customer_data, parent_partner, partner_type="contact"):
        """
        Creates or updates existing partner from Shopify customer's data.
        @author: Maulik Barad on Date 09-Sep-2020.
        """
        partner_obj = self.env["res.partner"]
        parent_partner = parent_partner.commercial_partner_id

        first_name = shopify_customer_data.get("first_name")
        last_name = shopify_customer_data.get("last_name")

        if not first_name and not last_name:
            return False

        company_name = shopify_customer_data.get("company") if not instance.import_customer_as_company else False
        partner_vals = self.shopify_prepare_partner_vals(shopify_customer_data)
        address_key_list = ["name", "street", "street2", "city", "zip", "phone", "state_id", "country_id"]

        if company_name:
            address_key_list.append("company_name")
            partner_vals["company_name"] = company_name

        partner = partner_obj._find_partner_ept(partner_vals, address_key_list,
                                                [("parent_id", "=", parent_partner.id), ("type", "=", partner_type)])

        if partner:
            if parent_partner.email and partner.email != parent_partner.email:
                partner.write({'email': parent_partner.email})
            return partner

        partner_vals.update({"type": partner_type, "parent_id": parent_partner.id})
        if parent_partner.email:
            partner_vals.update({'email': parent_partner.email})
        partner = partner_obj.create(partner_vals)
        if company_name:
            partner.company_name = company_name
        return partner

    def shopify_prepare_partner_vals(self, vals, instance=False):
        """
        This method used to prepare a partner vals.
        @param : self,vals
        @return: partner_vals
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 29 August 2020 .
        Task_id: 165956
        @change : pass lang on vals by Nilam Kubavat for task id : 190111 at 19/05/2022
        """
        partner_obj = self.env["res.partner"]

        first_name = vals.get("first_name")
        last_name = vals.get("last_name")
        #name = "%s %s" % (first_name, last_name)

        name = ""
        if first_name:
            name = "%s" % first_name
        if last_name:
            name += " %s" % last_name if name else "%s" % last_name

        zipcode = vals.get("zip")
        state_code = vals.get("province_code")

        country_code = vals.get("country_code")
        country = partner_obj.get_country(country_code)

        state = partner_obj.create_or_update_state_ept(country_code, state_code, zipcode, country)

        partner_vals = {
            "email": vals.get("email") or False,
            "name": name,
            "phone": vals.get("phone"),
            "street": vals.get("address1"),
            "street2": vals.get("address2"),
            "city": vals.get("city"),
            "zip": zipcode,
            "state_id": state and state.id or False,
            "country_id": country and country.id or False,
            "is_company": False,
            'lang': instance.shopify_lang_id.code if instance != False else None,
        }
        update_partner_vals = partner_obj.remove_special_chars_from_partner_vals(partner_vals)
        return update_partner_vals
