"""Permanent accounting trail for a Shopify payment synchronization."""
import json
from odoo import api, fields, models
from odoo.exceptions import UserError


class ShopifyPaymentAudit(models.Model):
    _name = 'shopify.payment.audit.ept'
    _description = 'Shopify Payment Repair Audit'
    _order = 'id desc'
    _rec_name = 'order_id'

    order_id = fields.Many2one('sale.order', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='order_id.company_id', store=True, index=True)
    plan = fields.Json(readonly=True)
    plan_text = fields.Text(compute='_compute_plan_text', string='Source Transactions and Repair Plan')
    payment_ids = fields.Many2many('account.payment', readonly=True)
    replaced_payment_ids = fields.Many2many('account.payment', 'shopify_audit_replaced_payment_rel', readonly=True)
    credit_note_ids = fields.Many2many('account.move', readonly=True)

    @api.depends('plan')
    def _compute_plan_text(self):
        for audit in self:
            audit.plan_text = json.dumps(audit.plan or {}, indent=2, sort_keys=True)

    def write(self, vals):
        raise UserError('Payment audit records cannot be edited.')

    @api.model_create_multi
    def create(self, vals_list):
        raise UserError('Payment audits can only be created by Shopify payment synchronization.')

    def _record_sync(self, values):
        # Private entry point: accountants can read the trail, but cannot forge
        # the prior invoice treatment that later refunds rely on. sudo keeps uid.
        return super(ShopifyPaymentAudit, self.sudo()).create([values])

    def unlink(self):
        raise UserError('Payment audit records cannot be deleted.')
