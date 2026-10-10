"""Repair open-period payout accounting when its Shopify data is reimported."""
import logging

from odoo import Command, fields, models, _
from odoo.exceptions import UserError
from psycopg2 import OperationalError

_logger = logging.getLogger(__name__)


class ShopifyPayoutReimport(models.Model):
    _inherit = 'shopify.payout.report.ept'

    reimport_reconciliation_issue = fields.Text(
        string='Payout Reimport Review', readonly=True, copy=False,
        help='Why automatic reconciliation was skipped or needs review after reimporting this payout.')

    def _check_payout_reimport_period(self):
        """Never use a user lock exception or shift a payout into another period."""
        self.ensure_one()
        company = self.instance_id.shopify_company_id.with_context(ignore_exceptions=True)
        journal = self.instance_id.shopify_settlement_report_journal_id
        if not self.payout_date:
            raise UserError(_('The payout has no accounting date.'))
        if self.payout_date <= company._get_user_fiscal_lock_date(journal, ignore_exceptions=True):
            raise UserError(_('The payout date is in a closed accounting period. Accounting was left unchanged.'))
        moves = self.payout_statement_line_ids.move_id | self.settlement_move_id
        for move in moves:
            has_tax = bool(move.line_ids.tax_ids or move.line_ids.tax_line_id)
            move_company = move.company_id.with_context(ignore_exceptions=True)
            lock_date = move_company._get_user_fiscal_lock_date(move.journal_id, ignore_exceptions=True)
            if has_tax:
                lock_date = max(lock_date, move_company.user_tax_lock_date)
            if move.date <= lock_date:
                raise UserError(_('Journal entry %s is in a closed accounting period. Accounting was left unchanged.', move.name))

    def _generate_imported_bank_statements(self):
        """Keep a locked payout's metadata available without creating shifted entries."""
        for payout in self:
            if payout.state not in ('draft', 'partially_generated'):
                continue
            try:
                with self.env.cr.savepoint():
                    payout._check_payout_reimport_period()
                    payout.generate_bank_statement()
            except OperationalError:
                raise
            except UserError as error:
                payout.reimport_reconciliation_issue = str(error)
        return True

    def _reset_generic_shop_cash_statement(self, statement):
        """Only replace a plain account posting, never undo another real match."""
        liquidity, suspense, counterpart = statement._seek_for_lines()
        if (statement.payment_ids or len(statement.line_ids) != 2 or len(liquidity) != 1
                or suspense or len(counterpart) != 1
                or statement.line_ids.matched_debit_ids or statement.line_ids.matched_credit_ids
                or statement.line_ids.tax_ids or statement.line_ids.tax_line_id):
            raise UserError(_('Shop Cash transaction %s already has linked accounting or a nonstandard posting. Review its match manually.', statement.shopify_transaction_id))
        statement.action_undo_reconciliation()

    def _reprocess_imported_bank_statement(self):
        """Preserve proven matches; atomically reset/rematch legacy Shop Cash rows."""
        self.ensure_one()
        self._lock_settlement_payouts()
        try:
            self._check_payout_reimport_period()
        except UserError as error:
            self.reimport_reconciliation_issue = str(error)
            return False
        self._generate_imported_bank_statements()
        if self.state in ('draft', 'partially_generated'):
            if not self.reimport_reconciliation_issue:
                self.reimport_reconciliation_issue = _('Some payout transactions have no statement line yet. Review the payout configuration and reimport.')
            return False
        statements = self.payout_statement_line_ids
        if not statements:
            return False
        issues, repaired = [], []
        for statement in statements.filtered(lambda row: row.payout_line_id.shop_cash_kind and row.is_reconciled):
            repaired_transaction = False
            try:
                with self.env.cr.savepoint():
                    transaction = statement.payout_line_id
                    allocations = self._resolve_shop_cash_payments(transaction)
                    try:
                        self._check_shop_cash_reconciliation(transaction, statement)
                    except UserError:
                        self._reset_generic_shop_cash_statement(statement)
                        self._reconcile_shop_cash_statement(statement)
                        repaired_transaction = transaction.transaction_id
                    else:
                        payments = self.env['account.payment'].union(*(payment for payment, _amount in allocations))
                        transaction.shop_cash_payment_ids = [Command.set(payments.ids)]
                if repaired_transaction:
                    repaired.append(repaired_transaction)
            except OperationalError:
                raise
            except UserError as error:
                _logger.warning('Shop Cash transaction %s needs review after reimport: %s', statement.shopify_transaction_id, error)
                issues.append(str(error))
            except Exception as error:
                _logger.exception('Shop Cash transaction %s needs review after reimport', statement.shopify_transaction_id)
                issues.append(str(error))
        if repaired or issues or statements.filtered(lambda row: not row.is_reconciled):
            self.state = 'partially_processed'
        if self.state in ('generated', 'partially_processed', 'processed'):
            try:
                # Normal processing isolates missing payments per statement line.
                # If final validation fails, keep the earlier atomic Shop Cash repairs.
                with self.env.cr.savepoint():
                    self.with_context(cron_process=True).process_bank_statement()
            except OperationalError:
                raise
            except UserError as error:
                _logger.warning('Payout %s needs review after reimport: %s', self.payout_reference_id, error)
                issues.append(str(error))
            except Exception as error:
                _logger.exception('Payout %s needs review after reimport', self.payout_reference_id)
                issues.append(str(error))
            remaining = statements.filtered(lambda row: not row.is_reconciled)
            if remaining:
                issues.append(_('%s statement transaction(s) still need reconciliation. Review the payout logs and payments.', len(remaining)))
        self.reimport_reconciliation_issue = '\n'.join(dict.fromkeys(issues)) or False
        if repaired:
            self.message_post(body=_('Payout reimport reset the generic Shop Cash posting and matched the existing payments for transaction(s): %s.', ', '.join(repaired)))
        return not issues
