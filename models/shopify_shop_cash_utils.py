"""Shop Cash payout classification and order-level evidence, without Odoo."""
from decimal import Decimal, InvalidOperation


def shop_cash_kind(transaction_type, reason):
    kind = (transaction_type or '').lower()
    if kind in ('shop_cash_credit', 'shop_cash_refund_debit',
                'shop_cash_credit_reversal', 'shop_cash_refund_debit_reversal'):
        return kind
    if reason == 'shop_cash':
        return 'shop_cash_credit'
    if reason == 'shop_cash_refund':
        return 'shop_cash_refund_debit'
    return False


def decimal_amount(value):
    if isinstance(value, dict):
        value = value.get('amount')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError('Shop Cash has an invalid monetary amount.') from error
    if not result.is_finite():
        raise ValueError('Shop Cash has a nonfinite monetary amount.')
    return result


def shop_cash_allocations(data):
    """Keep explicit transaction IDs distinct from REST adjustment-row IDs."""
    rows = data.get('adjustment_order_transactions') or []
    if not isinstance(rows, list):
        raise ValueError('Shop Cash order breakdown must be a list.')
    if not rows and (data.get('source_order_id') or data.get('source_order_transaction_id')):
        rows = [{'order': {'id': data.get('source_order_id')},
                 'order_transaction_id': data.get('source_order_transaction_id'),
                 'amount': data.get('amount'), 'fee': data.get('fee'), 'net': data.get('net')}]
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Shop Cash has an invalid order breakdown.')
        order_id = str((row.get('order') or {}).get('id') or row.get('order_id') or '')
        transaction_id = str(row.get('order_transaction_id') or row.get('source_order_transaction_id') or '')
        # `id` in REST's adjustment_order_transactions is not documented as an
        # order transaction ID. Never turn it into a payment identity.
        if not order_id and not transaction_id:
            raise ValueError('Shop Cash order breakdown lacks an order or payment transaction ID.')
        identity = ('transaction', transaction_id) if transaction_id else ('order', order_id)
        if identity in seen:
            raise ValueError('Shop Cash order breakdown contains duplicate payment allocations.')
        seen.add(identity)
        amount = abs(decimal_amount(row.get('amount')))
        if not amount:
            raise ValueError('Shop Cash order breakdown contains a zero payment allocation.')
        item = {'order_id': order_id, 'transaction_id': transaction_id, 'amount': str(amount)}
        for source, target in (('fee', 'fee'), ('fees', 'fee'), ('net', 'net')):
            if source in row and row[source] is not None:
                item[target] = str(decimal_amount(row[source]))
        result.append(item)
    return result


SHOP_CASH_DETAILS_QUERY = """
query ShopCashSettlement($id: ID!) {
  node(id: $id) {
    ... on ShopifyPaymentsBalanceTransaction {
      id
      associatedPayout { id }
      amount { amount currencyCode }
      fee { amount currencyCode }
      net { amount currencyCode }
      adjustmentsOrders {
        orderTransactionId
        amount { amount currencyCode }
        fees { amount currencyCode }
        net { amount currencyCode }
      }
    }
  }
}
"""
