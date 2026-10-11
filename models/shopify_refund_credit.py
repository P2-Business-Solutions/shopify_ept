"""Reviewed Shopify cash refunds can settle an existing credit, without an SO."""
from odoo import api, Command, fields, models, _
from odoo.exceptions import UserError
from .shopify_payment_plan import cash_events, fingerprint


class ShopifyRefundReference(models.Model):
    _name = 'shopify.refund.reference.ept'
    _description = 'Reviewed Shopify Refund Credit Reference'
    _rec_name = 'transaction_id'

    instance_id = fields.Many2one('shopify.instance.ept', required=True, readonly=True, ondelete='restrict')
    company_id = fields.Many2one(related='instance_id.shopify_company_id', store=True)
    currency_id = fields.Many2one('res.currency', required=True, readonly=True)
    source_order_id = fields.Char(required=True, readonly=True)
    refund_id = fields.Char(required=True, readonly=True)
    transaction_id = fields.Char(required=True, readonly=True, index=True)
    amount = fields.Monetary(required=True, readonly=True)
    credit_note_id = fields.Many2one('account.move', required=True, readonly=True, ondelete='restrict')
    source = fields.Json(required=True, readonly=True)
    ledger_fingerprint = fields.Char(required=True, readonly=True)
    review_note = fields.Text(required=True, readonly=True)
    _sql_constraints = [
        ('transaction_unique', 'unique(instance_id, transaction_id)', 'This Shopify refund is already linked.'),
        ('positive_amount', 'check(amount > 0)', 'A cash refund must be positive.'),
    ]

    @api.model_create_multi
    def create(self, values):
        raise UserError(_('Use Link Refund Credit Note to record a reviewed source reference.'))

    def _record_reference(self, values):
        return super(ShopifyRefundReference, self.sudo()).create([values])

    def write(self, values):
        raise UserError(_('Reviewed refund references cannot be edited.'))

    def unlink(self):
        raise UserError(_('Reviewed refund references cannot be deleted.'))

    def _check_credit(self):
        for reference in self:
            note = reference.credit_note_id
            note.check_access('read')
            if (note.state != 'posted' or note.move_type != 'out_refund'
                    or note.company_id != reference.company_id or note.currency_id != reference.currency_id
                    or note._shopify_refund_ledger_fingerprint() != reference.ledger_fingerprint):
                raise UserError(_('The linked credit note changed after review. Review its accounting before matching.'))


class ShopifyRefundCreditMove(models.Model):
    _inherit = 'account.move'

    shopify_refund_reference_ids = fields.One2many('shopify.refund.reference.ept', 'credit_note_id', readonly=True)

    def _shopify_refund_ledger_fingerprint(self):
        self.ensure_one()
        # Residuals change as individual refunds settle; the posted ledger must not.
        return fingerprint(dict(company=self.company_id.id, currency=self.currency_id.id,
            partner=self.commercial_partner_id.id, date=str(self.date), total=self.amount_total,
            lines=sorted((line.id, line.account_id.id, line.partner_id.id, line.currency_id.id,
                          line.balance, line.amount_currency) for line in self.line_ids)))

    def _shopify_refund_credit_candidates(self, transaction):
        """Optional integrations add source-linked candidates; never match by amount alone."""
        return self.search([
            ('move_type', '=', 'out_refund'), ('state', '=', 'posted'),
            ('shopify_instance_id', '=', transaction.payout_id.instance_id.id),
            '|', ('shopify_source_order_id', '=', transaction.source_order_id),
            ('shopify_refund_order_id.shopify_order_id', '=', transaction.source_order_id),
        ]) if transaction.source_order_id else self.browse()

    def _check_shopify_refund_credit_source(self, transaction, document):
        """Integration hook to validate a return's existing source identity."""
        return True


