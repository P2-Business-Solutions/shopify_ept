"""Source-only evidence for historical refunds and final net invoices."""
from datetime import datetime, date
from decimal import Decimal
from zoneinfo import ZoneInfo

from .shopify_payment_plan import PaymentPlanError, component_money, money


def source_date(timestamp, timezone):
    try:
        value = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError()
        return value.astimezone(ZoneInfo(timezone)).date()
    except (AttributeError, ValueError, TypeError, KeyError):
        raise PaymentPlanError('A source event needs a timestamp and a valid business time zone.') from None


def prove_historical_fulfillment(payload, cutover, timezone):
    """Require completed shipment evidence, rather than the order creation date."""
    cutover = date.fromisoformat(str(cutover))
    fulfilled = {}
    for fulfillment in payload.get('fulfillments', []):
        if fulfillment.get('status') != 'success':
            continue
        if source_date(fulfillment.get('created_at'), timezone) >= cutover:
            raise PaymentPlanError('The order has a successful fulfillment on or after cutover.')
        for line in fulfillment.get('line_items', []):
            key = str(line.get('id'))
            fulfilled[key] = fulfilled.get(key, Decimal(0)) + money(line.get('quantity'))
    required = [line for line in payload.get('line_items', []) if line.get('requires_shipping', True)]
    if not required or any(fulfilled.get(str(line.get('id')), 0) < money(line.get('quantity')) for line in required):
        raise PaymentPlanError('Complete successful fulfillment before cutover cannot be proven.')


def refund_components(refund, events, currency, shop_currency, compare):
    """Shopify discrepancy adjustments are subtracted from calculated refunds."""
    if refund.get('duties') or refund.get('refund_duties') or refund.get('additional_fees'):
        raise PaymentPlanError('Duties or additional fees require a reviewed refund credit.')
    event_ids = {str(row.get('id')) for row in refund.get('transactions', [])}
    cash = [event for event in events if event['kind'] == 'refund' and event['id'] in event_ids]
    if not cash or not refund.get('id'):
        raise PaymentPlanError('A refund document must identify its successful cash transactions.')
    parts = []
    for item in refund.get('refund_line_items', []):
        qty = money(item.get('quantity'))
        subtotal = component_money(item, 'subtotal', currency, shop_currency)
        tax = component_money(item, 'total_tax', currency, shop_currency)
        if qty <= 0 or subtotal < 0 or tax < 0 or not item.get('line_item_id'):
            raise PaymentPlanError('Refund item quantities and amounts are incomplete or invalid.')
        parts.append(dict(kind='item', line_id=str(item['line_item_id']), quantity=str(qty),
                          subtotal=str(subtotal), tax=str(tax)))
    for shipping in refund.get('refund_shipping_lines', []):
        subtotal = component_money(shipping, 'subtotal_amount', currency, shop_currency)
        if 'tax_amount' in shipping or shipping.get('tax_amount_set'):
            tax = component_money(shipping, 'tax_amount', currency, shop_currency)
        elif shipping.get('shipping_line', {}).get('tax_lines') == []:
            tax = Decimal(0)
        else:
            raise PaymentPlanError('Shipping refund tax is not explicitly available.')
        if subtotal < 0 or tax < 0:
            raise PaymentPlanError('Shipping refund amounts are invalid.')
        parts.append(dict(kind='shipping', quantity='1', subtotal=str(subtotal), tax=str(tax)))
    for adjustment in refund.get('order_adjustments', []):
        kind = adjustment.get('kind')
        if kind not in ('shipping_refund', 'refund_discrepancy'):
            raise PaymentPlanError('The refund adjustment needs an explicit accounting classification.')
        # A shipping refund is already accounted for by modern shipping rows.
        if kind == 'shipping_refund' and refund.get('refund_shipping_lines'):
            raise PaymentPlanError('The refund contains overlapping shipping representations.')
        subtotal = -component_money(adjustment, 'amount', currency, shop_currency)
        tax = -component_money(adjustment, 'tax_amount', currency, shop_currency)
        parts.append(dict(kind='shipping' if kind == 'shipping_refund' else 'adjustment',
                          quantity='1', subtotal=str(subtotal), tax=str(tax),
                          reason=adjustment.get('reason') or kind))
    total = sum((money(part['subtotal']) + money(part['tax']) for part in parts), Decimal(0))
    cash_total = sum((money(event['amount']) for event in cash), Decimal(0))
    if not parts or compare(float(total), float(cash_total)):
        raise PaymentPlanError('Refund items, shipping, adjustments and tax do not explain the cash refund.')
    return parts, cash


def prove_final_net_invoice(payload, events, invoiced_quantities, invoice_total, compare):
    """Allow amount-only/order-edit refunds only with matching final source documents."""
    currency = events[0]['currency']
    shop_currency = payload.get('currency', currency)
    current_total = component_money(payload, 'current_total_price', currency, shop_currency)
    net_cash = sum((money(event['amount']) * (-1 if event['kind'] == 'refund' else 1)
                    for event in events), Decimal(0))
    if compare(float(current_total), invoice_total) or compare(float(current_total), float(net_cash)):
        raise PaymentPlanError('The final Shopify order total, invoice and net cash do not agree.')
    if payload.get('current_total_duties_set') or payload.get('current_total_additional_fees_set'):
        raise PaymentPlanError('Final-order duties or additional fees require accounting review.')
    source_lines = {str(line['id']): line for line in payload.get('line_items', [])}
    if not source_lines:
        raise PaymentPlanError('Final Shopify line quantities are missing.')
    for key in source_lines.keys() | invoiced_quantities.keys():
        source = source_lines.get(key)
        if source is None or source.get('current_quantity') is None:
            raise PaymentPlanError('Invoice items cannot be traced to final Shopify line quantities.')
        if money(invoiced_quantities.get(key, 0)) != money(source['current_quantity']):
            raise PaymentPlanError('Invoice quantities differ from the final Shopify order.')
    refunds = {event['id'] for event in events if event['kind'] == 'refund'}
    covered = set()
    for refund in payload.get('refunds', []):
        ids = {str(row.get('id')) for row in refund.get('transactions', [])} & refunds
        if not ids:
            continue
        if ids & covered:
            raise PaymentPlanError('A cash refund occurs in multiple refund documents.')
        refund_components(refund, events, currency, shop_currency, compare)
        covered |= ids
    if covered != refunds:
        raise PaymentPlanError('Some successful refunds lack a unique source refund document.')
