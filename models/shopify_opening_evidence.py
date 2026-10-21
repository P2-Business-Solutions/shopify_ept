"""Immutable reviewed A2000 cash allocations, attached to exact Shopify events."""
import base64
import hashlib

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class OpeningEvidence(models.Model):
    _name = 'shopify.opening.evidence.ept'
    _description = 'Reviewed A2000 Opening Cash Evidence'
    _rec_name = 'document_number'

    instance_id = fields.Many2one('shopify.instance.ept', required=True, ondelete='restrict')
    company_id = fields.Many2one(related='instance_id.shopify_company_id', store=True)
    source_order_id = fields.Char(required=True)
    transaction_id = fields.Char(required=True, index=True)
    document_type = fields.Selection([('invoice', 'PAID Invoice'), ('credit_memo', 'PAID Credit Memo')], required=True)
    document_number = fields.Char(required=True)
    document_date = fields.Date(required=True)
    currency_id = fields.Many2one('res.currency', required=True)
    document_amount = fields.Monetary(required=True)
    allocated_amount = fields.Monetary(required=True)
    allocation_reference = fields.Char(required=True)
    attachment_id = fields.Many2one('ir.attachment', required=True, ondelete='restrict')
    source_hash = fields.Char(required=True)
    file_hash = fields.Char(required=True)

    _sql_constraints = [
        ('opening_event_unique', 'unique(instance_id, transaction_id)', 'This opening cash event already has evidence.'),
        ('opening_allocation_unique', 'unique(instance_id, document_type, document_number, allocation_reference)',
         'This A2000 document allocation already identifies another event.'),
    ]

    @api.model_create_multi
    def create(self, values):
        raise UserError(_('Use Import A2000 Opening Evidence to review and attach allocations.'))

    def _record(self, values):
        return super().create(values)

    def write(self, values):
        raise UserError(_('Reviewed A2000 evidence cannot be edited.'))

    def unlink(self):
        raise UserError(_('Reviewed A2000 evidence cannot be deleted.'))

    def _check_file_integrity(self):
        for evidence in self:
            data = base64.b64decode(evidence.attachment_id.datas or b'')
            if hashlib.sha256(data).hexdigest() != evidence.file_hash:
                raise UserError(_('The original A2000 evidence file changed or is unavailable. Review it before clearing cash.'))
