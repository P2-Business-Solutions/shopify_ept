"""Private evidence API shared by UI and the optional read-only facade."""
from odoo import models
from odoo.exceptions import AccessError, UserError

READ_TOKEN = object()


def read_scope(env):
    return env.context.get('_p2_read_scope') if env.context.get('_p2_read_token') is READ_TOKEN else None


def read_lock_dates(move):
    """Read effective parent/company lock dates with no user exceptions.

    Odoo's _get_violated_soft_lock_date deliberately switches the context back
    to ignore_exceptions=False. Its general violation checker cannot enforce
    this project's stricter no-exception policy, even with an outer context.
    """
    move.ensure_one()
    company = move.company_id
    soft_fields = ['fiscalyear_lock_date']
    if move.journal_id.type == 'sale':
        soft_fields.append('sale_lock_date')
    if move.journal_id.type == 'purchase':
        soft_fields.append('purchase_lock_date')
    if move.line_ids.tax_ids or move.line_ids.tax_line_id:
        soft_fields.append('tax_lock_date')
    violations = [(field, company._get_user_lock_date(field, ignore_exceptions=True)) for field in soft_fields]
    violations.append(('hard_lock_date', company.user_hard_lock_date))
    return [(field, date) for field, date in violations if date and move.date <= date]


class ShopifyRepairRead(models.Model):
    _inherit = 'shopify.payout.report.line.ept'

    def _refund_credit_reference_safe(self):
        """Existing binding conflicts are diagnostics, not opaque RPC errors."""
        self.ensure_one()
        refs = self.env['shopify.refund.reference.ept'].search([
            ('instance_id', '=', self.payout_id.instance_id.id),
            ('transaction_id', '=', self.source_order_transaction_id)], limit=2)
        if len(refs) > 1:
            return {'status': 'failed', 'code': 'REFUND_REFERENCE_DUPLICATE'}
        if refs and self.env['account.move.line'].search_count([('move_id', '=', refs.credit_note_id.id)], limit=501) > 500:
            return {'status': 'unverified', 'code': 'LEDGER_RECORD_LIMIT'}
        try:
            reference = self._refund_credit_reference()
        except AccessError:
            raise
        except UserError:
            return {'status': 'failed', 'code': 'REFUND_REFERENCE_CONFLICT'}
        return {'status': 'passed' if reference else 'not_applicable',
                'code': 'REFUND_REFERENCE_VERIFIED' if reference else 'REFUND_REFERENCE_NOT_REGISTERED'}

    def _refund_credit_link_evidence(self, credit, source, scope_context, related_evidence=None):
        self.ensure_one()
        # The scope was authorized by the facade. This object cannot cross RPC.
        row = self.with_context(_p2_read_token=READ_TOKEN, _p2_read_scope=scope_context)
        credit = credit.with_env(row.env)
        try:
            plan = row._refund_credit_link_evidence_values(credit, source, scope_context)
        except AccessError:
            raise
        except UserError as error:
            # No translated-error string matching or raw text in the result.
            return {'status': 'failed' if getattr(error, 'repair_code', None) else 'unverified',
                    'code': getattr(error, 'repair_code', 'CREDIT_EVIDENCE_BLOCKED')}
        return {'status': 'passed', 'code': 'CREDIT_IDENTITY_AND_ALLOCATIONS_VERIFIED', 'plan': plan}
