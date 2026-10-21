"""Review A2000 exports; a PAID header alone never allocates an individual cash event."""
import base64
import csv
import hashlib
import io
from decimal import Decimal, InvalidOperation

from markupsafe import Markup
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from ..models.shopify_payment_plan import fingerprint

EVIDENCE_INTERNAL = object()
COLUMNS = {'order_id', 'transaction_id', 'document_type', 'document_number', 'status', 'currency',
           'document_amount', 'allocated_amount', 'document_date', 'allocation_reference'}


class OpeningEvidenceImport(models.TransientModel):
    _name = 'shopify.opening.evidence.import.ept'
    _description = 'Import A2000 Opening Cash Evidence'

    repair_id = fields.Many2one('shopify.payout.repair.ept', required=True, readonly=True)
    csv_file = fields.Binary(attachment=False)
    filename = fields.Char()
    reviewed = fields.Boolean(string='I verified the PAID documents and exact cash allocations in A2000')
    preview_html = fields.Html(readonly=True)
    plan_json = fields.Json(readonly=True)
    state = fields.Selection([('draft', 'Draft'), ('preview', 'Preview'), ('done', 'Attached')], default='draft', readonly=True)

    @api.model_create_multi
    def create(self, values_list):
        if any(set(v) - {'repair_id', 'csv_file', 'filename', 'reviewed'} for v in values_list):
            raise UserError(_('Use Preview to inspect A2000 allocations.'))
        return super().create(values_list)

    def write(self, values):
        if self.env.context.get('_opening_evidence_internal') is not EVIDENCE_INTERNAL:
            if set(values) - {'csv_file', 'filename', 'reviewed'} or self.state == 'done':
                raise UserError(_('Use Preview and Attach Evidence to update this review.'))
            values = dict(values, state='draft', plan_json=False)
        return super().write(values)

    def _plan(self):
        self.ensure_one()
        repair = self.repair_id
        repair._check_manager()
        instances = repair.payout_ids.instance_id
        if len(instances) != 1 or not self.reviewed or not repair.historical_collection_account_id:
            raise UserError(_('Select one store, its opening cash account, and confirm the exact A2000 allocations.'))
        if not self.csv_file:
            raise UserError(_('Upload the reviewed A2000 allocation CSV before previewing.'))
        try:
            raw = base64.b64decode(self.csv_file, validate=True)
            if len(raw) > 2_000_000:
                raise ValueError()
            reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
            if set(reader.fieldnames or []) != COLUMNS or len(reader.fieldnames or []) != len(COLUMNS):
                raise ValueError()
            records = list(reader)
            if not records or len(records) > 1000:
                raise ValueError()
        except (ValueError, UnicodeError, csv.Error):
            raise UserError(_('Upload a UTF-8 CSV with the documented columns and 1–1,000 allocations (maximum 2 MB).')) from None
        evidence = self.env['shopify.opening.evidence.ept']
        rows, seen, grouped = [], set(), {}
        for row in records:
            if None in row or any(value is None for value in row.values()):
                raise UserError(_('An evidence row has missing or extra columns.'))
            row = {k: (v or '').strip() for k, v in row.items() if k}
            key = row['transaction_id']
            if key in seen:
                raise UserError(_('Duplicate cash transaction in the evidence file.'))
            seen.add(key)
            line = repair.payout_ids.payout_transaction_ids.filtered(lambda item: item.source_order_transaction_id == key)
            if len(line) != 1 or line.source_order_id != row['order_id']:
                raise UserError(_('An allocation does not identify one exact cash event in the selected payouts.'))
            expected = 'credit_memo' if line.amount < 0 else 'invoice'
            try:
                header, allocated = Decimal(row['document_amount']), Decimal(row['allocated_amount'])
                date = fields.Date.to_date(row['document_date'])
                if (not header.is_finite() or not allocated.is_finite() or header <= 0 or allocated <= 0
                        or allocated > header or not date or date >= repair.cutover_date
                        or allocated % Decimal(str(line.payout_id.currency_id.rounding))
                        or header % Decimal(str(line.payout_id.currency_id.rounding))):
                    raise ValueError()
            except (ValueError, InvalidOperation):
                raise UserError(_('Document amounts must be positive, finite, and dated before cutover. Allocations cannot exceed their document.')) from None
            if (row['status'] != 'PAID' or row['document_type'] != expected
                    or row['currency'] != line.payout_id.currency_id.name
                    or line.payout_id.currency_id.compare_amounts(float(allocated), abs(line.amount))
                    or not row['document_number'] or not row['allocation_reference']):
                raise UserError(_('Each row needs the correct PAID document type, currency, exact cash amount and allocation reference.'))
            # Existing source proof still checks successful cash, fulfillment, cutover and duplicates.
            plan = line._historical_collection_plan(repair.cutover_date, repair.timezone,
                repair.historical_collection_account_id, True)
            existing = evidence.search([('instance_id', '=', instances.id), ('transaction_id', '=', key)])
            if existing:
                raise UserError(_('This cash transaction already has attached evidence. Reuse its existing review.'))
            doc_key = (expected, row['document_number'])
            group = grouped.setdefault(doc_key, {'header': header, 'date': date, 'total': Decimal(0), 'refs': set()})
            if group['header'] != header or group['date'] != date or row['allocation_reference'] in group['refs']:
                raise UserError(_('A2000 document details or allocation references conflict within this file.'))
            group['total'] += allocated
            group['refs'].add(row['allocation_reference'])
            prior = evidence.search([('instance_id', '=', instances.id), ('document_type', '=', expected),
                                     ('document_number', '=', row['document_number'])])
            if (any(e.currency_id != line.payout_id.currency_id or e.document_date != date
                    or e.document_amount != float(header) for e in prior)
                    or group['total'] + sum(Decimal(str(e.allocated_amount)) for e in prior) > header):
                raise UserError(_('Total reviewed allocations exceed or conflict with the A2000 document.'))
            rows.append(dict(instance_id=instances.id, source_order_id=row['order_id'], transaction_id=key,
                document_type=expected, document_number=row['document_number'], document_date=str(date),
                currency_id=line.payout_id.currency_id.id, document_amount=float(header), allocated_amount=float(allocated),
                allocation_reference=row['allocation_reference'], source_hash=fingerprint(dict(event=plan['event'], amount=plan['amount'], currency=line.payout_id.currency_id.id))))
        return dict(rows=rows, file_hash=hashlib.sha256(raw).hexdigest())

    def action_preview(self):
        plan = self._plan()
        html = Markup('<p>%s exact allocations. Attaching evidence does not post accounting. Refresh Bulk Repair and select the ready cases.</p>') % len(plan['rows'])
        html += Markup('<table class="table"><tr><th>Transaction</th><th>PAID document</th><th>Allocation</th></tr>')
        html += Markup('').join(Markup('<tr><td>%s</td><td>%s %s</td><td>%s</td></tr>') % (
            r['transaction_id'], r['document_type'], r['document_number'], r['allocated_amount']) for r in plan['rows'])
        self.with_context(_opening_evidence_internal=EVIDENCE_INTERNAL).write(dict(plan_json=plan,
            preview_html=html + Markup('</table>'), state='preview'))
        return self._reopen()

    def action_apply(self):
        self.repair_id._check_manager()
        if self.state != 'preview':
            raise UserError(_('Preview the exact allocations first.'))
        self.repair_id.payout_ids._lock_settlement_payouts()
        self.env.cr.execute('SELECT id FROM shopify_instance_ept WHERE id = %s FOR UPDATE',
                            [self.repair_id.payout_ids.instance_id.id])
        if fingerprint(self._plan()) != fingerprint(self.plan_json):
            raise UserError(_('Evidence or source changed. Preview again.'))
        attachment = self.env['ir.attachment'].create(dict(name=self.filename or 'A2000-opening-allocations.csv',
            datas=self.csv_file, mimetype='text/csv', res_model='shopify.payout.report.ept',
            res_id=self.repair_id.payout_ids[:1].id))
        for row in self.plan_json['rows']:
            self.env['shopify.opening.evidence.ept']._record(dict(row, attachment_id=attachment.id, file_hash=self.plan_json['file_hash']))
        for payout in self.repair_id.payout_ids:
            payout.message_post(body=_('Attached reviewed A2000 opening-cash allocation evidence.'), attachment_ids=attachment.ids)
        self.with_context(_opening_evidence_internal=EVIDENCE_INTERNAL).write({'state': 'done'})
        return self._reopen()

    def _reopen(self):
        return dict(type='ir.actions.act_window', name=_('Import A2000 Opening Evidence'),
                    res_model=self._name, res_id=self.id, view_mode='form', target='new')


class OpeningEvidenceWorkspace(models.Model):
    _inherit = 'shopify.payout.repair.ept'

    def action_import_opening_evidence(self):
        self._check_manager()
        wizard = self.env['shopify.opening.evidence.import.ept'].create({'repair_id': self.id})
        return wizard._reopen()


class OpeningEvidenceCase(models.Model):
    _inherit = 'shopify.payout.repair.line.ept'

    opening_evidence_id = fields.Many2one('shopify.opening.evidence.ept', compute='_compute_opening_evidence',
                                         string='A2000 Opening Evidence')

    @api.depends('plan')
    def _compute_opening_evidence(self):
        for case in self:
            case.opening_evidence_id = (case.plan or {}).get('a2000_evidence', {}).get('id', False)
