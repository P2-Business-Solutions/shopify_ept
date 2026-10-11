"""Review an existing credit and match its exact Shopify payout refund."""
from odoo import fields, models, Command, _
from odoo.exceptions import UserError
from ..models.shopify_payment_plan import fingerprint


class ShopifyRefundCreditLink(models.TransientModel):
    _name = 'shopify.refund.credit.link.ept'
    _description = 'Link Shopify Refund Credit Note'

    transaction_id = fields.Many2one('shopify.payout.report.line.ept', required=True,
        domain="[('transaction_type', 'in', ('refund', 'payment_refund'))] + ([('id', 'in', context.get('shopify_refund_candidate_ids'))] if context.get('shopify_refund_candidate_ids') else [])")
    company_id = fields.Many2one(related='transaction_id.payout_id.instance_id.shopify_company_id')
    currency_id = fields.Many2one(related='transaction_id.payout_id.currency_id')
    amount = fields.Float(related='transaction_id.amount', string='Payout Refund')
    credit_note_id = fields.Many2one('account.move',
        domain="[('move_type', '=', 'out_refund'), ('state', '=', 'posted'), ('company_id', '=', company_id), ('currency_id', '=', currency_id)]")
    review_note = fields.Text(string='Accounting Review')
    plan = fields.Json(readonly=True)
    preview_text = fields.Text(readonly=True, string='Review')
    result = fields.Text(readonly=True)
    state = fields.Selection([('review', 'Review'), ('done', 'Done')], default='review', readonly=True)

    def _check_manager(self):
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_('Only accounting managers can link and reconcile refund credits.'))
        self.transaction_id.check_access('write')
        self.transaction_id.payout_id.check_access('write')

    def action_preview(self):
        self._check_manager()
        plan = self.transaction_id._refund_credit_link_plan(self.credit_note_id)
        self.write(dict(plan=plan, preview_text=_(
            '%(order)s / Shopify refund %(refund)s: reuse %(credit)s for %(amount)s %(currency)s. '
            '%(count)s successful cash refund(s). The posted credit accounts and taxes are retained. '
            'Matching uses an existing exact refund payment when present, otherwise the open credit receivable.',
            order=plan['source_name'], refund=plan['refund_id'], credit=self.credit_note_id.display_name,
            amount=plan['amount'], currency=self.currency_id.name, count=len(plan['events']))))
        return self._reopen()

    def action_apply(self):
        self._check_manager()
        if not self.plan or not (self.review_note or '').strip():
            raise UserError(_('Preview the source refund and record your accounting review before matching.'))
        payout = self.transaction_id.payout_id
        payout._lock_settlement_payouts()
        key = 'shopify-cash:%s:%s' % (payout.instance_id.id, self.transaction_id.source_order_id)
        self.env.cr.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', [key])
        self.env.cr.execute('SELECT id FROM account_move WHERE id = %s FOR UPDATE', [self.credit_note_id.id])
        self.credit_note_id.invalidate_recordset()
        fresh = self.transaction_id._refund_credit_link_plan(self.credit_note_id)
        if fingerprint(fresh) != fingerprint(self.plan):
            raise UserError(_('Source data or accounting changed after preview. Refresh the preview.'))
        transactions = self.transaction_id._register_refund_credit(fresh, self.review_note)
        issues = []
        for report in transactions.payout_id:
            scoped = report.with_context(shopify_bulk_repaired_line_ids=transactions.filtered(
                lambda tx: tx.payout_id == report).ids, cron_process=True)
            report._check_payout_reimport_period()
            if report.state in ('draft', 'partially_generated'):
                scoped.generate_bank_statement()
            scoped.process_bank_statement()
            if report.reimport_reconciliation_issue:
                issues.append('%s: %s' % (report.display_name, report.reimport_reconciliation_issue))
        # Keep the existing workspace and its links, including any matching error.
        cases = self.env['shopify.payout.repair.line.ept'].search([('transaction_ids', 'in', transactions.ids)])
        for case in cases:
            values = dict(credit_ids=[Command.set((case.credit_ids | self.credit_note_id).ids)],
                selected=False, last_apply_at=fields.Datetime.now(),
                last_error='\n'.join(issues) or False,
                detail=_('Linked existing credit %s to Shopify refund %s.', self.credit_note_id.display_name, fresh['refund_id']))
            if not case.transaction_ids - transactions:
                values.update(status='done', kind='credit_link', plan=dict(kind='credit_link',
                    credit_ids=self.credit_note_id.ids, transaction_ids=case.transaction_ids.ids))
            case.write(values)
            case._record_history('match')
        self.write(dict(state='review' if issues else 'done', result=_('Existing credit linked. No new credit note or refund payment was created.')
            + ('\n' + '\n'.join(issues) if issues else '\n' + _('Imported payout refund statements reconciled.'))))
        return self._reopen()

    def _reopen(self):
        return dict(type='ir.actions.act_window', res_model=self._name, res_id=self.id,
                    view_mode='form', target='new', name=_('Link Refund Credit Note'))
