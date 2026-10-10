"""Reconcile Shop Cash settlements to their actual capture/refund payments."""
import json

from odoo import Command, models, _
from odoo.exceptions import UserError
from psycopg2 import OperationalError

from .. import shopify
from .shopify_shop_cash_utils import (
    SHOP_CASH_DETAILS_QUERY, decimal_amount, shop_cash_kind, shop_cash_allocations,
)


class ShopifyShopCash(models.Model):
    _inherit = 'shopify.payout.report.ept'

    def _enrich_shop_cash_transaction(self, data):
        """REST sometimes omits itemization; fetch that exact balance node."""
        if not shop_cash_kind(data.get('type'), data.get('adjustment_reason')):
            return data
        data = dict(data)
        try:
            allocations = shop_cash_allocations(data)
            if not allocations:
                balance_id = str(data.get('id') or '')
                if not balance_id:
                    raise ValueError('Shop Cash settlement lacks its balance transaction ID.')
                self.instance_id.connect_in_shopify()
                graphql = shopify.GraphQL()
                # This connector authenticates REST through URL credentials;
                # the SDK's GraphQL client does not carry Basic Auth across.
                # Set the token on this client only, avoiding shared headers.
                graphql.headers = dict(graphql.headers, **{
                    'X-Shopify-Access-Token': self.instance_id.shopify_password})
                response = json.loads(graphql.execute(
                    SHOP_CASH_DETAILS_QUERY,
                    variables={'id': 'gid://shopify/ShopifyPaymentsBalanceTransaction/' + balance_id},
                    operation_name='ShopCashSettlement'))
                if response.get('errors'):
                    raise ValueError('Shopify could not return the Shop Cash order breakdown. Check payout API permissions and retry.')
                node = (response.get('data') or {}).get('node') or {}
                if (node.get('id') != 'gid://shopify/ShopifyPaymentsBalanceTransaction/' + balance_id
                        or (node.get('associatedPayout') or {}).get('id') !=
                        'gid://shopify/ShopifyPaymentsPayout/' + self.payout_reference_id):
                    raise ValueError('Shopify returned another Shop Cash balance transaction or payout.')
                for name in ('amount', 'fee', 'net'):
                    value = node.get(name) or {}
                    if (value.get('currencyCode') != data.get('currency')
                            or decimal_amount(value) != decimal_amount(data.get(name))):
                        raise ValueError('Shop Cash payout API amounts or currency disagree with the imported balance transaction.')
                rows = []
                for item in node.get('adjustmentsOrders') or []:
                    if any((item.get(name) or {}).get('currencyCode') != data.get('currency')
                           for name in ('amount', 'fees', 'net')):
                        raise ValueError('Shop Cash order breakdown has another currency.')
                    rows.append({'order_transaction_id': item.get('orderTransactionId'),
                                 'amount': item.get('amount'), 'fees': item.get('fees'), 'net': item.get('net')})
                data['adjustment_order_transactions'] = rows
                allocations = shop_cash_allocations(data)
            data['shop_cash_allocations'] = allocations
            data['shop_cash_detail_error'] = False if allocations else 'Shopify returned no Shop Cash order-level payment details. Reimport after those details are available.'
        except OperationalError:
            raise
        except Exception as error:
            # A missing breakdown must not stop unrelated card transactions
            # importing, nor turn Shop Cash into a generic income adjustment.
            data['shop_cash_allocations'] = []
            data['shop_cash_detail_error'] = str(error)
        return data

    def _shop_cash_order_for_payment(self, payment):
        orders = payment.shopify_cash_order_id
        if not orders:
            orders = payment.reconciled_invoice_ids.invoice_line_ids.sale_line_ids.order_id
        orders = orders.filtered(lambda order: order.shopify_instance_id == self.instance_id
                                 and order.company_id == self.instance_id.shopify_company_id)
        if len(orders) != 1:
            raise UserError(_('A Shop Cash payment must identify one order in this Shopify store and company.'))
        return orders

    def _shop_cash_gateway_matches(self, payment, order):
        if payment.shopify_cash_gateway:
            return payment.shopify_cash_gateway == 'shop_cash'
        rows = order.shopify_payment_ids.filtered(lambda row: row.workflow_id.journal_id == payment.journal_id)
        if payment.shopify_order_transaction_id:
            rows = rows.filtered(lambda row: row.payment_transaction_id == payment.shopify_order_transaction_id)
        else:
            rows = rows.filtered(lambda row: not self.currency_id.compare_amounts(row.amount, payment.amount))
        if rows:
            return len(rows) == 1 and rows.payment_gateway_id.code == 'shop_cash'
        return not order.is_shopify_multi_payment and order.shopify_payment_gateway_id.code == 'shop_cash'

    def _shop_cash_orders_for_preview(self, transaction):
        orders = self.env['sale.order']
        for entry in transaction.shop_cash_allocations or []:
            if entry.get('order_id'):
                order = orders.search([('shopify_order_id', '=', entry['order_id']),
                    ('shopify_instance_id', '=', self.instance_id.id),
                    ('company_id', '=', self.instance_id.shopify_company_id.id)], limit=2)
                if len(order) != 1:
                    raise UserError(_('Import the missing Shop Cash order %s first.', entry['order_id']))
            else:
                payment = self.env['account.payment'].search([
                    ('shopify_order_transaction_id', '=', entry.get('transaction_id')),
                    ('shopify_instance_id', '=', self.instance_id.id),
                    ('company_id', '=', self.instance_id.shopify_company_id.id)], limit=2)
                if len(payment) != 1:
                    raise UserError(_('Open the Shopify order for Shop Cash transaction %s and use Preview / Repair Payments to record its payment history.', entry.get('transaction_id')))
                order = self._shop_cash_order_for_payment(payment)
            orders |= order
        if not orders:
            raise UserError(transaction.shop_cash_detail_error or _('Reimport this payout to retrieve the Shop Cash order breakdown.'))
        return orders

    def _resolve_shop_cash_payments(self, transaction):
        """Resolve complete allocations; never select payments by amount alone."""
        self.ensure_one()
        kind = transaction.shop_cash_kind
        if kind not in ('shop_cash_credit', 'shop_cash_refund_debit'):
            raise UserError(_('Shop Cash settlement reversals require accounting review.'))
        direction = 'inbound' if kind == 'shop_cash_credit' else 'outbound'
        sign = 1 if direction == 'inbound' else -1
        if transaction.amount * sign <= 0:
            raise UserError(_('The Shop Cash settlement has an unexpected credit/refund direction. Review the source data.'))
        if transaction.currency_id != self.currency_id:
            raise UserError(_('The Shop Cash settlement currency differs from the payout.'))
        entries = transaction.shop_cash_allocations or []
        if not entries:
            raise UserError(transaction.shop_cash_detail_error or _('Reimport this payout to retrieve the Shop Cash order breakdown.'))
        currency = self.currency_id
        try:
            gross = sum(decimal_amount(entry['amount']) for entry in entries)
            if currency.compare_amounts(float(gross), abs(transaction.amount)):
                raise ValueError('Shop Cash order amounts do not equal the settlement gross amount.')
            if all('fee' in entry for entry in entries) and currency.compare_amounts(
                    float(sum(decimal_amount(entry['fee']) for entry in entries)), transaction.fee):
                raise ValueError('Shop Cash order fees do not equal the settlement fee.')
            if all('net' in entry for entry in entries) and currency.compare_amounts(
                    float(sum(abs(decimal_amount(entry['net'])) for entry in entries)), abs(transaction.net_amount)):
                raise ValueError('Shop Cash order net amounts do not equal the settlement net amount.')
            if currency.compare_amounts(transaction.amount - transaction.fee, transaction.net_amount):
                raise ValueError('Shop Cash gross, fee and net amounts do not balance.')
        except (ValueError, KeyError, TypeError) as error:
            raise UserError(str(error)) from error
        payment_obj = self.env['account.payment']
        resolved, used = [], payment_obj
        identities = set()
        for entry in entries:
            amount = float(decimal_amount(entry['amount']))
            transaction_id, order_id = entry.get('transaction_id'), entry.get('order_id')
            identity = ('transaction', transaction_id) if transaction_id else ('order', order_id)
            if amount <= 0 or not identity[1] or identity in identities:
                raise UserError(_('Shop Cash contains missing or duplicate payment identities.'))
            identities.add(identity)
            order = self.env['sale.order']
            if order_id:
                order = order.search([('shopify_order_id', '=', order_id),
                                      ('shopify_instance_id', '=', self.instance_id.id),
                                      ('company_id', '=', self.instance_id.shopify_company_id.id)], limit=2)
                if len(order) != 1:
                    raise UserError(_('Import the missing Shop Cash order %s before processing this payout.', order_id))
            if transaction_id:
                candidates = payment_obj.search([
                    ('shopify_order_transaction_id', '=', transaction_id),
                    ('shopify_instance_id', '=', self.instance_id.id),
                    ('company_id', '=', self.instance_id.shopify_company_id.id),
                ], limit=2)
                if len(candidates) != 1:
                    raise UserError(_('Record the Shop Cash payment/refund for transaction %s before processing this payout.', transaction_id))
                payment_order = self._shop_cash_order_for_payment(candidates)
                if order and order != payment_order:
                    raise UserError(_('Shop Cash payment and settlement refer to different orders.'))
                order = payment_order
                if currency.compare_amounts(amount, candidates.amount) > 0:
                    raise UserError(_('Shop Cash settlement exceeds its payment amount.'))
            else:
                candidates = payment_obj.search([('shopify_cash_order_id', '=', order.id)])
                candidates |= order.invoice_ids.reconciled_payment_ids
                candidates = candidates.filtered(lambda payment:
                    payment.payment_type == direction and payment.state in ('in_process', 'paid')
                    and payment.company_id == self.instance_id.shopify_company_id
                    and payment.currency_id == currency
                    and (not payment.shopify_instance_id or payment.shopify_instance_id == self.instance_id)
                    and self._shop_cash_gateway_matches(payment, order))
                exact = candidates.filtered(lambda payment: not currency.compare_amounts(payment.amount, amount))
                if len(exact) == 1:
                    candidates = exact
                elif len(exact) > 1 or not candidates or currency.compare_amounts(sum(candidates.mapped('amount')), amount):
                    raise UserError(_('Shop Cash order %s has missing or ambiguous payments. Verify their transaction IDs.', order_id))
            if candidates & used:
                raise UserError(_('One Shop Cash payment appears in several settlement allocations.'))
            for payment in candidates:
                if (payment.state not in ('in_process', 'paid') or payment.move_id.state != 'posted'
                        or payment.payment_type != direction or payment.currency_id != currency
                        or payment.company_id != self.instance_id.shopify_company_id
                        or (payment.shopify_instance_id and payment.shopify_instance_id != self.instance_id)
                        or not self._shop_cash_gateway_matches(payment, order)):
                    raise UserError(_('The Shop Cash settlement payment has inconsistent gateway, currency or accounting.'))
            used |= candidates
            resolved.append((candidates, amount * sign))
        return resolved

    def _reconcile_shop_cash_statement(self, statement):
        transaction = statement.payout_line_id
        allocations = self._resolve_shop_cash_payments(transaction)
        payments = self.env['account.payment'].union(*(payment for payment, _amount in allocations))
        payments.flush_recordset()
        self.env.cr.execute('SELECT id FROM account_payment WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(payments.ids)])
        payments.invalidate_recordset()
        payments.move_id.line_ids.invalidate_recordset()
        lines = self.env['account.move.line']
        for candidates, amount in allocations:
            available, payment_lines = 0.0, self.env['account.move.line']
            for payment in candidates:
                total, _currencies, pending = self.get_payment_move_line_amount(statement, payment)
                available += total
                payment_lines |= pending
            if not payment_lines or self.currency_id.compare_amounts(available, amount):
                raise UserError(_('Shop Cash payment is already settled, partly settled or differs from its allocation. Review its outstanding entries.'))
            lines |= payment_lines
        self.shopify_reconcile_bank_statement_line_ept(statement.id, lines.ids)
        self._check_shop_cash_reconciliation(transaction, statement)
        transaction.shop_cash_payment_ids = [Command.set(payments.ids)]

    def _check_shop_cash_reconciliation(self, transaction, statement):
        """A generic account posting is not proof of payment reconciliation."""
        for payments, amount in self._resolve_shop_cash_payments(transaction):
            matched = 0.0
            for payment in payments:
                liquidity, _counterpart, _writeoffs = payment._seek_for_lines()
                for line in liquidity:
                    for partial in line.matched_debit_ids | line.matched_credit_ids:
                        debit = partial.debit_move_id == line
                        other = partial.credit_move_id if debit else partial.debit_move_id
                        if other.move_id == statement.move_id:
                            if line.currency_id == self.currency_id:
                                matched += partial.debit_amount_currency if debit else -partial.credit_amount_currency
                            else:
                                matched += self.instance_id.shopify_company_id.currency_id._convert(
                                    partial.amount * (1 if debit else -1), self.currency_id,
                                    self.instance_id.shopify_company_id, statement.date)
            if self.currency_id.compare_amounts(matched, amount):
                raise UserError(_('Shop Cash settlement %s must reconcile its actual capture/refund payments. Undo any generic credit/debit account posting and match those payments.', transaction.transaction_id))
        return True