class ShopifyRefundCreditLine(models.Model):
    _inherit = 'shopify.payout.report.line.ept'

    refund_credit_note_id = fields.Many2one('account.move', compute='_compute_refund_credit_note',
                                          string='Linked Refund Credit Note')

    def _compute_refund_credit_note(self):
        for transaction in self:
            transaction.refund_credit_note_id = transaction._refund_credit_reference().credit_note_id

    def _refund_credit_reference(self):
        self.ensure_one()
        if self.shop_cash_kind or self.transaction_type not in ('refund', 'payment_refund'):
            return self.env['shopify.refund.reference.ept']
        reference = self.env['shopify.refund.reference.ept'].search([
            ('instance_id', '=', self.payout_id.instance_id.id),
            ('transaction_id', '=', self.source_order_transaction_id),
        ]) if self.source_order_transaction_id else self.env['shopify.refund.reference.ept']
        if reference:
            if (reference.source_order_id != self.source_order_id or reference.currency_id != self.payout_id.currency_id
                    or self.amount >= 0 or reference.currency_id.compare_amounts(reference.amount, -self.amount)):
                raise UserError(_('The payout transaction conflicts with its reviewed refund source.'))
            reference._check_credit()
        return reference

    def action_link_refund_credit_note(self):
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_('Only accounting managers can link refund credits.'))
        self.check_access('write')
        if self.shop_cash_kind or self.transaction_type not in ('refund', 'payment_refund'):
            raise UserError(_('Select an individual Shopify Payments refund.'))
        notes = self.env['account.move']._shopify_refund_credit_candidates(self).filtered(
            lambda note: note.company_id == self.payout_id.instance_id.shopify_company_id
            and note.currency_id == self.payout_id.currency_id)
        wizard = self.env['shopify.refund.credit.link.ept'].create({
            'transaction_id': self.id, 'credit_note_id': notes.id if len(notes) == 1 else False})
        return wizard._reopen()

    def _refund_credit_link_plan(self, credit, source=None):
        self.ensure_one()
        payout, instance = self.payout_id, self.payout_id.instance_id
        payout._check_payout_reimport_period()
        company, currency = instance.shopify_company_id, payout.currency_id
        credit.check_access('write')
        if (len(credit) != 1 or credit.move_type != 'out_refund' or credit.state != 'posted'
                or credit.company_id != company or credit.currency_id != currency):
            raise UserError(_('Select a posted customer credit note in the payout company and currency.'))
        if (self.shop_cash_kind or self.transaction_type not in ('refund', 'payment_refund')
                or self.amount >= 0 or not self.source_order_id or not self.source_order_transaction_id):
            raise UserError(_('The payout must identify an individual negative refund and its exact Shopify order transaction.'))
        if company.with_context(ignore_exceptions=True)._get_violated_lock_dates(credit.date, True, credit.journal_id):
            raise UserError(_('The credit note is in a closed accounting period.'))
        payload, transactions = source or self._repair_source()
        if str(payload.get('id')) != self.source_order_id:
            raise UserError(_('The source order differs from the payout refund.'))
        try:
            events = cash_events(transactions, self.source_order_id, currency.name)
        except ValueError as error:
            raise UserError(str(error)) from error
        event = next((row for row in events if row['id'] == self.source_order_transaction_id), None)
        if (not event or event['kind'] != 'refund' or event['gateway'] != 'shopify_payments'
                or currency.compare_amounts(float(event['amount']), -self.amount)):
            raise UserError(_('The payout does not match its exact successful Shopify Payments refund.'))
        documents = [row for row in payload.get('refunds', [])
                     if event['id'] in {str(tx.get('id')) for tx in row.get('transactions', [])}]
        if len(documents) != 1 or not documents[0].get('id'):
            raise UserError(_('The refund must identify exactly one Shopify refund document.'))
        document = documents[0]
        credit._check_shopify_refund_credit_source(self, document)
        if ('payout_refund_reference_ids' in credit._fields and credit.payout_refund_reference_ids):
            raise UserError(_('This credit already has another processor refund assignment. Review that settlement before linking Shopify cash.'))
        ids = {str(row.get('id')) for row in document.get('transactions', [])}
        refunds = [row for row in events if row['kind'] == 'refund' and row['id'] in ids]
        if any(row['gateway'] != 'shopify_payments' for row in refunds):
            raise UserError(_('This refund document includes another processor; review its separate settlement.'))
        if any(sum(row['id'] in {str(tx.get('id')) for tx in doc.get('transactions', [])}
                   for doc in payload.get('refunds', [])) != 1 for row in refunds):
            raise UserError(_('A refund transaction occurs in more than one source document.'))
        amount = sum(float(row['amount']) for row in refunds)
        if currency.compare_amounts(credit.amount_total, amount):
            raise UserError(_('The credit total must equal all successful cash refunds in this Shopify refund document.'))
        partner = credit.commercial_partner_id
        mappings = self.env['shopify.res.partner.ept'].search([
            ('shopify_instance_id', '=', instance.id),
            ('shopify_customer_id', '=', str((payload.get('customer') or {}).get('id') or '')),
        ]).partner_id.commercial_partner_id
        order = self._repair_order()
        if mappings or order:
            if (mappings and mappings != partner) or (order and order.partner_id.commercial_partner_id != partner):
                raise UserError(_('The credit customer differs from the Shopify order/customer mapping.'))
        else:
            email = str((payload.get('customer') or {}).get('email') or payload.get('email') or '').strip()
            partners = self.env['res.partner'].search([
                ('email', '=ilike', email), '|', ('company_id', '=', False), ('company_id', '=', company.id),
            ]).commercial_partner_id if email else self.env['res.partner']
            if partners != partner:
                raise UserError(_('The Shopify customer must identify this credit customer through a unique mapping or email.'))
        if ((credit.shopify_instance_id and credit.shopify_instance_id != instance)
                or (credit.shopify_source_order_id and credit.shopify_source_order_id != self.source_order_id)
                or (credit.shopify_refund_order_id and credit.shopify_refund_order_id != order)
                or (credit.shopify_refund_id and credit.shopify_refund_id != str(document['id']))):
            raise UserError(_('The credit note already identifies a different Shopify source.'))
        source_orders = credit.invoice_line_ids.sale_line_ids.order_id | credit.reversed_entry_id.invoice_line_ids.sale_line_ids.order_id
        if source_orders.filtered(lambda so: so.shopify_instance_id != instance or so.shopify_order_id != self.source_order_id):
            raise UserError(_('The credit or its original invoice belongs to another sales order.'))
        payout_rows = self.search([('payout_id.instance_id', '=', instance.id),
            ('source_order_transaction_id', 'in', [row['id'] for row in refunds]),
            ('transaction_type', 'in', ('refund', 'payment_refund'))])
        if any(len(payout_rows.filtered(lambda tx: tx.source_order_transaction_id == row['id'])) > 1 for row in refunds):
            raise UserError(_('Several payout rows identify the same cash refund. Review their source identity before linking.'))
        clearing = self.env['account.move'].search([
            ('shopify_instance_id', '=', instance.id), ('state', '!=', 'cancel'),
            ('shopify_legacy_collection_transaction_id', 'in', [row['id'] for row in refunds]),
        ], limit=1)
        if clearing:
            raise UserError(_('This refund already has opening-balance cash treatment. Review that entry before assigning a customer credit.'))
        for transaction in payout_rows:
            settled = transaction.payout_id.payout_statement_line_ids.filtered(
                lambda line: line.payout_line_id == transaction and line.is_reconciled)
            if settled and transaction._refund_credit_reference().credit_note_id != credit:
                raise UserError(_('A payout refund is already reconciled without this reviewed credit link. Review its existing match first.'))
        duplicates = self.env['account.move'].search([
            ('shopify_instance_id', '=', instance.id), ('shopify_refund_id', '=', str(document['id'])),
            ('move_type', '=', 'out_refund'), ('state', '!=', 'cancel'), ('id', '!=', credit.id),
        ])
        if duplicates:
            raise UserError(_('Another credit note already represents this Shopify refund.'))
        references = self.env['shopify.refund.reference.ept'].search([
            ('instance_id', '=', instance.id), ('transaction_id', 'in', [row['id'] for row in refunds])])
        if references.filtered(lambda row: row.credit_note_id != credit):
            raise UserError(_('A source refund is already linked to another credit note.'))
        if credit.shopify_refund_reference_ids.filtered(
                lambda row: row.instance_id != instance or row.refund_id != str(document['id'])):
            raise UserError(_('This credit note is already assigned to another refund document.'))
        ar = credit.line_ids.filtered(lambda line: line.account_type == 'asset_receivable')
        if len(ar.account_id) != 1 or not ar.account_id.reconcile or ar.account_id.deprecated:
            raise UserError(_('The credit needs one reconcilable customer receivable account.'))
        self._check_refund_credit_allocations(credit, refunds)
        return dict(kind='credit_link', line_id=self.id, source_order_id=self.source_order_id,
            source_name=payload.get('name') or self.source_order_id, refund_id=str(document['id']),
            credit_id=credit.id, amount=amount, events=refunds,
            ledger=credit._shopify_refund_ledger_fingerprint(),
            customer=payload.get('customer'), email=payload.get('email'))

    def _check_refund_credit_allocations(self, credit, events):
        """Accept previous exact payments/statement matches, never an unrelated allocation."""
        instance, currency = self.payout_id.instance_id, self.payout_id.currency_id
        ids = {row['id'] for row in events}
        ar = credit.line_ids.filtered(lambda line: line.account_type == 'asset_receivable')
        matches = ar.matched_debit_ids | ar.matched_credit_ids
        others = ((matches.debit_move_id | matches.credit_move_id) - ar).move_id
        for move in others:
            statement = move.statement_line_id
            payment = move.origin_payment_id
            if statement and statement.payout_id.instance_id == instance:
                reference = statement.payout_line_id._refund_credit_reference() if statement.payout_line_id else False
                if (reference and reference.credit_note_id == credit and reference.transaction_id in ids
                        and statement.is_reconciled and currency.compare_amounts(-statement.amount, reference.amount) == 0):
                    continue
            if payment and payment.shopify_instance_id == instance and payment.shopify_order_transaction_id in ids:
                event = next(row for row in events if row['id'] == payment.shopify_order_transaction_id)
                if (payment.payment_type == 'outbound' and payment.partner_type == 'customer'
                        and payment.move_id.state == 'posted' and payment.state not in ('canceled', 'rejected')
                        and payment.company_id == credit.company_id and payment.currency_id == currency
                        and payment.partner_id.commercial_partner_id == credit.commercial_partner_id
                        and not currency.compare_amounts(payment.amount, float(event['amount']))):
                    counterpart = payment._seek_for_lines()[1]
                    links = counterpart.matched_debit_ids | counterpart.matched_credit_ids
                    allocated = (links.debit_move_id | links.credit_move_id).move_id - move
                    if allocated == credit and counterpart.account_id == ar.account_id:
                        continue
            raise UserError(_('The credit is allocated to an unidentified payment, invoice or another statement. Review that allocation first.'))

    def _register_refund_credit(self, plan, review_note):
        self.ensure_one()
        if not self.env.user.has_group('account.group_account_manager') or not review_note.strip():
            raise UserError(_('An accounting manager must review and describe this refund credit link.'))
        credit = self.env['account.move'].browse(plan['credit_id'])
        instance = self.payout_id.instance_id
        credit.write(dict(shopify_instance_id=instance.id, shopify_source_order_id=self.source_order_id,
            shopify_refund_id=plan['refund_id'], is_refund_in_shopify=True,
            shopify_refund_order_id=self._repair_order().id))
        references = self.env['shopify.refund.reference.ept']
        for event in plan['events']:
            existing = references.search([('instance_id', '=', instance.id), ('transaction_id', '=', event['id'])])
            if existing:
                if (existing.credit_note_id != credit or fingerprint(existing.source) != fingerprint(event)
                        or existing.ledger_fingerprint != plan['ledger']):
                    raise UserError(_('The previously reviewed refund link conflicts with the current source or credit.'))
                continue
            references._record_reference(dict(instance_id=instance.id, currency_id=credit.currency_id.id,
                source_order_id=self.source_order_id, refund_id=plan['refund_id'], transaction_id=event['id'],
                amount=float(event['amount']), credit_note_id=credit.id, source=event,
                ledger_fingerprint=plan['ledger'], review_note=review_note.strip()))
        transactions = self.search([('payout_id.instance_id', '=', instance.id),
            ('source_order_id', '=', self.source_order_id), ('source_order_transaction_id', 'in', [row['id'] for row in plan['events']]),
            ('transaction_type', 'in', ('refund', 'payment_refund'))])
        self.env['shopify.payout.repair.audit.ept']._record_repair(dict(
            company_id=credit.company_id.id, source_order_id=self.source_order_id,
            payout_ids=[Command.set(transactions.payout_id.ids)], transaction_ids=[Command.set(transactions.ids)],
            credit_ids=[Command.set(credit.ids)], plan=dict(plan, review_note=review_note.strip())))
        transactions.invalidate_recordset(['refund_credit_note_id'])
        credit._message_log(body=_('Linked to Shopify refund %s. Review: %s. Match its imported payout to settle the credit.',
                                   plan['refund_id'], review_note.strip()))
        return transactions


