# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class AccountBankStatementLine(models.Model):
    """
    Inherited for adding transaction line id for Shopify Payout Report.
    @author: Maulik Barad on Date 02-Dec-2020.
    """
    _inherit = "account.bank.statement.line"

    shopify_transaction_id = fields.Char("Shopify Transaction")
    shopify_order_transaction_id = fields.Char(
        "Shopify Order Transaction", copy=False, index=True
    )
    shopify_transaction_type = fields.Selection([('charge', 'Charge'), ('refund', 'Refund'), ('dispute', 'Dispute'),
                                                 ('reserve', 'Reserve'), ('adjustment', 'Adjustment'),
                                                 ('credit', 'Credit'),
                                                 ('debit', 'Debit'),
                                                 ('tax_adjustment', 'Marketplace Sales Tax'),
                                                 ('payout', 'Payout'),
                                                 ('payout_failure', 'Payout Failure'),
                                                 ('payout_cancellation', 'Payout Cancellation'), ('fees', 'Fees'),
                                                 ('payment_refund', 'Payment Refund'),
                                                 ('shopify_collective_debit_reversal', 'Shopify Collective Debit Reversal'),
                                                 ('seller_protection_credit_reversal','Seller Protection Credit Reversal'),
                                                 ('refund_failure', 'Refund Failure')
                                                 ],
                                                help="The type of the balance transaction",
                                                string="Balance Transaction Type")
    payout_id = fields.Many2one('shopify.payout.report.ept', string="Payout ID", ondelete="cascade", index=True)
    payout_line_id = fields.Many2one('shopify.payout.report.line.ept', string="Payout line ID", ondelete="cascade", index=True)

    def write(self, vals):
        res = super(AccountBankStatementLine, self).write(vals)
        if 'checked' in vals and not vals['checked']:
            self.payout_id.filtered(lambda payout: payout.state == 'validated').write({
                'state': 'partially_processed',
            })
        return res
