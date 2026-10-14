"""Live payout exceptions, with navigation to the original evidence and repairs."""

from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError


STAGES = [
    ('missing_statement', 'Statement Not Generated'),
    ('unmatched', 'Awaiting Reconciliation'),
    ('statement_review', 'Statement Needs Review'),
    ('statement_structure', 'Statement Links / Amounts Need Review'),
    ('missing_transactions', 'No Imported Transactions'),
    ('validation', 'Payout Needs Validation'),
    ('settlement', 'Settlement Transfer Not Created'),
    ('bank', 'Awaiting Bank Match'),
    ('settlement_review', 'Settlement Needs Review'),
    ('waiting', 'Waiting for Transaction Repairs'),
    ('resolved', 'Resolved'),
]


class ShopifyPayoutExceptionReport(models.Model):
    _name = 'shopify.payout.exception.report.ept'
    _description = 'Shopify Payout Reconciliation Exceptions'
    _auto = False
    _rec_name = 'payout_id'
    _order = 'payout_date asc, payout_id asc, id asc'
    _depends = {
        'shopify.payout.report.ept': ['instance_id', 'payout_date', 'payout_reference_id', 'currency_id',
                                     'state', 'settlement_status'],
        'shopify.instance.ept': ['shopify_company_id', 'shopify_settlement_report_journal_id'],
        'shopify.payout.report.line.ept': ['payout_id', 'transaction_id', 'source_order_id',
            'source_order_transaction_id', 'transaction_type', 'amount', 'currency_id'],
        'account.bank.statement.line': ['payout_id', 'payout_line_id', 'company_id', 'move_id',
            'journal_id', 'amount', 'amount_currency', 'foreign_currency_id', 'amount_residual',
            'is_reconciled', 'shopify_transaction_type', 'shopify_transaction_id', 'shopify_order_transaction_id'],
        'account.move': ['state', 'checked'],
        'res.currency': ['rounding'],
        'shopify.payout.repair.line.ept': ['active', 'company_id', 'transaction_ids', 'last_preview_at', 'write_date'],
    }

    payout_id = fields.Many2one('shopify.payout.report.ept', readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    instance_id = fields.Many2one('shopify.instance.ept', string='Store', readonly=True)
    payout_date = fields.Date(readonly=True)
    payout_reference_id = fields.Char(string='Shopify Payout ID', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    scope = fields.Selection([('transaction', 'Transaction'), ('payout', 'Payout Closeout'),
                              ('statement', 'Unlinked Statement')], readonly=True)
    transaction_id = fields.Many2one('shopify.payout.report.line.ept', string='Payout Transaction', readonly=True)
    shopify_transaction_id = fields.Char(string='Shopify Balance Transaction ID', readonly=True)
    source_order_id = fields.Char(string='Shopify Order ID', readonly=True)
    source_order_transaction_id = fields.Char(string='Shopify Cash Transaction ID', readonly=True)
    transaction_type = fields.Char(readonly=True)
    amount = fields.Monetary(string='Transaction Amount', readonly=True, aggregator=False)
    remaining_amount = fields.Monetary(string='Unmatched Amount', readonly=True, aggregator=False,
        help='Signed residual for cash matching. Payout closeout rows have no cash residual; '
             'use Payout Amount for settlement work. Review-only issues can have a zero residual.')
    payout_amount = fields.Float(related='payout_id.amount', string='Payout Amount', readonly=True, aggregator=False)
    statement_id = fields.Many2one('account.bank.statement.line', string='Statement Transaction', readonly=True)
    statement_count = fields.Integer(readonly=True)
    case_id = fields.Many2one('shopify.payout.repair.line.ept', string='Latest Saved Case', readonly=True)
    stage = fields.Selection(STAGES, readonly=True)
    is_open = fields.Boolean(string='Open Exception', readonly=True)
    resolution = fields.Selection(
        selection=lambda self: self.env['shopify.payout.repair.line.ept']._fields['resolution']._description_selection(self.env),
        string='Repair Issue', readonly=True)
    case_status = fields.Selection(related='case_id.status', string='Repair Status', readonly=True)
    source_reference = fields.Char(related='case_id.source_reference', readonly=True)
    order_id = fields.Many2one('sale.order', compute='_compute_order_id', string='Odoo Order')
    summary = fields.Char(compute='_compute_guidance')
    next_step = fields.Char(compute='_compute_guidance')
    detail = fields.Text(related='case_id.detail', readonly=True)
    last_error = fields.Text(related='case_id.last_error', readonly=True)
    last_preview_at = fields.Datetime(related='case_id.last_preview_at', readonly=True)
    last_apply_at = fields.Datetime(related='case_id.last_apply_at', readonly=True)
    evidence_html = fields.Html(related='case_id.evidence_html', readonly=True)
    review_html = fields.Html(related='case_id.review_html', readonly=True)
    processing_issue = fields.Text(related='payout_id.reimport_reconciliation_issue', readonly=True)
    last_reimport_at = fields.Datetime(related='payout_id.last_reimport_at', readonly=True)
    last_reimport_result = fields.Text(related='payout_id.last_reimport_result', readonly=True)
    settlement_move_id = fields.Many2one(related='payout_id.settlement_move_id', readonly=True)
    settlement_status = fields.Selection(related='payout_id.settlement_status', readonly=True)
    settlement_bank_line_ids = fields.Many2many(related='payout_id.settlement_bank_line_ids', readonly=True)
    case_ids = fields.Many2many('shopify.payout.repair.line.ept', compute='_compute_breadcrumbs',
                               string='Saved Cases', context={'active_test': False})
    history_ids = fields.Many2many('shopify.payout.repair.history.ept', compute='_compute_breadcrumbs', string='Repair History')
    audit_ids = fields.Many2many('shopify.payout.repair.audit.ept', compute='_compute_breadcrumbs', string='Applied Accounting')

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        # Stable IDs survive matching and case refresh. Each transaction appears once,
        # even when it belongs to several workspaces or has duplicate statement rows.
        self.env.cr.execute("""
            CREATE VIEW shopify_payout_exception_report_ept AS
            WITH transactions AS (
                SELECT t.id AS transaction_id, p.id AS payout_id,
                       i.shopify_company_id AS company_id, p.instance_id, p.payout_date,
                       p.payout_reference_id, p.currency_id, t.transaction_id AS shopify_transaction_id,
                       t.source_order_id, t.source_order_transaction_id, t.transaction_type,
                       t.amount, s.statement_id, s.statement_count,
                       CASE WHEN s.statement_count = 0 THEN t.amount
                            ELSE s.remaining_amount END AS remaining_amount,
                       c.id AS case_id, c.resolution,
                       CASE WHEN s.statement_count = 0 THEN 'missing_statement'
                            WHEN s.statement_count <> 1 OR NOT COALESCE(s.structure_valid, FALSE) THEN 'statement_structure'
                            WHEN NOT COALESCE(s.matched, FALSE) THEN 'unmatched'
                            WHEN NOT COALESCE(s.reviewed, FALSE) THEN 'statement_review'
                            ELSE 'resolved' END AS stage
                  FROM shopify_payout_report_line_ept t
                  JOIN shopify_payout_report_ept p ON p.id = t.payout_id
                  JOIN shopify_instance_ept i ON i.id = p.instance_id
                  LEFT JOIN res_currency currency ON currency.id = p.currency_id
                  LEFT JOIN LATERAL (
                      SELECT MIN(st.id) AS statement_id, COUNT(*) AS statement_count,
                             BOOL_AND(st.is_reconciled) AS matched,
                             BOOL_AND(m.state = 'posted' AND m.checked) AS reviewed,
                             BOOL_AND(ROUND((st.amount - t.amount)::numeric /
                                            COALESCE(NULLIF(currency.rounding, 0), 0.01)) = 0
                                      AND st.shopify_transaction_type = t.transaction_type
                                      AND st.journal_id = i.shopify_settlement_report_journal_id
                                      AND (t.currency_id IS NULL OR t.currency_id = p.currency_id)) AS structure_valid,
                             SUM(CASE WHEN st.is_reconciled THEN 0
                                      WHEN st.foreign_currency_id IS NOT NULL AND st.amount_currency <> 0
                                      THEN -st.amount_residual * st.amount / st.amount_currency
                                      ELSE -st.amount_residual END) AS remaining_amount
                        FROM account_bank_statement_line st
                        JOIN account_move m ON m.id = st.move_id
                       WHERE st.payout_id = p.id AND st.payout_line_id = t.id
                         AND st.company_id = i.shopify_company_id
                  ) s ON TRUE
                  LEFT JOIN LATERAL (
                      SELECT rc.id, rc.resolution
                        FROM shopify_bulk_repair_transaction_rel rel
                        JOIN shopify_payout_repair_line_ept rc ON rc.id = rel.case_id
                       WHERE rel.transaction_id = t.id AND rc.active
                         AND rc.company_id = i.shopify_company_id
                       ORDER BY rc.last_preview_at DESC NULLS LAST, rc.write_date DESC, rc.id DESC
                       LIMIT 1
                  ) c ON TRUE
                 WHERE ROUND(t.amount::numeric / COALESCE(NULLIF(currency.rounding, 0), 0.01)) <> 0
            ), orphan_statements AS (
                SELECT st.id, st.payout_id, st.company_id, p.instance_id, p.payout_date,
                       p.payout_reference_id, p.currency_id, st.shopify_transaction_id,
                       st.shopify_order_transaction_id, st.shopify_transaction_type, st.amount,
                       CASE WHEN st.is_reconciled THEN 0
                            WHEN st.foreign_currency_id IS NOT NULL AND st.amount_currency <> 0
                            THEN -st.amount_residual * st.amount / st.amount_currency
                            ELSE -st.amount_residual END AS remaining_amount
                  FROM account_bank_statement_line st
                  JOIN shopify_payout_report_ept p ON p.id = st.payout_id
                  JOIN shopify_instance_ept i ON i.id = p.instance_id
                  LEFT JOIN shopify_payout_report_line_ept t ON t.id = st.payout_line_id
                 WHERE st.company_id = i.shopify_company_id
                   AND (t.id IS NULL OR t.payout_id IS DISTINCT FROM p.id)
            ), closeout AS (
                SELECT p.id AS payout_id, i.shopify_company_id AS company_id, p.instance_id,
                       p.payout_date, p.payout_reference_id, p.currency_id,
                       CASE WHEN NOT EXISTS (SELECT 1 FROM transactions t WHERE t.payout_id = p.id)
                                 THEN 'missing_transactions'
                            WHEN EXISTS (SELECT 1 FROM transactions t
                                          WHERE t.payout_id = p.id AND t.stage <> 'resolved')
                                 OR EXISTS (SELECT 1 FROM orphan_statements s WHERE s.payout_id = p.id)
                                 THEN 'waiting'
                            WHEN p.state IS DISTINCT FROM 'validated' THEN 'validation'
                            WHEN p.settlement_status = 'matched' THEN 'resolved'
                            WHEN p.settlement_status = 'not_created' THEN 'settlement'
                            WHEN p.settlement_status IN ('pending', 'partial') THEN 'bank'
                            ELSE 'settlement_review' END AS stage
                  FROM shopify_payout_report_ept p
                  JOIN shopify_instance_ept i ON i.id = p.instance_id
            )
            SELECT t.transaction_id::bigint * 4 AS id, t.payout_id, t.company_id, t.instance_id,
                   t.payout_date, t.payout_reference_id, t.currency_id, 'transaction'::varchar AS scope,
                   t.transaction_id, t.shopify_transaction_id, t.source_order_id,
                   t.source_order_transaction_id, t.transaction_type, t.amount, t.remaining_amount,
                   t.statement_id, t.statement_count, t.case_id, t.resolution, t.stage, t.stage <> 'resolved' AS is_open
              FROM transactions t
            UNION ALL
            SELECT p.payout_id::bigint * 4 + 1, p.payout_id, p.company_id, p.instance_id,
                   p.payout_date, p.payout_reference_id, p.currency_id, 'payout',
                   NULL::integer, NULL::varchar, NULL::varchar, NULL::varchar, NULL::varchar,
                   NULL::double precision, NULL::double precision, NULL::integer, 0::bigint,
                   NULL::integer, NULL::varchar, p.stage, p.stage NOT IN ('resolved', 'waiting')
              FROM closeout p
            UNION ALL
            SELECT s.id::bigint * 4 + 2, s.payout_id, s.company_id, s.instance_id, s.payout_date,
                   s.payout_reference_id, s.currency_id, 'statement', NULL::integer,
                   s.shopify_transaction_id, NULL::varchar, s.shopify_order_transaction_id,
                   s.shopify_transaction_type, s.amount, s.remaining_amount, s.id, 1::bigint,
                   NULL::integer, NULL::varchar, 'statement_structure', TRUE
              FROM orphan_statements s
        """)

    @api.depends('case_id.current_order_id', 'transaction_id.order_id')
    def _compute_order_id(self):
        for row in self:
            row.order_id = row.case_id.current_order_id or row.transaction_id.order_id

    @api.depends('stage', 'case_id.summary', 'case_id.next_step')
    def _compute_guidance(self):
        guidance = {
            'missing_statement': _('Open Payout and Generate Bank Statement; review its configuration if generation fails.'),
            'unmatched': _('Open Repair Workspace, preview the unmatched transactions and review each proposed repair.'),
            'statement_review': _('Open Statement Transactions and review the posted entry; then validate the payout.'),
            'statement_structure': _('Open Payout and Statement Transactions; review duplicate/missing links, amounts, types and currency.'),
            'missing_transactions': _('Open Payout and Reimport / Reconcile to retrieve its source transactions.'),
            'validation': _('Open Payout, review its totals and processing issue, then reconcile/validate using the existing actions.'),
            'settlement': _('Open Payout and Create Settlement Transfer, then match the actual bank transaction.'),
            'bank': _('Open Payout and Match Bank Receipt to reconcile the settlement with the actual bank transaction.'),
            'settlement_review': _('Open Payout and review its settlement entry, statement reviews and bank match.'),
            'waiting': _('Finish this payout’s open transaction exceptions first.'),
            'resolved': _('Resolved. Saved cases, evidence and repair history remain available.'),
        }
        labels = dict(self._fields['stage']._description_selection(self.env))
        for row in self:
            row.summary = (row.case_id.summary if row.stage == 'unmatched' and row.case_id
                           else labels.get(row.stage, ''))
            row.next_step = (row.case_id.next_step if row.stage == 'unmatched' and row.case_id
                             else guidance.get(row.stage, ''))

    @api.depends('transaction_id', 'payout_id')
    def _compute_breadcrumbs(self):
        # Include archived/superseded cases. Refreshing a preview must not hide an
        # earlier failure or the accounting links retained by an earlier workspace.
        cases = self.env['shopify.payout.repair.line.ept'].with_context(active_test=False).search([
            ('payout_ids', 'in', self.payout_id.ids)])
        for row in self:
            related = cases.filtered(lambda case: row.payout_id in case.payout_ids
                and (not row.transaction_id or row.transaction_id in case.transaction_ids))
            row.case_ids = related
            row.history_ids = related.history_ids
            row.audit_ids = row.payout_id.repair_audit_ids.filtered(lambda audit:
                not row.transaction_id or row.transaction_id in audit.transaction_ids
                or (not audit.transaction_ids and row.transaction_id.source_order_id
                    and audit.source_order_id == row.transaction_id.source_order_id))

    def _check_source_access(self):
        self.check_access('read')
        self.payout_id.check_access('read')

    def action_open_payout(self):
        self.ensure_one()
        self._check_source_access()
        return {'type': 'ir.actions.act_window', 'res_model': 'shopify.payout.report.ept',
                'res_id': self.payout_id.id, 'view_mode': 'form', 'target': 'current'}

    def action_open_case(self):
        self.ensure_one()
        self._check_source_access()
        if not self.case_id:
            raise UserError(_('Open Repair Workspace and preview this transaction to create its saved case.'))
        self.case_id.check_access('read')
        return self.case_id.action_open_details()

    def action_open_workspace(self):
        self.ensure_one()
        self._check_source_access()
        # Resume the exact workspace when a case exists, retaining its cutover,
        # settings and history. The existing workflow enforces manager access.
        if self.case_id:
            self.case_id.wizard_id._check_manager()
            return self.case_id.wizard_id._reopen()
        return self.payout_id.action_bulk_repair_transactions()

    def action_open_statements(self):
        self.ensure_one()
        self._check_source_access()
        domain = [('payout_id', '=', self.payout_id.id)]
        if self.transaction_id:
            domain.append(('payout_line_id', '=', self.transaction_id.id))
        elif self.scope == 'statement':
            domain.append(('id', '=', self.statement_id.id))
        return {'type': 'ir.actions.act_window', 'res_model': 'account.bank.statement.line',
                'view_mode': 'list,form', 'domain': domain, 'name': _('Payout Statement Transactions')}

    def action_open_shopify_order(self):
        self.ensure_one()
        self._check_source_access()
        self.transaction_id.check_access('read')
        return self.transaction_id.action_open_shopify_order()

    @api.model_create_multi
    def create(self, values):
        raise UserError(_('This live exception report cannot create records. Use the existing payout repair tools.'))

    def write(self, values):
        raise UserError(_('This live exception report cannot edit records. Use the linked source records.'))

    def unlink(self):
        raise UserError(_('This live exception report cannot delete records.'))


class ShopifyPayoutExceptionNavigation(models.Model):
    _inherit = 'shopify.payout.report.ept'

    def action_open_exception_report(self):
        self.check_access('read')
        action = self.env['ir.actions.act_window']._for_xml_id(
            'shopify_ept.action_shopify_payout_exception_report')
        action['domain'] = [('payout_id', 'in', self.ids)]
        return action