class ShopifyRefundCreditPayout(models.Model):
    _inherit = 'shopify.payout.report.ept'

    def _reconcile_linked_refund_credit(self, statement, reference):
        self.ensure_one()
        self._check_payout_reimport_period()
        reference._check_credit()
        credit, currency = reference.credit_note_id, self.currency_id
        all_refs = credit.shopify_refund_reference_ids
        statement.payout_line_id._check_refund_credit_allocations(credit, all_refs.mapped('source'))
        payments = self.env['account.payment'].search([
            ('shopify_instance_id', '=', self.instance_id.id),
            ('shopify_order_transaction_id', '=', reference.transaction_id),
            ('state', 'not in', ('draft', 'canceled', 'rejected')),
        ])
        if payments:
            if len(payments) != 1:
                raise UserError(_('Several payments identify this refund. Review them before matching.'))
            payment = payments
            liquidity, counterpart, writeoffs = payment._seek_for_lines()
            links = counterpart.matched_debit_ids | counterpart.matched_credit_ids
            allocated = (links.debit_move_id | links.credit_move_id).move_id - payment.move_id
            if (payment.move_id.state != 'posted' or payment.payment_type != 'outbound'
                    or payment.partner_type != 'customer' or payment.company_id != credit.company_id
                    or payment.currency_id != currency or payment.partner_id.commercial_partner_id != credit.commercial_partner_id
                    or currency.compare_amounts(payment.amount, reference.amount) or allocated - credit
                    or len(counterpart) != 1 or writeoffs or len(liquidity) != 1
                    or liquidity.reconciled or not liquidity.account_id.reconcile
                    or currency.compare_amounts(self._payout_move_line_residual(statement, liquidity), -reference.amount)):
                raise UserError(_('The exact refund payment must be allocated only to this credit with its full outstanding cash still available.'))
            if not counterpart.reconciled:
                ar = credit.line_ids.filtered(lambda line: line.account_type == 'asset_receivable' and not line.reconciled)
                if (ar.account_id != counterpart.account_id
                        or currency.compare_amounts(credit.amount_residual, reference.amount) < 0):
                    raise UserError(_('The existing refund payment cannot be allocated to this credit receivable.'))
                (ar | counterpart).reconcile()
            if not counterpart.reconciled:
                raise UserError(_('The existing refund payment did not settle its customer receivable.'))
            lines = liquidity
        else:
            lines = credit.line_ids.filtered(lambda line: line.account_type == 'asset_receivable' and not line.reconciled)
            available = -sum(self._payout_move_line_residual(statement, line) for line in lines)
            if not lines or currency.compare_amounts(available, reference.amount) < 0:
                raise UserError(_('The linked credit no longer has enough open receivable for this refund.'))
        before = sum(self._payout_move_line_residual(statement, line) for line in lines)
        statement.write(dict(partner_id=credit.commercial_partner_id.id, refund_invoice_id=credit.id))
        self.shopify_reconcile_bank_statement_line_ept(statement.id, lines.ids)
        lines.invalidate_recordset()
        statement.invalidate_recordset()
        after = sum(self._payout_move_line_residual(statement, line) for line in lines)
        if not statement.is_reconciled or currency.compare_amounts(after - before, reference.amount):
            raise UserError(_('The statement did not settle the exact reviewed refund amount. Its matching was rolled back.'))


