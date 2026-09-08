"""Net settlement entries and bank receipt tracking for Shopify payouts."""

from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError


class ShopifyPayoutSettlement(models.Model):
    _inherit = 'shopify.payout.report.ept'

    settlement_move_id = fields.Many2one(
        'account.move', string='Settlement Entry', readonly=True,
        copy=False, ondelete='restrict', index=True)
    settlement_line_id = fields.Many2one(
        'account.move.line', string='Settlement Journal Item', readonly=True,
        copy=False, ondelete='restrict', index=True)
    settlement_bank_journal_id = fields.Many2one(
        'account.journal', string='Receiving Bank', readonly=True, copy=False)
    settlement_status = fields.Selection([
        ('not_created', 'Not Created'), ('pending', 'Awaiting Bank Match'),
        ('partial', 'Partially Matched'), ('matched', 'Bank Matched'),
        ('review', 'Needs Review'),
    ], compute='_compute_settlement_status', string='Bank Settlement', store=True, index=True)
    settlement_bank_line_ids = fields.Many2many(
        'account.bank.statement.line', compute='_compute_settlement_status',
        string='Matched Bank Transactions', store=True)
    payout_statement_line_ids = fields.One2many(
        'account.bank.statement.line', 'payout_id', readonly=True)

    @api.depends(
        'settlement_move_id.state', 'settlement_move_id.reversal_move_ids.state',
        'settlement_line_id.reconciled', 'settlement_line_id.amount_currency',
        'settlement_line_id.matched_debit_ids', 'settlement_line_id.matched_credit_ids',
        'settlement_bank_journal_id',
        'settlement_line_id.matched_debit_ids.debit_move_id.move_id.state',
        'settlement_line_id.matched_credit_ids.credit_move_id.move_id.state',
        'settlement_line_id.matched_debit_ids.debit_move_id.move_id.statement_line_id.is_reconciled',
        'settlement_line_id.matched_credit_ids.credit_move_id.move_id.statement_line_id.is_reconciled',
        'settlement_line_id.matched_debit_ids.debit_move_id.move_id.checked',
        'settlement_line_id.matched_credit_ids.credit_move_id.move_id.checked',
        'settlement_line_id.matched_debit_ids.credit_amount_currency',
        'settlement_line_id.matched_credit_ids.debit_amount_currency',
        'settlement_line_id.matched_debit_ids.debit_move_id.move_id.journal_id',
        'settlement_line_id.matched_credit_ids.credit_move_id.move_id.journal_id',
        'settlement_line_id.matched_debit_ids.debit_move_id.move_id.statement_line_id.payout_id',
        'settlement_line_id.matched_credit_ids.credit_move_id.move_id.statement_line_id.payout_id',
        'settlement_line_id.currency_id.rounding',
        'payout_statement_line_ids.is_reconciled', 'payout_statement_line_ids.move_id.checked',
        'payout_statement_line_ids.move_id.state',
    )
    def _compute_settlement_status(self):
        for payout in self:
            line = payout.settlement_line_id
            bank_lines = self.env['account.bank.statement.line']
            bank_amount = 0.0
            for partial in line.matched_debit_ids | line.matched_credit_ids:
                is_debit = partial.debit_move_id == line
                other = partial.credit_move_id if is_debit else partial.debit_move_id
                bank_line = other.move_id.statement_line_id
                if (bank_line and bank_line.journal_id == payout.settlement_bank_journal_id
                        and bank_line.move_id.state == 'posted'
                        and not bank_line.payout_id):
                    bank_lines |= bank_line
                    bank_amount += (partial.debit_amount_currency if is_debit
                                    else partial.credit_amount_currency)
            payout.settlement_bank_line_ids = bank_lines
            move = payout.settlement_move_id
            if not move:
                payout.settlement_status = 'not_created'
            elif (move.state != 'posted' or not line
                  or not payout.payout_statement_line_ids
                  or payout.payout_statement_line_ids.filtered(
                      lambda statement: not statement.is_reconciled or not statement.move_id.checked
                      or statement.move_id.state != 'posted')
                  or bank_lines.filtered(lambda statement: not statement.move_id.checked)
                  or move.reversal_move_ids.filtered(lambda reverse: reverse.state == 'posted')):
                payout.settlement_status = 'review'
            elif (line.reconciled and bank_lines
                  and all(bank_lines.mapped('is_reconciled'))
                  and line.currency_id.compare_amounts(bank_amount, abs(line.amount_currency)) == 0):
                payout.settlement_status = 'matched'
            elif line.reconciled:
                # A write-off or a reversal is not evidence of a bank receipt.
                payout.settlement_status = 'review'
            else:
                payout.settlement_status = 'partial' if bank_lines else 'pending'

    def _lock_settlement_payouts(self):
        self.check_access('write')
        if self:
            self.flush_recordset()
            self.env.cr.execute(
                'SELECT id FROM shopify_payout_report_ept WHERE id IN %s ORDER BY id FOR UPDATE',
                [tuple(self.ids)],
            )
            self.invalidate_recordset()

    def _check_settlement_transactions(self):
        """Prove completeness and totals from this payout, never from order totals."""
        self.ensure_one()
        currency = self.currency_id
        if not currency:
            raise UserError(_('The payout currency is missing.'))
        transactions = self.payout_transaction_ids.filtered(
            lambda row: not currency.is_zero(row.amount))
        if not transactions:
            raise UserError(_('There are no nonzero payout transactions to validate.'))
        identifiers = self.payout_transaction_ids.filtered('transaction_id').mapped('transaction_id')
        if len(identifiers) != len(set(identifiers)):
            raise UserError(_('The payout contains duplicate Shopify transaction IDs.'))
        fees = self.payout_transaction_ids.filtered(lambda row: row.transaction_type == 'fees')
        expected_fees = -sum((self.payout_transaction_ids - fees).mapped('fee'))
        if currency.compare_amounts(sum(fees.mapped('amount')), expected_fees):
            raise UserError(_('The booked fee total differs from Shopify transaction fees. Review the fee line.'))
        statements = self.env['account.bank.statement.line'].search([('payout_id', '=', self.id)])
        by_transaction = statements.grouped(lambda row: row.payout_line_id.id)
        for transaction in transactions:
            linked = by_transaction.get(transaction.id, self.env['account.bank.statement.line'])
            if len(linked) != 1:
                raise UserError(_('Payout transaction %s must have exactly one statement line.',
                                  transaction.transaction_id or transaction.transaction_type))
            if currency.compare_amounts(linked.amount, transaction.amount):
                raise UserError(_('A statement amount differs from its imported payout transaction.'))
            if (linked.shopify_transaction_type != transaction.transaction_type
                    or (transaction.currency_id and transaction.currency_id != currency)):
                raise UserError(_('A payout transaction has an inconsistent type or currency.'))
        if statements.filtered(lambda row: row.payout_line_id not in self.payout_transaction_ids):
            raise UserError(_('The payout contains statement lines without a matching payout transaction.'))
        if statements.filtered(
                lambda row: not row.is_reconciled or row.move_id.state != 'posted' or not row.move_id.checked):
            raise UserError(_('Reconcile and review every payout statement line before creating a settlement transfer.'))
        source = self.check_journal_and_currency()
        if not source or statements.filtered(lambda row: row.journal_id != source):
            raise UserError(_('All payout statement lines must use the configured Payout Report Journal.'))
        # Shopify may include the actual payout-outflow transaction in its ledger.
        # It is already the transfer out of clearing, not an additional expense.
        outflows = statements.filtered(lambda row: row.shopify_transaction_type == 'payout')
        if len(outflows) > 1 or (outflows and currency.compare_amounts(outflows.amount, -self.amount)):
            raise UserError(_('The imported payout outflow does not uniquely match the net payout.'))
        activity = statements - outflows
        total = sum(activity.mapped('amount'))
        if currency.compare_amounts(total, self.amount):
            raise UserError(_(
                'Payout %(payout)s does not balance: reconciled activity is %(total)s, '
                'but Shopify reports %(amount)s. Check missing transactions, refunds and fees; '
                'do not write off the difference to force a match.',
                payout=self.payout_reference_id, total=total, amount=self.amount))
        return activity, outflows

    def _check_settlement_configuration(self):
        self.ensure_one()
        instance = self.instance_id
        company = instance.shopify_company_id
        source = self.check_journal_and_currency()
        bank = instance.shopify_payout_bank_journal_id
        journal = instance.shopify_payout_transfer_journal_id
        transit = instance.shopify_payout_transit_account_id
        if not source or not bank or not journal or not transit:
            raise UserError(_('Configure the receiving bank, settlement transfer journal and payouts in transit account '
                              'on the Shopify instance’s Payout Configurations tab.'))
        if (source.company_id != company or bank.company_id != company or journal.company_id != company
                or company not in transit.company_ids):
            raise UserError(_('Settlement journals and the transit account must belong to the Shopify company.'))
        if source.type != 'bank' or bank.type != 'bank' or journal.type != 'general' or source == bank:
            raise UserError(_('Use separate Shopify and receiving bank journals, and a miscellaneous transfer journal.'))
        if (not transit.reconcile or transit.deprecated or transit.account_type != 'asset_current'
                or transit in (source.default_account_id, bank.default_account_id,
                               source.suspense_account_id, bank.suspense_account_id)
                or source.default_account_id == bank.default_account_id):
            raise UserError(_('Use a dedicated reconcilable current asset transit account and distinct bank accounts.'))
        if (bank.currency_id or company.currency_id) != self.currency_id:
            raise UserError(_('The receiving bank journal must use the payout currency.'))
        for record in (journal, transit, source.default_account_id):
            if record.currency_id and record.currency_id != self.currency_id:
                raise UserError(_('The settlement journal and accounts must support the payout currency.'))
        methods = (bank.inbound_payment_method_line_ids if self.amount > 0
                   else bank.outbound_payment_method_line_ids)
        if transit not in methods.payment_account_id:
            raise UserError(_('Set the transit account as an outstanding receipts/payments account on the receiving '
                              'bank journal so the settlement appears in normal bank matching.'))
        if not self.payout_date or not self.payout_reference_id or self.payout_status != 'paid':
            raise UserError(_('A settlement transfer requires a dated, identified payout marked Paid by Shopify.'))
        if self.currency_id.is_zero(self.amount):
            raise UserError(_('A zero payout does not need a bank settlement transfer.'))
        return source, bank, journal, transit

    def action_create_settlement_transfer(self):
        """Post or reuse one entry; the bank deposit is matched separately in Odoo."""
        self._lock_settlement_payouts()
        for payout in self.sorted('id'):
            # Include entries whose payout-side link was accidentally cleared.
            existing = self.env['account.move'].search([
                '|', ('shopify_settlement_payout_id', '=', payout.id),
                '&', ('shopify_settlement_instance_id', '=', payout.instance_id.id),
                ('shopify_settlement_reference', '=', payout.payout_reference_id),
            ], limit=1)
            if existing:
                if payout.settlement_move_id != existing:
                    raise UserError(_('The settlement entry already exists. Restore its payout link before continuing.'))
                continue
            if payout.settlement_move_id:
                raise UserError(_('This payout already has a settlement entry. Review it before continuing.'))
            source, bank, journal, transit = payout._check_settlement_configuration()
            activity, outflows = payout._check_settlement_transactions()
            liquidity = activity.move_id.line_ids.filtered(lambda line: line.account_id == source.default_account_id)
            if (len(liquidity) != len(activity) or liquidity.currency_id != payout.currency_id
                    or payout.currency_id.compare_amounts(sum(liquidity.mapped('amount_currency')), payout.amount)):
                raise UserError(_('The booked Shopify liquidity entries do not agree to this payout.'))
            balance = sum(liquidity.mapped('balance'))
            if outflows:
                move = outflows.move_id
                transit_line = move.line_ids.filtered(lambda line: line.account_id == transit)
                source_line = move.line_ids.filtered(lambda line: line.account_id == source.default_account_id)
                if (len(move.line_ids) != 2 or len(transit_line) != 1 or len(source_line) != 1
                        or transit_line.currency_id != payout.currency_id
                        or payout.currency_id.compare_amounts(transit_line.amount_currency, payout.amount)
                        or move.company_currency_id.compare_amounts(transit_line.balance, balance)):
                    raise UserError(_('The existing payout outflow must debit the configured transit account and '
                                      'credit Shopify clearing for the exact settlement amount. Review its accounting; '
                                      'a second transfer will not be created.'))
                move.write({
                    'shopify_settlement_payout_id': payout.id,
                    'shopify_settlement_instance_id': payout.instance_id.id,
                    'shopify_settlement_reference': payout.payout_reference_id,
                })
            else:
                label = _('Shopify payout %s', payout.payout_reference_id)
                move = self.env['account.move'].with_company(source.company_id).create({
                    'move_type': 'entry', 'journal_id': journal.id,
                    'date': payout.payout_date, 'ref': label,
                    'currency_id': payout.currency_id.id,
                    'shopify_settlement_payout_id': payout.id,
                    'shopify_settlement_instance_id': payout.instance_id.id,
                    'shopify_settlement_reference': payout.payout_reference_id,
                    'line_ids': [Command.create({
                        'name': label, 'account_id': account.id,
                        'currency_id': payout.currency_id.id,
                        'amount_currency': sign * payout.amount,
                        'debit': max(sign * balance, 0), 'credit': max(-sign * balance, 0),
                    }) for account, sign in ((transit, 1), (source.default_account_id, -1))],
                })
                move.action_post()
                transit_line = move.line_ids.filtered(lambda line: line.account_id == transit)
            payout.write({
                'settlement_move_id': move.id, 'settlement_line_id': transit_line.id,
                'settlement_bank_journal_id': bank.id,
                'state': 'validated',
            })
            payout.message_post(body=_('Net settlement entry %s is ready for bank reconciliation.', move.name))
        if len(self) == 1:
            return self.action_view_settlement_transfer()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def validate_statement(self):
        self._lock_settlement_payouts()
        for payout in self:
            payout._check_settlement_transactions()
        result = super().validate_statement()
        automatic = self.filtered(lambda payout: payout.instance_id.shopify_auto_settlement_transfer
                                  and not payout.currency_id.is_zero(payout.amount))
        if automatic:
            automatic.action_create_settlement_transfer()
        return result

    def action_view_settlement_transfer(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Net Settlement Transfer'),
            'res_model': 'account.move', 'res_id': self.settlement_move_id.id,
            'view_mode': 'form', 'target': 'current',
        }

    def action_open_settlement_bank(self):
        self.ensure_one()
        bank = self.settlement_bank_journal_id or self.instance_id.shopify_payout_bank_journal_id
        if not bank:
            raise UserError(_('Configure the receiving bank journal first.'))
        return self.env['account.bank.statement.line']._action_open_bank_reconciliation_widget(
            extra_domain=[('journal_id', '=', bank.id)])
