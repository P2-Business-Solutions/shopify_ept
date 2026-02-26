# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
import logging
from odoo import models, fields

_logger = logging.getLogger("Shopify Order")

class StockMove(models.Model):
    """Inherit model to set the instance and is shopify delivery order flag"""
    _inherit = "stock.move"

    shopify_fulfillment_order_id = fields.Char("Fulfillment Order ID")
    shopify_fulfillment_line_id = fields.Char("Fulfillment Line ID")
    shopify_fulfillment_order_status = fields.Char("Fulfillment Order Status")
    carrier_id = fields.Many2one('delivery.carrier', string="Shipping Carrier",
                                 help="Carrier extracted from Shopify Fulfillment")
    tracking_reference = fields.Char(string="Tracking Reference", help="Tracking number from Shopify fulfillment")
    shopify_is_free_product = fields.Boolean("Free Product (Discount)", default=False,
                                             help="True if linked sale order line was 100%% discounted")

    def _get_new_picking_values(self):
        """We need this method to set Shopify Instance in Stock Pickin"""
        res = super(StockMove, self)._get_new_picking_values()
        order_id = self.sale_line_id.order_id
        if order_id.shopify_order_id:
            res.update({'shopify_instance_id': order_id.shopify_instance_id.id, 'is_shopify_delivery_order': True})
        return res

    def _action_assign(self, force_qty=False):
        # We inherited the base method here to set the instance values in picking while the picking type is dropship.
        res = super(StockMove, self)._action_assign(force_qty=force_qty)

        for picking in self.picking_id:
            if not picking.shopify_instance_id and picking.sale_id and picking.sale_id.shopify_instance_id:
                picking.write(
                    {'shopify_instance_id': picking.sale_id.shopify_instance_id.id, 'is_shopify_delivery_order': True})
        return res

    def _generate_valuation_lines_data(self, partner_id, qty, debit_value, credit_value,
                                        debit_account_id, credit_account_id, svl_id, description):
        """Override COGS account and analytic for free products from Shopify orders.
        When a product is 100% discounted (free), redirect the COGS debit entry to the
        configured expense account with a discount-code-specific analytic account."""
        rslt = super()._generate_valuation_lines_data(
            partner_id, qty, debit_value, credit_value,
            debit_account_id, credit_account_id, svl_id, description)

        if self.shopify_is_free_product and self.sale_line_id:
            order = self.sale_line_id.order_id
            instance = order.shopify_instance_id
            if instance and instance.free_product_cogs_account_id:
                if rslt.get('debit_line_vals'):
                    rslt['debit_line_vals']['account_id'] = instance.free_product_cogs_account_id.id
                discount_code = self.sale_line_id.shopify_discount_code
                if discount_code:
                    config = order._get_discount_code_config(instance, discount_code)
                    if config and config.analytic_account_id:
                        if rslt.get('debit_line_vals'):
                            rslt['debit_line_vals']['analytic_distribution'] = {
                                str(config.analytic_account_id.id): 100}

        return rslt

    def auto_process_stock_move_ept(self):
        """
        This method is use to check if stock move contain the lot/serial product but stock is not available then cron check
        if stock is received then it assigned and done the stock move.
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 10 October 2023 .
        """
        move_ids = self.prepre_query_to_get_stock_move_ept()
        moves = self.browse(move_ids)
        for move in moves:
            try:
                with self.env.cr.savepoint():
                    move.picked = False
                    move.move_line_ids.unlink()
                    # move._action_confirm()
                    move._action_assign()
                    move.picked = True
                    # move._set_quantity_done(move.product_uom_qty)
                    move._action_done()
            except Exception as error:
                message = "Receive error while assign stock to stock move(%s) of shipped order, Error is:  (%s)" % (move,error)
                _logger.info(message)
                continue
        return True

    def prepre_query_to_get_stock_move_ept(self):
        """
        This method is use to prepare a query to get stock move
        @author: Haresh Mori @Emipro Technologies Pvt. Ltd on date 10 October 2023 .
        """
        sm_query = """
                    SELECT
                        sm.id as move_id,
                        so.id as so_id
                    FROM 
                        stock_move  as sm
                    INNER JOIN
                        sale_order_line as sol on sol.id = sm.sale_line_id 
                    INNER JOIN
                        sale_order as so on so.id = sol.order_id
                    INNER JOIN
                        product_product as pp on pp.id = sm.product_id
                    INNER JOIN
                        product_template as pt on pt.id = pp.product_tmpl_id
                    WHERE
                        picking_id is null AND
                        sale_line_id is not null AND
                        so.shopify_order_id is not null AND
                        sm.state in ('confirmed','partially_available','assigned')
                    limit 100
                   """
        self._cr.execute(sm_query)
        result = self._cr.dictfetchall()
        move_ids = [data.get('move_id') for data in result]
        return move_ids