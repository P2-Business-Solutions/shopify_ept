# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import secrets


class ShopifyAuthProcessEpt(models.TransientModel):
    redirect_url = fields.Char(string='Redirect URLs', readonly=True, compute='_compute_urls')
    app_url = fields.Char(string='App URL', readonly=True, compute='_compute_urls')

    @api.depends('shopify_host')
    def _compute_urls(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for rec in self:
            rec.redirect_url = f"{base_url}/ept_shopify/oauth/callback"
            rec.app_url = f"{base_url}/ept_shopify/launch"

    show_help = fields.Boolean(string='Show Help', default=False)
    _name = 'shopify.auth.process.ept'
    _description = 'Shopify OAuth process to Link Generator Wizard'

    shopify_host = fields.Char(
        string="Shopify Store Domain",
        help="Enter your Shopify store domain, e.g., 'mystore.myshopify.com'. Must not include 'https://' or any path."
    )
    shopify_client_id = fields.Char(
        string="Shopify API Key (Client ID)",
        help="Enter the API Key (Client ID) from your Shopify custom app."
    )
    shopify_secret_id = fields.Char(
        string="Shopify API Secret Key",
        help="Enter the API Secret Key from your Shopify custom app."
    )

    # auth_link = fields.Char(string='Authorization Link', readonly=True)

    def generate_auth_link(self):
        """
        Generate the Shopify OAuth authorization link for custom app installation (with state/nonce and scopes).
        """
        self.ensure_one()
        if not self.shopify_host or not self.shopify_client_id or not self.shopify_secret_id:
            raise UserError(_("Both Shopify Host, Client ID and Secret ID are required to Set the details."))
        if 'myshopify' not in self.shopify_host:
            raise UserError(
                _("A host should contain 'myshopify', for example: 'demo.myshopify.com'. You can refer the host in your Shopify store: Shopify => Settings => Domains"))

        # Save credentials in system parameters with domain prefix
        domain = self.shopify_host.strip().lower().replace('https://', '').replace('/', '')
        param_prefix = f"shopify_{domain}"
        config = self.env['ir.config_parameter'].sudo()
        config.set_param(f'{param_prefix}_client_id', self.shopify_client_id)
        config.set_param(f'{param_prefix}_secret_id', self.shopify_secret_id)

        # Show notification and close wizard. Use plain text with newlines for display_notification, and return a 'next' action to close wizard.
        message = _(
            'Your Shopify OAuth configuration has been saved.\n\n'
            'Next Steps:\n'
            '1. Go to your Shopify Partner account.\n'
            '2. In the Distribution section, find the link for Custom App Installation.\n'
            '3. Copy that installation link and paste it into your browser.\n'
            '4. You will be redirected to your actual Shopify store and prompted to install the custom app.\n'
            '5. Once installation is complete, you will be redirected to the next page in Odoo where you can see the token.'
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Configuration Saved'),
                'message': message,
                'sticky': False,
                'type': 'success',
            }
        }

    def toggle_show_help(self):
        """
        Toggle the help/instructions section visibility in the wizard.
        """
        self.ensure_one()
        self.show_help = not self.show_help
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'shopify.auth.process.ept',
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }
