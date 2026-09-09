# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
from odoo import models, fields, _
from odoo.exceptions import UserError


class ShopifyPayoutReportLineEpt(models.Model):
    _name = "shopify.payout.report.line.ept"
    _description = "Shopify Payout Report Line"
    _rec_name = "transaction_id"

    payout_id = fields.Many2one('shopify.payout.report.ept', string="Payout ID", ondelete="cascade", index=True)
    transaction_id = fields.Char(string="Transaction ID", help="The unique identifier of the transaction.", index=True)
    source_id = fields.Char(
        string="Source ID",
        help="The Shopify Payments resource that originated this balance transaction.",
    )
    source_order_id = fields.Char(string="Order Reference ID", help="The id of the Order that this transaction  "
                                                                    "ultimately originated from")
    source_order_transaction_id = fields.Char(
        string="Order Transaction ID",
        help="The Shopify order transaction that originated this balance transaction.",
        index=True,
    )
    transaction_type = fields.Selection(
        [('charge', 'Charge'), ('refund', 'Refund'), ('dispute', 'Dispute'),
         ('reserve', 'Reserve'), ('adjustment', 'Adjustment'), ('credit', 'Credit'),
         ('debit', 'Debit'), ('tax_adjustment', 'Marketplace Sales Tax'),
         ('payout', 'Payout'), ('payout_failure', 'Payout Failure'),
         ('payout_cancellation', 'Payout Cancellation'), ('fees', 'Fees'), ('payment_refund', 'Payment Refund'),
         ('shopify_collective_debit_reversal', 'Shopify Collective Debit Reversal'),
         ('seller_protection_credit_reversal','Seller Protection Credit Reversal'),
         ('refund_failure', 'Refund Failure')],
        help="The type of the balance transaction", string="Balance Transaction Type")
    raw_transaction_type = fields.Char(
        string="Shopify Balance Transaction Type",
        help="The original balance transaction type returned by Shopify before accounting classification.")
    adjustment_reason = fields.Char(
        string="Adjustment Reason",
        help="The adjustment reason returned by Shopify, when applicable.")
    currency_id = fields.Many2one('res.currency', string='Currency', help="currency code of the payout.")
    source_type = fields.Char(
        string="Resource Leading Transaction",
        help="The Shopify Payments resource class that originated the balance transaction.",
    )
    amount = fields.Float(string="Amount", help="The gross amount of the transaction.")
    fee = fields.Float(string="Fees", help="The total amount of fees deducted from the transaction amount.")
    net_amount = fields.Float(string="Net Amount", help="The net amount of the transaction.")
    order_id = fields.Many2one('sale.order', string="Order Reference")
    is_processed = fields.Boolean("Processed?")
    is_remaining_statement = fields.Boolean(string="Is Remaining Statement?")

    def write(self, vals):
        protected = {'payout_id', 'transaction_id', 'transaction_type', 'amount', 'fee', 'net_amount', 'currency_id'}
        if protected.intersection(vals):
            linked = self.env['account.bank.statement.line'].search([('payout_line_id', 'in', self.ids)]).payout_line_id
            for line in linked:
                if any((line[name].id if name in ('payout_id', 'currency_id') else line[name]) != vals[name]
                       for name in protected.intersection(vals)):
                    raise UserError(_('A payout transaction already has a statement line. Review that accounting before changing its source amounts or identity.'))
        return super().write(vals)

    def unlink(self):
        if self.env['account.bank.statement.line'].search_count([('payout_line_id', 'in', self.ids)]):
            raise UserError(_('Remove the eligible unreconciled statement lines explicitly before deleting their payout transactions.'))
        return super().unlink()
