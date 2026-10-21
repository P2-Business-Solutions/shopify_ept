"""Keep imported source dates on new accounting; stop unsupported backdating."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from odoo import fields, models, _
from odoo.exceptions import UserError


def dated_source(value, timezone_name='UTC'):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not stamp.tzinfo:
            raise ValueError()
        local_date = stamp.astimezone(ZoneInfo(timezone_name or 'UTC')).date()
        return stamp.astimezone(timezone.utc).replace(tzinfo=None), local_date
    except (ValueError, AttributeError, TypeError, KeyError):
        raise UserError('The Shopify source needs a timestamp with timezone and a valid store timezone.') from None


class ShopifySourceDateOrder(models.Model):
    _inherit = 'sale.order'

    def _prepare_invoice(self):
        values = super()._prepare_invoice()
        payload = self.env.context.get('shopify_order_response')
        if self.shopify_instance_id and payload:
            if str(payload.get('id')) != self.shopify_order_id:
                raise UserError(_('The invoice import context belongs to another Shopify order.'))
            zone = self.shopify_instance_id.shopify_store_time_zone
            stamp = payload.get('processed_at') or payload.get('created_at')
            fulfillments = [r for r in payload.get('fulfillments', []) if r.get('status') == 'success']
            if fulfillments and not self.auto_workflow_process_id.invoice_date_is_order_date:
                dated = [dated_source(row.get('created_at'), zone) for row in fulfillments]
                date = max(dated)[1]
            else:
                _stamp, date = dated_source(stamp, zone)
            if date > fields.Date.context_today(self):
                raise UserError(_('The source invoice date cannot be in the future.'))
            values.update(invoice_date=date, date=date)
        return values

    def validate_invoice_ept(self, invoices):
        if not self.shopify_instance_id or not self.env.context.get('shopify_order_response'):
            return super().validate_invoice_ept(invoices)
        with self.env.cr.savepoint():
            for invoice in invoices:
                date = invoice.invoice_date
                if invoice.company_id._get_violated_lock_dates(date, True, invoice.journal_id):
                    raise UserError(_('The source invoice date is locked. Review its accounting period before importing.'))
                invoice.action_post()
                if invoice.date != date:
                    raise UserError(_('Odoo changed the source invoice posting date. Review the accounting period.'))
        return True


class ShopifySourceDateMove(models.Model):
    _inherit = 'stock.move'

    shopify_source_done_at = fields.Datetime(readonly=True, copy=False,
        help='Successful source fulfillment time used when this new imported movement was completed.')
    shopify_source_accounting_date = fields.Date(readonly=True, copy=False)

    def _shopify_import_date(self):
        self.ensure_one()
        payload = self.env.context.get('shopify_order_response')
        order = self.sale_line_id.order_id
        if (not payload or not order.shopify_instance_id or self.location_dest_id.usage != 'customer'
                or self.state in ('done', 'cancel')):
            return False
        if str(payload.get('id')) != order.shopify_order_id:
            raise UserError(_('The stock import context belongs to another Shopify order.'))
        fulfillment_id = self.picking_id.shopify_fulfillment_id
        rows = [r for r in payload.get('fulfillments', []) if r.get('status') == 'success'
                and (str(r.get('id')) == fulfillment_id if fulfillment_id else any(
                    str(i.get('id')) == self.sale_line_id.shopify_line_id for i in r.get('line_items', [])))]
        dates = {dated_source(r.get('created_at'), order.shopify_instance_id.shopify_store_time_zone) for r in rows}
        if len(dates) != 1:
            raise UserError(_('This stock movement cannot be assigned to one dated successful Shopify fulfillment. Review its shipment allocation.'))
        stamp, date = dates.pop()
        if date > fields.Date.context_today(self):
            raise UserError(_('A source fulfillment cannot be posted in the future.'))
        accounts = self.product_id.product_tmpl_id.get_product_accounts()
        journal = accounts.get('stock_journal')
        if self.company_id._get_violated_lock_dates(date, False, journal):
            raise UserError(_('The source fulfillment accounting date is locked. Review it before importing stock.'))
        if (date < fields.Date.context_today(self) and self.product_id.valuation == 'real_time'
                and self.product_id.cost_method in ('fifo', 'average')):
            raise UserError(_('Backdated FIFO/average-cost fulfillment requires inventory valuation review. Import was stopped before completing stock.'))
        return stamp, date

    def _action_done(self, cancel_backorder=False):
        dated = [(move, move._shopify_import_date()) for move in self]
        if not any(date for _move, date in dated):
            return super()._action_done(cancel_backorder=cancel_backorder)
        result = self.env['stock.move']
        with self.env.cr.savepoint():
            for move, dates in dated:
                if dates:
                    stamp, date = dates
                    move.write({'shopify_source_done_at': stamp, 'shopify_source_accounting_date': date})
                    target = move.with_context(force_period_date=date)
                else:
                    target = move
                done = super(ShopifySourceDateMove, target)._action_done(cancel_backorder=cancel_backorder)
                if dates:
                    done.filtered(lambda m: m.state == 'done').write({'date': stamp})
                    done.move_line_ids.write({'date': stamp})
                    valuations = done.stock_valuation_layer_ids.account_move_id
                    if valuations.filtered(lambda entry: entry.date != date):
                        raise UserError(_('The valuation entry did not retain its source fulfillment date.'))
                result |= done
        return result


class ShopifySourceDatePicking(models.Model):
    _inherit = 'stock.picking'

    def _action_done(self):
        result = super()._action_done()
        for picking in self.filtered(lambda p: p.state == 'done'):
            moves = picking.move_ids.filtered(lambda m: m.state == 'done')
            if moves and all(m.shopify_source_done_at for m in moves):
                picking.date_done = max(moves.mapped('shopify_source_done_at'))
        return result
