"""Read-only evidence for a blocked repair; never an accounting approval."""
from decimal import Decimal
from datetime import date

from .shopify_payment_plan import cash_events, money
from .shopify_refund_plan import prove_historical_fulfillment, source_date


def source_review(payload, transactions, order_id, currency, cutover, timezone):
    cutover = date.fromisoformat(str(cutover))
    result = dict(order_id=str(order_id), currency=currency, cutover=str(cutover), timezone=timezone,
                  events=[], fulfillment=[], items=[])
    if str(payload.get('id')) != str(order_id):
        result['source_issue'] = 'The source identifies another order; no totals can be relied on.'
        return result
    result.update(financial_status=payload.get('financial_status') or '',
                  fulfillment_status=payload.get('fulfillment_status') or '',
                  cancelled_at=payload.get('cancelled_at') or '')
    try:
        events = cash_events(transactions, order_id, currency)
        gross = sum((money(row['amount']) for row in events if row['kind'] != 'refund'), Decimal(0))
        refunds = sum((money(row['amount']) for row in events if row['kind'] == 'refund'), Decimal(0))
        result.update(events=events, gross=str(gross), refunds=str(refunds), net=str(gross - refunds))
    except ValueError as error:
        result['source_issue'] = str(error)
    fulfilled = {}
    for fulfillment in payload.get('fulfillments', []):
        row = dict(id=str(fulfillment.get('id') or ''), status=fulfillment.get('status') or '',
                   timestamp=fulfillment.get('created_at') or '', before_cutover=False)
        try:
            row['before_cutover'] = source_date(row['timestamp'], timezone) < cutover
            if row['status'] == 'success' and row['before_cutover']:
                for item in fulfillment.get('line_items', []):
                    key = str(item.get('id'))
                    fulfilled[key] = fulfilled.get(key, Decimal(0)) + money(item.get('quantity'))
        except ValueError as error:
            row['issue'] = str(error)
        result['fulfillment'].append(row)
    for item in payload.get('line_items', []):
        if not item.get('requires_shipping', True):
            continue
        key = str(item.get('id'))
        try:
            required = money(item.get('quantity'))
            shipped = fulfilled.get(key, Decimal(0))
            result['items'].append(dict(id=key, name=item.get('title') or '', sku=item.get('sku') or '',
                                        required=str(required), fulfilled=str(shipped),
                                        missing=str(max(Decimal(0), required - shipped))))
        except ValueError as error:
            result['items'].append(dict(id=key, issue=str(error)))
    try:
        prove_historical_fulfillment(payload, cutover, timezone)
        result['fulfillment_result'] = 'Complete successful fulfillment before cutover is proven.'
    except ValueError as error:
        result['fulfillment_result'] = str(error)
    return result
