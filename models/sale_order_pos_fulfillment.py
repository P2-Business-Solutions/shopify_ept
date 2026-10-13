"""Record completed Shopify POS sales as deliveries before invoicing them."""

from odoo import models, _
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare, float_is_zero


class SaleOrderPosFulfillment(models.Model):
    _inherit = 'sale.order'

    def process_shopify_pos_fulfillments(self, payload, queue_line=False):
        """Resume imports atomically, using fulfillment IDs to prevent double stock moves."""
        self.ensure_one()
        if self.state == 'cancel':
            raise UserError(_('A cancelled sales order cannot receive POS fulfillments.'))
        with self.env.cr.savepoint():
            # Serialize an import and a webhook for the same order.
            self.env.cr.execute('SELECT id FROM sale_order WHERE id = %s FOR UPDATE', [self.id])
            self.invalidate_recordset()
            instance = self.shopify_instance_id
            self.apply_shopify_location_and_warehouse(payload, instance, pos_order=True)
            gateway = self.shopify_payment_gateway_id.code or 'no_payment_gateway'
            _gateway, workflow, _term = self.env[
                'shopify.payment.gateway.ept'].shopify_search_create_gateway_workflow(
                    instance, queue_line, payload, gateway)
            if not workflow:
                raise UserError(_('Configure an invoice workflow for this POS order and retry its import.'))
            self.auto_workflow_process_id = workflow
            self.shopify_order_status = payload.get('fulfillment_status')
            if self.state in ('draft', 'sent'):
                # POS fulfillments supply the completed stock movement. Creating
                # procurements here would also reserve the same units for shipping.
                self.with_context(skip_procurement=True).validate_order_ept()
            if not self.procurement_group_id:
                self.procurement_group_id = self.env['procurement.group'].create({
                    'name': self.name, 'sale_id': self.id,
                    'partner_id': self.partner_shipping_id.id,
                    'move_type': self.picking_policy,
                })
            fulfillments = [row for row in payload.get('fulfillments', [])
                            if row.get('status') == 'success']
            if not fulfillments and self.order_line.filtered(lambda line: line.product_id.type != 'service'):
                raise UserError(_('No successful POS fulfillment was supplied. Refresh the Shopify order and retry.'))
            for fulfillment in fulfillments:
                self._import_shopify_pos_delivery(fulfillment, payload)
            self.with_context(
                shopify_order_financial_status=payload.get('financial_status'),
                shopify_order_transactions=payload.get('transaction', []),
            ).validate_and_paid_invoices_ept(workflow)
        return True

    def _import_shopify_pos_delivery(self, fulfillment, payload):
        fulfillment_id = str(fulfillment.get('id') or '')
        if not fulfillment_id:
            raise UserError(_('A POS fulfillment is missing its Shopify ID.'))
        picking = self.picking_ids.filtered(
            lambda row: row.shopify_fulfillment_id == fulfillment_id and row.state != 'cancel')
        if picking.filtered(lambda row: row.state == 'done'):
            return
        location_id = fulfillment.get('location_id') or payload.get('location_id')
        location = self.env['shopify.location.ept'].search([
            ('instance_id', '=', self.shopify_instance_id.id),
            ('shopify_location_id', '=', str(location_id)),
        ], limit=1)
        warehouse = location.warehouse_for_order
        if not warehouse or warehouse.company_id != self.company_id:
            raise UserError(_(
                'Map Shopify POS fulfillment location %s to a warehouse in this company and retry.', location_id))
        if warehouse.shopify_order_hold_reason_id and not self.is_on_hold:
            raise UserError(_('Warehouse %s places imported deliveries on hold. Review the POS fulfillment before retrying.', warehouse.name))
        customer_location = self.partner_shipping_id.property_stock_customer
        specs = []
        for item in fulfillment.get('line_items', []):
            line = self.order_line.filtered(lambda row: row.shopify_line_id == str(item.get('id')))
            if len(line) != 1:
                raise UserError(_('POS fulfillment line %s does not match one sales order line.', item.get('id')))
            quantity = float(item.get('quantity') or 0)
            if line.product_id.type == 'service':
                continue
            if quantity <= 0:
                raise UserError(_('POS fulfillment line %s has no positive delivered quantity.', item.get('id')))
            bom_lines = self.check_for_bom_product(line.product_id)
            if bom_lines:
                specs.extend((line, bom[0].product_id, bom[0].product_uom_id,
                              bom[1]['qty'] * quantity, bom[1]['qty'] * line.product_uom_qty, bom[0])
                             for bom in bom_lines if bom[0].product_id.type != 'service' and
                             not float_is_zero(bom[1]['qty'], precision_rounding=bom[0].product_uom_id.rounding))
            else:
                specs.append((line, line.product_id, line.product_uom,
                              quantity, line.product_uom_qty, False))
        if not specs:
            return
        if not picking:
            picking = self.env['stock.picking'].create({
                'partner_id': self.partner_shipping_id.id,
                'origin': self.name, 'company_id': self.company_id.id,
                'group_id': self.procurement_group_id.id,
                'picking_type_id': warehouse.out_type_id.id,
                'location_id': warehouse.lot_stock_id.id,
                'location_dest_id': customer_location.id,
                'shopify_instance_id': self.shopify_instance_id.id,
                'is_shopify_delivery_order': True,
                'shopify_fulfillment_id': fulfillment_id,
            })
            for line, product, uom, quantity, ordered, bom_line in specs:
                self._shopify_pos_delivery_moves(
                    picking, line, product, uom, quantity, ordered, bom_line)
        if self.is_on_hold:
            self._apply_delivery_hold_to_pickings()
        picking.action_confirm()
        if picking.state != 'done':
            picking.action_assign()
            for move in picking.move_ids.filtered(lambda row: row.state not in ('done', 'cancel')):
                if move.product_id.tracking != 'none' and move.state != 'assigned':
                    raise UserError(_('Reserve the required lots or serial numbers for POS delivery %s and retry.', picking.name))
                move._set_quantity_done(move.product_uom_qty)
                move.picked = True
            picking.with_context(skip_sms=True, skip_backorder=True).button_validate()
            if picking.state != 'done':
                raise UserError(_('POS delivery %s could not be completed. Review its lots or serial numbers and retry.', picking.name))
        vals = {'updated_in_shopify': True}
        carrier = self.env['delivery.carrier'].search_carrier_for_webhook_fulfillment(
            self.shopify_instance_id, fulfillment)
        if carrier:
            vals['carrier_id'] = carrier.id
        if fulfillment.get('tracking_number'):
            vals['carrier_tracking_ref'] = fulfillment['tracking_number']
        picking.write(vals)

    def _shopify_pos_delivery_moves(self, picking, line, product, uom, quantity, ordered, bom_line):
        """Reuse legacy moves and pending deliveries rather than consuming stock twice."""
        moves = line.move_ids.filtered(lambda row: row.product_id == product and
                                        row.location_dest_id.usage == 'customer' and row.state != 'cancel' and
                                        (not bom_line or row.bom_line_id == bom_line))
        completed = moves.filtered(lambda row: row.state == 'done' and row.picking_id)
        delivered = sum(move.product_uom._compute_quantity(move.quantity, uom) for move in completed)
        if float_compare(delivered + quantity, ordered, precision_rounding=uom.rounding) > 0:
            raise UserError(_('POS fulfillment exceeds the undelivered quantity for %s. Review existing deliveries.', line.name))
        remaining = quantity
        for move in moves.filtered(lambda row: not row.picking_id or row.state != 'done').sorted('id'):
            if move.location_id != picking.location_id:
                raise UserError(_('An existing stock move for %s uses another location. Review it before retrying.', line.name))
            amount = move.product_uom._compute_quantity(
                move.quantity if move.state == 'done' else move.product_uom_qty, uom)
            if float_is_zero(amount, precision_rounding=uom.rounding):
                continue
            if move.state == 'done':
                if float_compare(amount, remaining, precision_rounding=uom.rounding) > 0:
                    raise UserError(_('A legacy completed move for %s spans multiple fulfillments. Review it before retrying.', line.name))
            else:
                if move.move_orig_ids or move.move_dest_ids:
                    raise UserError(_('Complete or review the existing warehouse transfers for %s before retrying.', line.name))
                move._do_unreserve()
                if float_compare(amount, remaining, precision_rounding=uom.rounding) > 0:
                    split_qty = uom._compute_quantity(remaining, product.uom_id)
                    pending_move = move
                    move = self.env['stock.move'].create(pending_move._split(split_qty))
                    pending_move._action_assign()
                    amount = remaining
            move.write({'picking_id': picking.id, 'group_id': self.procurement_group_id.id,
                        'picking_type_id': picking.picking_type_id.id})
            remaining -= amount
            if float_is_zero(remaining, precision_rounding=uom.rounding):
                return
        vals = {
            'name': line.name, 'origin': self.name, 'company_id': self.company_id.id,
            'product_id': product.id, 'product_uom': uom.id, 'product_uom_qty': remaining,
            'sale_line_id': line.id, 'group_id': self.procurement_group_id.id,
            'picking_id': picking.id, 'picking_type_id': picking.picking_type_id.id,
            'location_id': picking.location_id.id, 'location_dest_id': picking.location_dest_id.id,
        }
        if bom_line:
            vals['bom_line_id'] = bom_line.id
        self.env['stock.move'].create(vals)
