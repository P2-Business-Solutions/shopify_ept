# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

from odoo import models, fields, _


class AccountMove(models.Model):
    """
    Inherite the account move here to return refund action.
    """
    _inherit = "account.move"

    is_refund_in_shopify = fields.Boolean("Refund In Shopify", default=False,
                                          help="True: Refunded credit note amount in shopify store.\n False: "
                                               "Remaining to refund in Shopify Store")
    shopify_instance_id = fields.Many2one("shopify.instance.ept", "Instances")
    shopify_refund_id = fields.Char(help="Id of shopify refund.", copy=False)
    is_shopify_multi_payment = fields.Boolean("Multi Payments?", default=False, copy=False,
                                              help="It is used to identify that order has multi-payment gateway or not")

    def action_open_refund_wizard(self):
        """This method used to open a wizard for Refund order in Shopify.
            @param : self
            @return: action
            @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 20/11/2019.
            Task Id : 157911
        """
        form_view = self.env.ref('shopify_ept.view_shopify_refund_wizard')
        context = dict(self._context)
        context.update({'active_model': 'account.invoice', 'active_id': self.id, 'active_ids': self.ids})
        if self.reversed_entry_id.is_shopify_multi_payment:
            payment_gateway_ids = self.reversed_entry_id.invoice_line_ids.sale_line_ids.order_id.shopify_payment_ids
            payment_gateway_ids.write({'refund_amount': 0.0, 'is_want_to_refund': False})
            remaining_to_refund_payment_ids = payment_gateway_ids.filtered(
                lambda payment: payment.is_fully_refunded == False).payment_gateway_id.ids
            context.update({'display_refund_from': False, 'payment_gateway_ids': remaining_to_refund_payment_ids,
                            'default_payment_ids': [(6, 0, payment_gateway_ids.ids)]})
        return {
            'name': _('Refund order In Shopify'),
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'shopify.cancel.refund.order.wizard',
            'views': [(form_view.id, 'form')],
            'view_id': form_view.id,
            'target': 'new',
            'context': context
        }

    def open_shopify_multi_payment(self):
        """This method used to open a wizard to display details of multi payments.
            @author: Meera Sidapara @Emipro Technologies Pvt. Ltd on date 16/11/2021.
            Task Id : 179257
        """
        context = dict(self._context)
        context.update({'active_model': 'account.move', 'active_id': self.id, 'active_ids': self.ids})
        view_id = self.env.ref('shopify_ept.shopify_multi_payment_gateway_tree_view_ept').id
        return {
            'name': _('Multi payments'),
            'type': 'ir.actions.act_window',
            'view_mode': 'list',
            'res_model': 'shopify.order.payment.ept',
            'view_id': view_id,
            'views': [(view_id, 'list')],
            'domain': [('id', 'in', self.line_ids.sale_line_ids.order_id.shopify_payment_ids.ids)],
            "target": "new",
            'context': context
        }

    def _reconcile_reversed_moves(self, reverse_moves, move_reverse_cancel):
        ''' Inherit Method to not reverse the invoice while create refund from the payout process.
        '''
        if self._context.get('is_shopify_reverse_move_ept', False):
            return reverse_moves
        return super(AccountMove, self)._reconcile_reversed_moves(reverse_moves, move_reverse_cancel)

    def _stock_account_prepare_anglo_saxon_out_lines_vals(self):
        """Route COGS to configured Shopify expense account during invoice posting.

        This avoids a separate reclassification entry for discounted/free Shopify lines by
        replacing the generated COGS debit account directly in anglo-saxon lines.
        """
        lines_vals_list = super()._stock_account_prepare_anglo_saxon_out_lines_vals()
        if not lines_vals_list:
            return lines_vals_list

        account_by_product = {}
        account_by_label = {}
        analytic_by_product = {}
        analytic_by_label = {}

        for invoice_line in self.invoice_line_ids:
            if not invoice_line.sale_line_ids:
                continue

            selected_sale_line = False
            selected_discount_code = False
            selected_instance = False

            for sale_line in invoice_line.sale_line_ids:
                order = sale_line.order_id
                instance = order.shopify_instance_id
                if not instance or not instance.free_product_cogs_account_id:
                    continue

                # Primary trigger: discount code on the product sale line itself.
                discount_code = sale_line.shopify_discount_code

                # Fallback trigger: discount code exists on the related discount line
                # (common when the product line itself isn't flagged).
                if not discount_code and sale_line.shopify_line_id:
                    related_discount_line = order.order_line.filtered(
                        lambda line: line.shopify_related_line_id == "discount_%s" % sale_line.shopify_line_id and
                        line.shopify_discount_code
                    )[:1]
                    if related_discount_line:
                        discount_code = related_discount_line.shopify_discount_code

                if discount_code:
                    selected_sale_line = sale_line
                    selected_discount_code = discount_code
                    selected_instance = instance
                    break

            if not selected_sale_line:
                continue

            target_account_id = selected_instance.free_product_cogs_account_id.id
            if invoice_line.product_id:
                account_by_product[invoice_line.product_id.id] = target_account_id
            if invoice_line.name:
                account_by_label[invoice_line.name] = target_account_id

            config = selected_sale_line.order_id._get_discount_code_config(selected_instance, selected_discount_code)
            if config and config.analytic_account_id:
                analytic_dist = {str(config.analytic_account_id.id): 100}
                if invoice_line.product_id:
                    analytic_by_product[invoice_line.product_id.id] = analytic_dist
                if invoice_line.name:
                    analytic_by_label[invoice_line.name] = analytic_dist

        if not account_by_product and not account_by_label:
            return lines_vals_list

        for line_vals in lines_vals_list:
            # Only replace the debit (COGS) side; keep the credit side on the
            # default stock valuation/output account.
            if 'balance' in line_vals:
                is_debit_line = line_vals.get('balance', 0.0) > 0
            elif 'debit' in line_vals or 'credit' in line_vals:
                is_debit_line = line_vals.get('debit', 0.0) > 0 and line_vals.get('credit', 0.0) <= 0
            else:
                # Odoo 18: anglo-saxon line dicts use amount_currency, not balance/debit/credit.
                # Positive amount_currency = COGS expense (debit) side.
                is_debit_line = line_vals.get('amount_currency', 0.0) > 0
            if not is_debit_line:
                continue

            product_id = line_vals.get('product_id')
            label = line_vals.get('name')

            target_account_id = account_by_product.get(product_id) or account_by_label.get(label)
            if not target_account_id:
                continue

            line_vals['account_id'] = target_account_id
            analytic_distribution = analytic_by_product.get(product_id) or analytic_by_label.get(label)
            if analytic_distribution:
                line_vals['analytic_distribution'] = analytic_distribution

        return lines_vals_list
