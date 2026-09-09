"""Deterministic validation of Shopify cash history, independent of Odoo."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json


class PaymentPlanError(ValueError):
    pass


def money(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentPlanError('Missing or invalid transaction amount.') from None
    if not result.is_finite():
        raise PaymentPlanError('Invalid transaction amount.')
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def component_money(item, field, currency, shop_currency, set_field=None):
    """Choose explicitly denominated refund components for presentment currencies."""
    amounts = item.get(set_field or field + '_set') or {}
    for key in ('shop_money', 'presentment_money'):
        amount = amounts.get(key) or {}
        if amount.get('currency_code') == currency:
            return money(amount.get('amount'))
    if amounts or currency != shop_currency:
        raise PaymentPlanError('Refund component currency cannot be matched to the cash transaction.')
    return money(item.get(field))


def cash_events(transactions, order_id, currency):
    """Only successful cash movements; preserve identity rather than netting them."""
    rows = {}
    for raw in transactions:
        if raw.get('status') != 'success' or raw.get('kind') in ('authorization', 'void'):
            continue
        if raw.get('kind') not in ('sale', 'capture', 'refund'):
            raise PaymentPlanError('Unsupported successful Shopify transaction kind.')
        key = str(raw.get('id') or '')
        if not key or str(raw.get('order_id')) != str(order_id):
            raise PaymentPlanError('A transaction is missing its ID or belongs to another order.')
        if raw.get('currency') != currency:
            raise PaymentPlanError('Transaction currency differs from the invoice currency; review the conversion.')
        amount = money(raw.get('amount'))
        if amount <= 0:
            raise PaymentPlanError('Successful cash transactions must have a positive amount.')
        gateway = raw.get('gateway')
        if not gateway or gateway == 'gift_card':
            raise PaymentPlanError('Gift cards or unidentified gateways require a separate accounting review.')
        timestamp = raw.get('processed_at') or raw.get('created_at')
        try:
            parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError()
        except (AttributeError, ValueError, TypeError):
            raise PaymentPlanError('A cash transaction is missing its dated timezone information.') from None
        row = dict(id=key, parent_id=str(raw.get('parent_id') or ''), kind=raw['kind'],
                   gateway=gateway, currency=currency, amount=str(amount), timestamp=timestamp,
                   date=parsed.date().isoformat())
        if key in rows and rows[key] != row:
            raise PaymentPlanError('Conflicting copies of a Shopify transaction were returned.')
        rows[key] = row
    events = sorted(rows.values(), key=lambda row: (row['kind'] == 'refund', row['timestamp'], row['id']))
    charges = {row['id']: row for row in events if row['kind'] != 'refund'}
    refunded = {}
    for row in events:
        if row['kind'] != 'refund':
            continue
        parent = charges.get(row['parent_id'])
        if not parent or parent['gateway'] != row['gateway']:
            raise PaymentPlanError('A refund cannot be linked to its original successful charge and gateway.')
        refunded[parent['id']] = refunded.get(parent['id'], Decimal(0)) + money(row['amount'])
        if refunded[parent['id']] > money(parent['amount']):
            raise PaymentPlanError('Refunds exceed their original charge.')
    if not charges:
        raise PaymentPlanError('No successful Shopify charge was found; complete transaction history is required.')
    return events


def invoice_mode(events, invoice_total, credits, compare, embedded_refunds=0):
    gross = sum(money(row['amount']) for row in events if row['kind'] != 'refund')
    refunded = sum(money(row['amount']) for row in events if row['kind'] == 'refund')
    embedded = money(embedded_refunds)
    if embedded:
        if (embedded > refunded or compare(invoice_total, float(gross - embedded))
                or compare(credits, float(refunded - embedded)) > 0):
            raise PaymentPlanError('The documents no longer agree with the previously audited net invoice.')
        return 'net_with_credits'
    if compare(invoice_total, float(gross)) == 0:
        if compare(credits, float(refunded)) > 0:
            raise PaymentPlanError('Posted credit notes exceed the successful Shopify refunds.')
        return 'gross'
    if refunded and compare(credits, 0) == 0 and compare(invoice_total, float(gross - refunded)) == 0:
        return 'net'
    raise PaymentPlanError('Invoice and credit-note totals do not represent either the original sale or its final net amount.')


def prove_net_refunds(payload, events, invoiced_quantities):
    """Do not infer an already-net invoice from an amount difference alone."""
    lines = {str(row['id']): row for row in payload.get('line_items', [])}
    refund_ids = {row['id'] for row in events if row['kind'] == 'refund'}
    covered = set()
    quantities = {}
    for refund in payload.get('refunds', []):
        ids = {str(row.get('id')) for row in refund.get('transactions', [])} & refund_ids
        if not ids:
            continue
        if (refund.get('order_adjustments') or refund.get('refund_shipping_lines')
                or refund.get('duties') or refund.get('additional_fees')):
            raise PaymentPlanError('Net-invoice shipping, duty or adjustment refunds require accounting review.')
        components = Decimal(0)
        for item in refund.get('refund_line_items', []):
            key = str(item.get('line_item_id'))
            source = lines.get(key)
            if not source or source.get('current_quantity') is None:
                raise PaymentPlanError('Refunded items cannot be traced to the final invoice quantities.')
            quantity = money(item.get('quantity'))
            quantities[key] = quantities.get(key, Decimal(0)) + quantity
            if quantity <= 0 or quantities[key] > money(source['quantity']) - money(source['current_quantity']):
                raise PaymentPlanError('Refunded quantities are not fully reflected in the final Shopify order.')
            if money(invoiced_quantities.get(key, 0)) != money(source['current_quantity']):
                raise PaymentPlanError('The invoice still includes refunded quantities; do not skip a credit note.')
            currency = events[0]['currency']
            shop_currency = payload.get('currency', currency)
            components += component_money(item, 'subtotal', currency, shop_currency)
            components += component_money(item, 'total_tax', currency, shop_currency)
        cash = sum(money(row['amount']) for row in events if row['id'] in ids)
        if not components or components != cash or covered & ids:
            raise PaymentPlanError('Refund components do not uniquely explain the cash returned.')
        covered |= ids
    if covered != refund_ids:
        raise PaymentPlanError('Some cash refunds lack item-level evidence that the invoice already excludes them.')
