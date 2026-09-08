# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
import logging
import time
from psycopg2 import OperationalError

from datetime import datetime, timedelta
from odoo import models, fields,api, _
from odoo.exceptions import UserError
from .. import shopify
from ..shopify.pyactiveresource.connection import ClientError
import ast
from odoo.tools.float_utils import float_is_zero
from .shopify_transaction_utils import normalize_shopify_id

_logger = logging.getLogger('Shopify Payout')


class ShopifyPaymentReportEpt(models.Model):
    _name = "shopify.payout.report.ept"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = "Shopify Payout Report"
    _order = 'id desc'

    name = fields.Char(size=256)
    instance_id = fields.Many2one('shopify.instance.ept', string="Instance")
    payout_reference_id = fields.Char(string="Payout Reference ID",
                                      help="The unique identifier of the payout")
    payout_date = fields.Date(help="The date the payout was issued.")
    payout_transaction_ids = fields.One2many('shopify.payout.report.line.ept', 'payout_id',
                                             string="Payout transaction lines")
    common_log_line_ids = fields.One2many("common.log.lines.ept", 'shopify_payout_report_line_id', string="Log Lines")
    currency_id = fields.Many2one('res.currency', string='Currency',
                                  help="currency code of the payout.")
    amount = fields.Float(string="Total Amount", help="The total amount of the payout.")
    statement_id = fields.Many2one('account.bank.statement', string="Bank Statement")
    payout_status = fields.Selection([('scheduled', 'Scheduled'), ('in_transit', 'In Transit'), ('paid', 'Paid'),
                                      ('failed', 'Failed'), ('cancelled', 'Cancelled')],
                                     help="The transfer status of the payout. The value will be one of the following\n"
                                          "- Scheduled:  The payout has been created and had transactions assigned to"
                                          "it, but it has not yet been submitted to the bank\n"
                                          "- In Transit: The payout has been submitted to the bank for processing.\n"
                                          "- Paid: The payout has been successfully deposited into the bank.\n"
                                          "- Failed: The payout has been declined by the bank.\n"
                                          "- Cancelled: The payout has been canceled by Shopify")
    state = fields.Selection([('draft', 'Draft'), ('partially_generated', 'Partially Generated'),
                              ('generated', 'Generated'), ('partially_processed', 'Partially Processed'),
                              ('processed', 'Processed'), ('validated', 'Validated')], string="Status",
                             default="draft", tracking=True)
    is_skip_from_cron = fields.Boolean(string="Skip From Schedule Actions", default=False)

    def get_payout_report(self, start_date, end_date, instance):
        """
        This method is used to import Payout reports and create record in Odoo.
        @param start_date:From Date(year-month-day)
        @param end_date: To Date(year-month-day)
        @param instance: Browsable shopify instance.
        @author: Maulik Barad on Date 27-Nov-2020.
        """
        log_line_obj = self.env['common.log.lines.ept']

        instance.connect_in_shopify()
        _logger.info("Import Payout Reports....")
        try:
            payout_reports = shopify.Payouts().find(status="paid", date_min=start_date, date_max=end_date, limit=250)
        except Exception as error:
            message = ("System tried to import the payout report but an error occurred : %s.\n"
					   "Action Items:\n"
					   "- Might be chance the payout report is not available no the Shopify store.\n"
					   "- Tried to import the payout with different date range.") % error
            log_line_obj.create_common_log_line_ept(shopify_instance_id=instance.id, module="shopify_ept",
                                                    message=message,
                                                    model_name=self._name)
            _logger.info(message)
            return False

        payouts = self.create_payout_reports(self.shopify_list_all_transactions(payout_reports), instance)
        payouts = payouts.sorted(key=lambda x: x.id, reverse=True)

        self._cr.commit()
        _logger.info("Payout Reports are Created. Generating Bank statement lines...")
        payouts.generate_bank_statement()

        instance.write({'payout_last_import_date': end_date})
        _logger.info("Payout Reports are Imported.")
        return True

    def create_payout_reports(self, payout_reports, instance):
        """
        This method is used to create records of Payout report from the data.
        @param instance: Record of the Instance.
        @param payout_reports: List of Payout reports.
        @author: Maulik Barad on Date 03-Dec-2020.
        """
        payouts = self
        for payout_report in payout_reports:
            payout_data = payout_report.to_dict()
            payout_id = payout_data.get('id')
            payout = self.search([('instance_id', '=', instance.id),
                                  ('payout_reference_id', '=', payout_id)])
            if payout:
                _logger.info("Existing Payout Report found for %s.", payout_id)
                payout.refresh_payout_transaction_links()
                payouts += payout
                continue
            payout_vals = self.prepare_payout_vals(payout_data, instance)
            payout = self.create(payout_vals)

            if not payout:
                continue
            payouts += payout
            _logger.info("Payout Report created for %s. Importing Transaction lines..", payout_id)
            payout.create_payout_transaction_lines(payout_data)

        return payouts

    def create_payout_transaction_lines(self, payout_data):
        """
        Gets Payout Transactions and creates transaction lines from that.
        @param payout_data: Data of the payout.
        @author: Maulik Barad on Date 03-Dec-2020.
        """
        shopify_payout_report_line_obj = self.env['shopify.payout.report.line.ept']

        transaction_all = shopify.Transactions().find(payout_id=self.payout_reference_id, limit=250)
        transaction_all = self.shopify_list_all_transactions(transaction_all)
        for transaction in transaction_all:
            transaction_data = transaction.to_dict()
            transaction_vals = self.prepare_transaction_vals(transaction_data, self.instance_id)
            shopify_payout_report_line_obj.create(transaction_vals)

        # Use the fees on the canonical balance transactions, including reserve
        # and retried-payout fees that are absent from the old summary subset.
        fees_amount = sum(self.payout_transaction_ids.mapped('fee'))
        shopify_payout_report_line_obj.create({
            'payout_id': self.id or False,
            'transaction_type': 'fees',
            'amount': -fees_amount,
            'fee': 0.0,
            'net_amount': -fees_amount,
            'currency_id': self.currency_id.id,
            'is_remaining_statement': True
        })
        _logger.info("Transaction lines are added for %s.", self.payout_reference_id)
        return True

    def refresh_payout_transaction_links(self):
        """Backfill Shopify source IDs on a previously imported payout."""
        self.ensure_one()
        transactions = shopify.Transactions().find(
            payout_id=self.payout_reference_id, limit=250
        )
        transactions = self.shopify_list_all_transactions(transactions)
        existing_lines = {
            line.transaction_id: line
            for line in self.payout_transaction_ids.filtered("transaction_id")
        }
        statement_line_obj = self.env["account.bank.statement.line"]
        for transaction in transactions:
            transaction_data = transaction.to_dict()
            payout_line = existing_lines.get(
                normalize_shopify_id(transaction_data.get("id"))
            )
            if not payout_line:
                continue
            values = self.prepare_transaction_vals(transaction_data, self.instance_id)
            link_values = {
                "source_id": values.get("source_id"),
                "source_order_id": values.get("source_order_id"),
                "source_order_transaction_id": values.get("source_order_transaction_id"),
                "source_type": values.get("source_type"),
                "order_id": values.get("order_id"),
            }
            payout_line.write(link_values)
            statement_line_obj.search([
                ("payout_line_id", "=", payout_line.id)
            ]).write({
                "shopify_order_transaction_id":
                    payout_line.source_order_transaction_id
            })
        return True

    def shopify_list_all_transactions(self, result):
        """Follow collection-owned links and retain the first and final pages."""
        rows = list(result)
        while hasattr(result, 'has_next_page') and result.has_next_page():
            try:
                result = result.next_page(no_cache=True)
            except ClientError as error:
                if not hasattr(error, 'response') or error.response.code != 429:
                    raise
                time.sleep(int(float(error.response.headers.get('Retry-After', 5))))
                result = result.next_page(no_cache=True)
            rows.extend(result)
        return rows

    def prepare_transaction_vals(self, data, instance):
        """
        Use : Based on transaction data prepare transaction vals.
        Added by : Deval Jagad
        Added on : 05/06/2020
        Task ID : 164126
        :param data: Transaction data in dict{}.
        :param instance: Browsable record of instance.
        :return: Payout vals{}
        """
        currency_obj = self.env['res.currency']
        sale_order_obj = self.env['sale.order']
        transaction_id = data.get('id', '')
        source_id = data.get('source_id', '')
        source_order_id = data.get('source_order_id', '')
        source_order_transaction_id = data.get('source_order_transaction_id', '')
        source_type = data.get('source_type', '')
        raw_transaction_type = data.get('type', '')
        adjustment_reason = data.get('adjustment_reason', '')
        transaction_type = 'tax_adjustment' if adjustment_reason == 'tax_adjustment' else raw_transaction_type
        amount = data.get('amount', 0.0)
        fee = data.get('fee', 0.0)
        net_amount = data.get('net', 0.0)
        currency = data.get('currency', '')

        order_id = False
        if source_order_id:
            order_id = sale_order_obj.search([('shopify_order_id', '=', source_order_id),
                                              ('shopify_instance_id', '=', instance.id)],
                                             limit=1)

        transaction_vals = {
            'payout_id': self.id or False,
            'transaction_id': transaction_id,
            'source_id': normalize_shopify_id(source_id),
            'source_order_id': source_order_id,
            'source_order_transaction_id': normalize_shopify_id(source_order_transaction_id),
            'source_type': source_type or False,
            'transaction_type': transaction_type,
            'raw_transaction_type': raw_transaction_type,
            'adjustment_reason': adjustment_reason,
            'order_id': order_id and order_id.id,
            'amount': amount,
            'fee': fee,
            'net_amount': net_amount,
            'is_remaining_statement': True
        }

        currency_id = currency_obj.search([('name', '=', currency)], limit=1)
        if currency_id:
            transaction_vals.update({'currency_id': currency_id.id})

        return transaction_vals

    def shopify_view_bank_statement(self):
        """
        @author: Haresh Mori , 30th Oct 2023
        This function is used to show generated bank statement from Payout report.
        """
        self.ensure_one()
        return self.env['account.bank.statement.line']._action_open_bank_reconciliation_widget(
            extra_domain=[('payout_id', '=', self.id)]
        )

    def prepare_payout_vals(self, data, instance):
        """
        Use : Based on payout data prepare payout vals.
        Added by : Deval Jagad
        Added on : 05/06/2020
        Task ID : 164126
        :param data: Payout data in dict{}.
        :param instance: Browsable record of instance.
        :return: Payout vals{}
        """
        currency_obj = self.env['res.currency']
        payout_reference_id = data.get('id')
        payout_date = data.get('date', '')
        payout_status = data.get('status', '')
        currency = data.get('currency', '')
        amount = data.get('amount', 0.0)

        payout_vals = {
            'payout_reference_id': payout_reference_id,
            'payout_date': payout_date,
            'payout_status': payout_status,
            'amount': amount,
            'instance_id': instance.id
        }
        currency_id = currency_obj.search([('name', '=', currency)], limit=1)
        if currency_id:
            payout_vals.update({'currency_id': currency_id.id})
        return payout_vals

    def check_process_statement(self):
        """
        Use : Using this method visible/Invisible the statement execution button.
        Added by : Deval Jagad
        Added on : 05/06/2020
        Task ID : 164126
        """
        all_statement_processed = True
        if self.payout_transaction_ids and any(line.is_remaining_statement for line in self.payout_transaction_ids.filtered(
                lambda line: not self.currency_id.is_zero(line.amount))):
            all_statement_processed = False
        return all_statement_processed

    def generate_bank_statement(self):
        """Generate missing statement lines for selected, eligible payouts."""
        if not self:
            return True
        self.check_access('write')
        self.flush_recordset(['state'])
        # Serialize generation with imports, scheduled jobs and other users.
        # Updating the payout below also makes a concurrent stale PostgreSQL
        # snapshot fail with a serialization error, so Odoo can retry it.
        self.env.cr.execute(
            "SELECT id FROM shopify_payout_report_ept "
            "WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        self.invalidate_recordset(['state', 'payout_transaction_ids'])
        success = True
        for payout in self.sorted('id'):
            if payout.state not in ('draft', 'partially_generated'):
                continue
            journal = payout.check_journal_and_currency()
            if not journal:
                success = False
                continue
            payout._create_bank_statement_lines_for_payout_report()

            if payout.check_process_statement():
                state = 'generated'
            else:
                state = 'partially_generated'

            payout.write({'state': state, "is_skip_from_cron": False})

        return success

    def create_bank_statement_lines_for_payout_report(self):
        """Keep legacy callers on the same locked, idempotent generation path."""
        return self.generate_bank_statement()

    def _create_bank_statement_lines_for_payout_report(self):
        """
        This method creates bank statement lines from the transaction lines of Payout report.
        @author: Maulik Barad on Date 02-Dec-2020.
        """
        self.ensure_one()
        partner_obj = self.env['res.partner']
        bank_statement_line_obj = self.env['account.bank.statement.line']
        log_lines = []
        account_payment_obj = self.env['account.payment']
        sale_order_obj = self.env["sale.order"]

        # The relation is authoritative: flags can be stale after an interrupted
        # import or a manually removed statement line. The caller holds the
        # payout lock throughout this lookup and all statement-line creation.
        transactions = self.payout_transaction_ids
        existing_transactions = bank_statement_line_obj.search([
            ('payout_line_id', 'in', transactions.ids),
        ]).mapped('payout_line_id')
        existing_transactions.filtered('is_remaining_statement').write({
            'is_remaining_statement': False,
        })
        transaction_ids = transactions - existing_transactions
        transaction_ids.filtered(lambda line: not line.is_remaining_statement).write({
            'is_remaining_statement': True,
        })
        for transaction in transaction_ids:
            if self.currency_id.is_zero(transaction.amount):
                transaction.is_remaining_statement = False
                continue
            order_id = transaction.order_id
            if transaction.transaction_type in ['charge', 'refund', 'payment_refund'] and not order_id:
                source_order_id = transaction.source_order_id
                order_id = sale_order_obj.search([('shopify_order_id', '=', source_order_id),
                                                  ('shopify_instance_id', '=', self.instance_id.id)],
                                                 limit=1)
                if order_id:
                    transaction.order_id = order_id
                else:
                    message = ("System tried to automatically reconcile, but Order: %s was not found in the system.\n"
							   "Action Items:\n"
							   "- Import the missing order before processing the payout report.\n"
							   "- You can import orders using the operation wizard.") % transaction.source_order_id
                    log_lines.append({'message': message,
                                      'shopify_payout_report_line_id': transaction.id})
                    # We can not use shopify order reference here because it may create duplicate name,
                    # and name of journal entry should be unique per company. So here I have used transaction Id
                    bank_line_vals = {
                        # 'name': transaction.transaction_id,
                        'payment_ref': transaction.transaction_id,
                        'date': self.payout_date,
                        'amount': transaction.amount,
                        # 'statement_id': bank_statement_id.id,
                        'shopify_transaction_id': transaction.transaction_id,
                        'shopify_order_transaction_id': transaction.source_order_transaction_id,
                        "shopify_transaction_type": transaction.transaction_type,
                        'sequence': 1000,
                        'journal_id': self.instance_id.shopify_settlement_report_journal_id.id,
                        'payout_id': self.id,
                        'payout_line_id': transaction.id
                    }
                    bank_statement_line_obj.create(bank_line_vals)
                    transaction.is_remaining_statement = False
                    continue

            partner = partner_obj._find_accounting_partner(order_id.partner_id)
            exact_payment = self.find_payment_for_payout_transaction(transaction)
            invoice = self.env["account.move"]
            if exact_payment:
                reference = exact_payment.name
                if "reconciled_invoice_ids" in exact_payment._fields:
                    payment_invoices = exact_payment.reconciled_invoice_ids
                elif "invoice_ids" in exact_payment._fields:
                    payment_invoices = exact_payment.invoice_ids
                else:
                    payment_invoices = self.env["account.move"]
                invoice = payment_invoices.filtered(
                    lambda move: move.state == "posted"
                )[:1]
            else:
                domain, invoice, log_line = self.check_for_invoice_refund(transaction, log_lines)
                # An ambiguous invoice is not suitable for statement metadata.
                if len(invoice) != 1:
                    invoice = self.env['account.move']
                if domain:
                    payment_reference = account_payment_obj.search(domain, limit=1)
                    reference = payment_reference.name if payment_reference else invoice.name or ''
                else:
                    reference = transaction.order_id.name

            if transaction.amount:
                name = False
                if transaction.transaction_type not in ['charge', 'refund', 'payment_refund']:
                    reference = transaction.transaction_type + "/"
                    if transaction.transaction_id:
                        reference += transaction.transaction_id
                    else:
                        reference += self.payout_reference_id
                else:
                    if order_id.name:
                        name = transaction.transaction_type + "_" + order_id.name + "/" + transaction.transaction_id
                counter_part_account_id = self.instance_id.transaction_line_ids.filtered(lambda l: l.transaction_type
                                                                                                   ==
                                                                                                   transaction.transaction_type).account_id
                bank_line_vals = {
                    # 'name': name or reference,
                    'payment_ref': name or reference,
                    'date': self.payout_date,
                    'partner_id': partner and partner.id,
                    'amount': transaction.amount,
                    # 'statement_id': bank_statement_id.id,
                    'sale_order_id': order_id.id,
                    'shopify_transaction_id': transaction.transaction_id,
                    'shopify_order_transaction_id': transaction.source_order_transaction_id,
                    "shopify_transaction_type": transaction.transaction_type,
                    'sequence': 1000,
                    'journal_id': self.instance_id.shopify_settlement_report_journal_id.id,
                    'counterpart_account_id': counter_part_account_id.id,
                    'payout_id': self.id,
                    'payout_line_id': transaction.id
                }
                if invoice and invoice.move_type == "out_refund":
                    bank_line_vals.update({"refund_invoice_id": invoice.id})
                bank_statement_line_obj.create(bank_line_vals)
                transaction.is_remaining_statement = False

        if log_lines:
            self.set_payout_log_line(log_lines)

            note = "Bank statement lines are generated but will not reconcile automatically for Transaction IDs : "
            for log_line in log_lines:
                note += ',' + log_line.get('message').split()[2] if log_line.get('message') else ''
            self.message_post(body=note)
            if self.instance_id.is_shopify_create_schedule:
                self.common_log_line_ids.create_payout_schedule_activity(note, self)
        return True

    def find_payment_for_payout_transaction(self, transaction):
        """Match a charge or refund independently, including across payout dates."""
        payment_obj = self.env['account.payment']
        if not transaction or transaction.transaction_type not in ('charge', 'refund', 'payment_refund'):
            return payment_obj
        transaction_id = transaction.source_order_transaction_id
        payment_type = 'inbound' if transaction.transaction_type == 'charge' else 'outbound'
        payment = payment_obj
        if transaction_id:
            payment = payment_obj.search([
                ('shopify_instance_id', '=', self.instance_id.id),
                ('shopify_order_transaction_id', '=', transaction_id),
                ('company_id', '=', self.instance_id.shopify_company_id.id),
                ('payment_type', '=', payment_type),
                ('state', 'not in', ('draft', 'canceled', 'rejected')),
            ], limit=1)
        if payment or not transaction.order_id:
            return payment
        move_type = 'out_invoice' if payment_type == 'inbound' else 'out_refund'
        invoices = transaction.order_id.invoice_ids.filtered(
            lambda move: move.state == 'posted' and move.move_type == move_type)
        # Invoice payment_state may become "reversed" after a later refund.
        # The original incoming payment still belongs to its original payout.
        candidates = invoices.reconciled_payment_ids.filtered(
            lambda item: item.payment_type == payment_type
            and item.state not in ('draft', 'canceled', 'rejected')
            and item.company_id == self.instance_id.shopify_company_id
            and item.currency_id == self.currency_id
            and (not item.shopify_instance_id or item.shopify_instance_id == self.instance_id)
            and (not transaction_id or not item.shopify_order_transaction_id)
            and self.currency_id.compare_amounts(item.amount, abs(transaction.amount)) == 0)
        if len(candidates) == 1:
            if transaction_id:
                candidates.write({
                    'shopify_instance_id': self.instance_id.id,
                    'shopify_order_transaction_id': transaction_id,
                })
            return candidates
        return payment

    def check_for_invoice_refund(self, transaction, log_lines):
        """
        This method is used to search for invoice or refund and then prepare domain as that..
        @param transaction: record of the transaction line.
        @author: Maulik Barad on Date 03-Dec-2020.
        """
        invoice_ids = self.env["account.move"]
        domain = []
        order_id = transaction.order_id

        if transaction.transaction_type == 'charge':
            invoice_ids = order_id.invoice_ids.filtered(lambda x:
                                                        x.state == 'posted' and x.move_type == 'out_invoice' and
                                                        x.amount_total == transaction.amount)
            if not invoice_ids:
                message = ("System tried to automatically reconcile, but the invoice total does not properly match for Order: %s.\n"
							"Action Items:\n"
					        "- Verify the order total, invoice total, and bank statement line amount.\n"
							"- The difference might be due to taxes, discounts, or shipping fees.\n"
							"- Perform manual adjustments in the invoice "
						    "and then reconcile again.") %(order_id.name or transaction.source_order_id)
                log_lines.append({'message': message,
                                  'shopify_payout_report_line_id': transaction.id})
                return domain, invoice_ids, log_lines
            domain += [('amount', '=', transaction.amount), ('payment_type', '=', 'inbound')]
        elif transaction.transaction_type in ['refund', 'payment_refund']:
            invoice_ids = order_id.invoice_ids.filtered(lambda x:
                                                        x.state == 'posted' and x.move_type == 'out_refund' and
                                                        x.amount_total == -transaction.amount)
            if not invoice_ids:
                instance = order_id.shopify_instance_id
                instance.connect_in_shopify()
                shopify_order = shopify.Order().find(order_id.shopify_order_id)
                order_data = shopify_order.to_dict()
                shopify_status = order_data.get("financial_status")
                if shopify_status in ["refunded", "partially_refunded"] and order_data.get("refunds"):
                    created_by = 'import'
                    queue_line = self.env["shopify.order.data.queue.line.ept"]
                    order_id.process_order_refund_data_ept(shopify_status, order_data, order_id, created_by, instance,
                                                           queue_line)
                invoice_ids = order_id.invoice_ids.filtered(lambda x:
                                                            x.state == 'posted' and x.move_type == 'out_refund' and
                                                            x.amount_total == -transaction.amount)
            if not invoice_ids:
                message = ("System tried to automatically reconcile, but the refund amount does not match for Order: %s.\n"
							"Action Items:\n"
							"- Verify the refund invoice and ensure the refund total matches the bank statement line.\n"
							"- The difference might be due to taxes, discounts, or shipping fees.\n"
							"- Perform manual adjustments in the refund invoice "
							"and reconcile again.") % (order_id.name or transaction.source_order_id)
                log_lines.append({'message': message,
                                  'shopify_payout_report_line_id': transaction.id})
                return domain, invoice_ids, log_lines
            domain += [('amount', '=', -transaction.amount), ('payment_type', '=', 'outbound')]

        domain.append(('memo', 'in', invoice_ids.mapped("payment_reference")))
        return domain, invoice_ids, log_lines

    def check_journal_and_currency(self):
        """
        This method checks for configured journal and its currency.
        @author: Maulik Barad on Date 02-Dec-2020.
        """
        journal = self.instance_id.shopify_settlement_report_journal_id
        if not journal:
            message_body = ("Configuration mismatch to import the payout report.\n"
                           "The Payout Report Journal in the instance is not configured in the settings.\n"
                           "Please configure it from: Shopify → Configuration → Settings.")
            if self._context.get('cron_process'):
                self.message_post(body=_(message_body))
                self.is_skip_from_cron = True
                return False
            raise UserError(_(message_body))

        if journal.company_id != self.instance_id.shopify_company_id or journal.type != 'bank':
            raise UserError(_('The Payout Report Journal must be a bank journal belonging to the Shopify company.'))
        currency_id = (journal.currency_id or journal.company_id.currency_id).id
        if currency_id != self.currency_id.id:
            message_body = ("System tried to import the payout report but found a mismatch between the payout report "
							"currency and the currency in the journal configured in the instance.\n"
							"Action Items:\n"
							"- Ensure the payout report currency and the journal/instance currency are the same.\n"
							"- Re-import the payout report using the operation wizard.")
            self.message_post(body=_(message_body))
            raise UserError(_(message_body))
        return journal

    def check_reconciled_transactions(self, transaction, log_lines, aml_rec=False):
        """
        This method is used to check if the transaction line already reconciled or not.
        @param transaction: Record of the transaction.
        @param aml_rec: Record of move line.
        """
        reconciled = False
        if aml_rec and aml_rec.statement_id:
            message = 'Transaction line %s is already reconciled.' % transaction.transaction_id
            log_lines.append({'message': message,
                              'shopify_payout_report_line_id': transaction.id})
            reconciled = True
        return reconciled, log_lines

    def convert_move_amount_currency(self, bank_statement_line, moveline, amount, date):
        """
        This function is used to convert currency.
        :param: bank_statement_line: account.bank.statement.line()
        :param: moveline: account.move.line()
        :param: amount: float
        :param: date: datetime()
        :return: int - currency id, float - amount_currency
        """
        amount_currency = 0.0
        if moveline.company_id.currency_id.id != bank_statement_line.currency_id.id:
            amount_currency = moveline.currency_id._convert(moveline.amount_currency,
                                                            bank_statement_line.currency_id,
                                                            bank_statement_line.company_id,
                                                            date)
        elif (moveline.move_id and moveline.move_id.currency_id.id != bank_statement_line.currency_id.id):
            amount_currency = moveline.move_id.currency_id._convert(amount,
                                                                    bank_statement_line.currency_id,
                                                                    bank_statement_line.company_id,
                                                                    date)
        currency = moveline.currency_id.id
        return currency, amount_currency

    def get_invoices_for_reconcile(self, statement_line):
        """Resolve only this payout's order and the correct invoice direction."""
        transaction = statement_line.payout_line_id
        if not transaction:
            return self.env['account.move']
        order = transaction.order_id
        if not order and transaction.source_order_id:
            order = self.env['sale.order'].search([
                ('shopify_order_id', '=', transaction.source_order_id),
                ('shopify_instance_id', '=', self.instance_id.id),
            ], limit=1)
            transaction.order_id = order
        if not order:
            return self.env['account.move']
        statement_line.sale_order_id = order
        is_refund = transaction.transaction_type in ('refund', 'payment_refund')
        move_type = 'out_refund' if is_refund else 'out_invoice'
        invoices = order.invoice_ids.filtered(lambda move: move.state == 'posted' and move.move_type == move_type)
        if is_refund and not invoices:
            self.check_for_invoice_refund(transaction, [])
            invoices = order.invoice_ids.filtered(lambda move: move.state == 'posted' and move.move_type == move_type)
        if is_refund and len(invoices) == 1:
            statement_line.refund_invoice_id = invoices
        return invoices

    def get_paid_move_line_amount(self, statement_line, paid_invoices):
        """Never net an order's incoming payment against its later refund."""
        payment_type = 'outbound' if statement_line.amount < 0 else 'inbound'
        candidates = []
        for payment in paid_invoices.reconciled_payment_ids.filtered(
                lambda payment: payment.payment_type == payment_type
                and payment.company_id == self.instance_id.shopify_company_id
                and (not payment.shopify_instance_id or payment.shopify_instance_id == self.instance_id)
                and (not statement_line.shopify_order_transaction_id
                     or not payment.shopify_order_transaction_id
                     or payment.shopify_order_transaction_id == statement_line.shopify_order_transaction_id)):
            data = self.get_payment_move_line_amount(statement_line, payment)
            if data[2] and self.currency_id.compare_amounts(data[0], statement_line.amount) == 0:
                candidates.append(data)
        return candidates[0] if len(candidates) == 1 else (0.0, [], self.env['account.move.line'])

    def _payout_move_line_residual(self, statement_line, line):
        currency = statement_line.currency_id or self.currency_id
        if line.currency_id == currency:
            return line.amount_residual_currency
        return line.company_currency_id._convert(
            line.amount_residual, currency, line.company_id, statement_line.date)

    def get_payment_move_line_amount(self, statement_line, payment):
        """Use only the open liquidity/outstanding items of this payment."""
        liquidity, _counterpart, _writeoffs = payment._seek_for_lines()
        lines = liquidity.filtered(lambda line: line.move_id.state == 'posted'
                                   and line.account_id.reconcile and not line.reconciled
                                   and line.balance * statement_line.amount > 0)
        total = sum(self._payout_move_line_residual(statement_line, line) for line in lines)
        return total, [self.currency_id.id] if lines else [], lines

    def get_unpaid_move_line_data(self, statement_line, unpaid_invoices):
        lines = unpaid_invoices.line_ids.filtered(
            lambda line: line.account_type == 'asset_receivable' and not line.reconciled
            and line.balance * statement_line.amount > 0)
        data = []
        total = 0.0
        for line in lines:
            amount = self._payout_move_line_residual(statement_line, line)
            data.append({'id': line.id, 'balance': -amount, 'currency_id': line.currency_id.id})
            total += amount
        return total, [self.currency_id.id] if lines else [], data

    def reconcile_invoice_refund(self, statement_line, move_line_total_amount, currency_ids, move_line_data,
                                 paid_move_lines, log_lines):
        if self.currency_id.compare_amounts(statement_line.amount, move_line_total_amount) == 0:
            try:
                move_line_ids = [data['id'] for data in move_line_data]
                move_line_ids.extend(line.id for line in paid_move_lines)
                if move_line_ids:
                    with self.env.cr.savepoint():
                        self.shopify_reconcile_bank_statement_line_ept(statement_line.id, move_line_ids)
            except OperationalError:
                # Odoo must retry serialization/deadlock failures as a whole request.
                raise
            except Exception as error:
                message = ("System tried to automatically reconcile but encountered an error while processing the statement line: %s \n"
							"Action Items:\n"
							"- Verify the payment details.\n"
							"- Perform manual reconciliation if needed.") % (statement_line.payment_ref + ".\n" + str(error))
                transaction_line = self.payout_transaction_ids.filtered(
                    lambda x: x.transaction_type == statement_line.shopify_transaction_type and
                              x.transaction_id == statement_line.shopify_transaction_id and x.amount ==
                              statement_line.amount)
                log_lines.append({"message": message,
                                  "shopify_payout_report_line_id": transaction_line.id})
                #statement_line.button_undo_reconciliation()
        else:
            log_lines.append({
                'message': _('Payout transaction %(transaction)s is %(amount)s, but its available payment/refund '
                             'balance is %(available)s. Match the original charge and any later refund separately; '
                             'review missing or already reconciled payments.',
                             transaction=statement_line.shopify_transaction_id,
                             amount=statement_line.amount, available=move_line_total_amount),
                'shopify_payout_report_line_id': statement_line.payout_line_id.id,
            })
        return log_lines

    def shopify_reconcile_bank_statement_line_ept(self, statement_line_id, move_line_id):
        """
        This method will help to reconcile shopify bank statement line.
        """
        wizard = self.env['bank.rec.widget'].with_context(default_st_line_id=statement_line_id).new({})
        wizard._action_add_new_amls(self.env['account.move.line'].browse(move_line_id))
        wizard.with_context(dynamic_unlink=True)._action_validate()

    def shopify_reconcile_other_bank_statement_line_ept(self, statement_line_id, move_line_id):
        """This method is use to reconcile the other bank statement line which type has fee, payout"""

        wizard = self.env['bank.rec.widget'].with_context(default_st_line_id=statement_line_id.id).new({})
        if statement_line_id.amount < 0:
            line = wizard.line_ids.filtered(lambda x: float_is_zero(x.credit, precision_digits=2))
        else:
            line = wizard.line_ids.filtered(lambda x: not float_is_zero(x.credit, precision_digits=2))
        transaction_type = statement_line_id.shopify_transaction_type
        transaction_account_line = self.instance_id.transaction_line_ids.filtered(
            lambda x: x.transaction_type == transaction_type)
        wizard._js_action_mount_line_in_edit(line.index)
        line.account_id = transaction_account_line.account_id
        wizard._line_value_changed_account_id(line)
        wizard.with_context(dynamic_unlink=True)._action_validate()

    def reconcile_other_transactions(self, statement_line, move_line_data, log_lines):

        transaction_type = statement_line.shopify_transaction_type
        transaction_account_line = self.instance_id.transaction_line_ids.filtered(
            lambda x: x.transaction_type == transaction_type)

        if not transaction_account_line:
            message = ("Can't reconcile %s.\nPlease configure an account for the Transaction type : %s.\n"
                      "Go to: Configuration → Instances → Open Instance → Payout Configuration Tab, and set the "
                      "appropriate account.") % (statement_line.payment_ref, transaction_type)
            if self._context.get("cron_process"):
                log_lines.append({"message": message})
                return log_lines
            raise UserError(_(message))
        move_line_data.append({
            "id": statement_line.move_id.id,
            "name": statement_line.payment_ref,
            "balance": -statement_line.amount,
            "account_id": transaction_account_line[0].account_id.id
        })
        if move_line_data:
            data = move_line_data[0]
            move_line_id = data.get('id', False)
            # self.shopify_reconcile_bank_statement_line_ept(statement_line.id, move_line_id)
            self.shopify_reconcile_other_bank_statement_line_ept(statement_line, move_line_id)
        return log_lines

    def process_bank_statement(self):
        """
        This method is used to process the bank statement.
        @author: Maulik Barad on Date 07-Dec-2020.
        """
        statement_line_obj = self.env['account.bank.statement.line']
        self.ensure_one()
        self._lock_settlement_payouts()
        log_lines = []
        _logger.info("Processing Bank Statement line of payout : %s.", self.name)
        statement_lines = statement_line_obj.search([('payout_id', '=', self.id)])
        for statement_line in statement_lines.filtered(lambda x: not x.is_reconciled):
            move_line_data = []
            move_line_total_amount = 0.0
            currency_ids = []
            paid_move_lines = []
            try:
                with self.env.cr.savepoint():
                    if statement_line.shopify_transaction_type in ["charge", "refund", "payment_refund"]:
                        payout_transaction = statement_line.payout_line_id
                        exact_payment = self.find_payment_for_payout_transaction(payout_transaction)
                        if exact_payment and exact_payment.move_id:
                            move_line_total_amount, currency_ids, paid_move_lines = \
                                self.get_payment_move_line_amount(statement_line, exact_payment)
                        elif exact_payment:
                            # Odoo 18 payments without outstanding accounts may have
                            # no journal entry. Reconcile their open invoice instead.
                            move_type = 'out_refund' if statement_line.amount < 0 else 'out_invoice'
                            invoices = exact_payment.invoice_ids.filtered(
                                lambda move: move.state == 'posted' and move.move_type == move_type)
                            move_line_total_amount, currency_ids, move_line_data = self.get_unpaid_move_line_data(
                                statement_line, invoices)
                        else:
                            invoices = self.get_invoices_for_reconcile(statement_line)
                            if not invoices:
                                continue

                            paid_invoices = invoices.filtered(lambda x: x.reconciled_payment_ids)
                            unpaid_invoices = invoices.filtered(
                                lambda x: not x.reconciled_payment_ids.filtered('move_id') and x.amount_residual)

                            if paid_invoices:
                                move_line_total_amount, currency_ids, paid_move_lines = self.get_paid_move_line_amount(
                                    statement_line, paid_invoices)

                            if unpaid_invoices and not paid_move_lines:
                                move_line_total_amount, currency_ids, move_line_data = self.get_unpaid_move_line_data(
                                    statement_line, unpaid_invoices)

                        log_line = self.reconcile_invoice_refund(statement_line, move_line_total_amount, currency_ids,
                                                                 move_line_data, paid_move_lines, log_lines)
                    else:
                        log_line = self.reconcile_other_transactions(statement_line, move_line_data, log_lines)
                    # if log_line:
                    #     log_lines.append(log_line)
            except OperationalError:
                raise
            except Exception as error:
                if self._context.get("cron_process"):
                    message = ("System tried to automatically reconcile but encountered an error while processing the statement line: %s \n"
								"Action Items:\n"
								"- Verify the payment details.\n"
								"- Perform manual reconciliation if needed.") % (statement_line.payment_ref + ".\n" + str(error))
                    transaction_line = self.payout_transaction_ids.filtered(
                        lambda x: x.transaction_type == statement_line.shopify_transaction_type and
                                  x.transaction_id == statement_line.shopify_transaction_id and x.amount ==
                                  statement_line.amount)
                    log_lines.append({"message": message,
                                      "shopify_payout_report_line_id": transaction_line.id})
                else:
                    raise UserError(error)
        if log_lines:
            self.set_payout_log_line(log_lines)
            note = ""
            for log_line in self.common_log_line_ids:
                note += str(log_line.message) + "<br/>"
            self.message_post(body=note)

            if self.instance_id.is_shopify_create_schedule:
                self.common_log_line_ids.create_payout_schedule_activity(note, self)

        if statement_lines.filtered(lambda x: not x.is_reconciled):
            self.write({'state': 'partially_processed'})
        else:
            self.write({'state': 'processed'})
            self.validate_statement()

        return True

    def validate_statement(self):
        """
        Use : To reconcile the bank statement.
        @author: Maulik Barad on Date 07-Dec-2020.
        """
        self.state = 'validated'
        return True

    @api.model_create_multi
    def create(self, vals_list):
        """
        Use : Inherit Create method to Create Unique sequence for import payout.
        Added by : Deval Jagad
        Added on : 05/06/2020
        Task ID : 164126
        :param vals: dictionary
        :return: result
        """
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('shopify.payout.report.ept') or _('New')
        result = super(ShopifyPaymentReportEpt, self).create(vals_list)
        return result

    def unlink(self):
        """
        Use : Inherit method for Raiser warning if it is in Processed or closed state.
        Added by : Deval Jagad
        Added on : 05/06/2020
        Task ID : 164126
        :return: Raise warning of call super method.
        """
        for report in self:
            if report.state != 'draft':
                raise UserError(_('You cannot delete Payout Report, Which is not in Draft state.'))
        return super(ShopifyPaymentReportEpt, self).unlink()

    def auto_import_payout_report(self, ctx=False):
        """
        Added by : Deval Jagad
        Added on : 05/06/2020
        Task ID : 164126
        Func: this method use get payout report from the last import payout date and current date
        :param ctx: use for the instance
        :return: True
        """
        product_obj = self.env['product.product']
        ac_module = product_obj.search_installed_module_ept('account_accountant')
        shopify_instance_obj = self.env['shopify.instance.ept']
        if isinstance(ctx, dict) and ac_module:
            shopify_instance_id = ctx.get('shopify_instance_id', False)
            if shopify_instance_id:
                instance = shopify_instance_obj.search([('id', '=', shopify_instance_id)])
                payout_import_date = instance.payout_last_import_date
                if not instance.payout_last_import_date:
                    payout_import_date = datetime.now() - timedelta(days=30)
                if payout_import_date:
                    _logger.info("===== Auto Import Payout Report =====")
                    self.get_payout_report(payout_import_date, datetime.now(), instance)
        return True

    def auto_process_bank_statement(self, ctx=False):
        """
        Added by : Deval Jagad
        Added on : 05/06/2020
        Task ID : 164126
        Func: this method use for search  generated report and then process bank statement
        :param ctx: use for the instance
        :return: True
        """
        if isinstance(ctx, dict):
            shopify_instance_id = ctx.get("shopify_instance_id", False)
            if shopify_instance_id:
                generated_reports = self.search([("state", "in", ["generated", "partially_processed"]),
                                                 ("instance_id", "=", shopify_instance_id),
                                                 ("is_skip_from_cron", "=", False)], order="payout_date asc")
                for generated_report in generated_reports:
                    _logger.info("===== Auto Process Bank Statement:%s =====", generated_report.name)
                    try:
                        with self.env.cr.savepoint():
                            generated_report.with_context(cron_process=True).process_bank_statement()
                    except UserError as error:
                        generated_report.message_post(body=str(error))
                        generated_report.is_skip_from_cron = True
                    self._cr.commit()
        return True

    def open_log_book(self):
        """
        Returns action for opening the log book record.
        @author: Maulik Barad on Date 03-Dec-2020.
        @return: Action to open Log Book record.
        """
        return {
            "name": "Logs",
            "type": "ir.actions.act_window",
            "res_model": "common.log.book.ept",
            "views": [(False, "form")],
            'context': self.env.context
        }

    def set_payout_log_line(self, log_lines):
        """
        This method is used to create new log book, add log lines in it and attach to the Payout Report.
        @param log_lines: Recordset of the Log Lines.
        @author: Maulik Barad on Date 09-Dec-2020.
        """
        for log_line in log_lines:
            self.env["common.log.lines.ept"].create_common_log_line_ept(shopify_instance_id=self.instance_id.id,
                                                                        module="shopify_ept",
                                                                        message=log_line.get('message'),
                                                                        model_name=self._name)

        return True
