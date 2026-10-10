"""Payout navigation and proven refunds for sales completed in the old system."""
from urllib.parse import quote

from odoo import api, Command, fields, models, _
from odoo.exceptions import UserError
from .. import shopify
from ..shopify.pyactiveresource.connection import Error as ShopifyError
from .shopify_payment_plan import cash_events, fingerprint, money
from .shopify_refund_plan import prove_historical_fulfillment, refund_components, source_date


class ShopifyHistoricalRefundMove(models.Model):
    _inherit = 'account.move'

    shopify_source_order_id = fields.Char(string='Shopify Source Order ID', copy=False, index=True)


class ShopifyHistoricalRefundPayment(models.Model):
    _inherit = 'account.payment'

    shopify_source_order_id = fields.Char(string='Shopify Source Order ID', copy=False, index=True)


class ShopifyPayoutRepairAudit(models.Model):
    _name = 'shopify.payout.repair.audit.ept'
    _description = 'Shopify Payout Repair Audit'
    _order = 'id desc'

    payout_ids = fields.Many2many('shopify.payout.report.ept', 'shopify_payout_repair_audit_rel',
                                 'audit_id', 'payout_id', readonly=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    source_order_id = fields.Char(readonly=True)
    plan = fields.Json(readonly=True)
    credit_ids = fields.Many2many('account.move', readonly=True)
    payment_ids = fields.Many2many('account.payment', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        raise UserError(_('Repair audits are created only by the reviewed repair workflow.'))

    def _record_repair(self, values):
        return super(ShopifyPayoutRepairAudit, self.sudo()).create([values])

    def write(self, values):
        raise UserError(_('Repair audits cannot be edited.'))

    def unlink(self):
        raise UserError(_('Repair audits cannot be deleted.'))


class ShopifyPayoutRepairLine(models.Model):
    _inherit = 'shopify.payout.report.line.ept'

    def _repair_order(self):
        self.ensure_one()
        instance = self.payout_id.instance_id
        order = self.env['sale.order'].search([
            ('shopify_instance_id', '=', instance.id), ('company_id', '=', instance.shopify_company_id.id),
            ('shopify_order_id', '=', self.source_order_id),
        ], limit=2) if self.source_order_id else self.order_id
        if len(order) > 1 or (self.order_id and order != self.order_id):
            raise UserError(_('The payout order reference is ambiguous or inconsistent.'))
        return order

    def action_open_order(self):
        self.ensure_one()
        order = self._repair_order()
        if not order:
            raise UserError(_('This order is absent from Odoo. Use Open Shopify Order or the historical-refund preview.'))
        return {'type': 'ir.actions.act_window', 'res_model': 'sale.order', 'res_id': order.id,
                'view_mode': 'form', 'target': 'current'}

    def action_open_shopify_order(self):
        self.ensure_one()
        host = (self.payout_id.instance_id.shopify_host or '').removeprefix('https://').removeprefix('http://').strip('/')
        if not self.source_order_id or not host or '/' in host:
            raise UserError(_('Configure the store hostname and a source order ID first.'))
        return {'type': 'ir.actions.act_url', 'url': 'https://%s/admin/orders/%s' %
                (host, quote(self.source_order_id, safe='')), 'target': 'new'}

    def _repair_source(self):
        self.ensure_one()
        instance = self.payout_id.instance_id
        instance.connect_in_shopify()
        try:
            payload = shopify.Order.find(self.source_order_id).to_dict()
            result = shopify.Transaction.find(order_id=self.source_order_id, limit=250)
            transactions = [row.to_dict() for row in self.payout_id.shopify_list_all_transactions(result)]
        except ShopifyError as error:
            raise UserError(_('Could not read the complete Shopify order and transaction history.')) from error
        if str(payload.get('id')) != self.source_order_id:
            raise UserError(_('Shopify returned a different order.'))
        return payload, transactions

    def _historical_refund_plan(self, cutover, timezone, sales_journal, post, source=None):
        self.ensure_one()
        payout, instance = self.payout_id, self.payout_id.instance_id
        company, currency = instance.shopify_company_id, payout.currency_id
        payout._check_payout_reimport_period()
        if self.shop_cash_kind or self.transaction_type not in ('refund', 'payment_refund'):
            raise UserError(_('Historical refund preparation requires an individual refund transaction.'))
        order = self._repair_order()
        if order and order.invoice_ids.filtered(lambda move: move.state != 'cancel'):
            raise UserError(_('This order has accounting documents. Use its normal payment-repair workflow.'))
        if not cutover:
            raise UserError(_('Select the fulfillment cutover date.'))
        payload, transactions = source or self._repair_source()
        try:
            prove_historical_fulfillment(payload, cutover, timezone)
            events = cash_events(transactions, self.source_order_id, currency.name)
            if any(source_date(row['timestamp'], timezone) >= cutover for row in events if row['kind'] != 'refund'):
                raise UserError(_('The original collection is on or after cutover. Review its normal invoice/payment accounting.'))
            event = next((row for row in events if row['id'] == self.source_order_transaction_id), None)
            if (not event or event['kind'] != 'refund' or self.amount >= 0
                    or currency.compare_amounts(float(event['amount']), abs(self.amount))):
                raise UserError(_('The payout refund does not match its exact successful Shopify transaction.'))
            if event['gateway'] != 'shopify_payments':
                raise UserError(_('Use the processor-specific workflow for this historical refund gateway.'))
            if source_date(event['timestamp'], timezone) < cutover:
                raise UserError(_('This refund predates cutover and needs opening-balance treatment.'))
            refunds = [refund for refund in payload.get('refunds', [])
                       if event['id'] in {str(row.get('id')) for row in refund.get('transactions', [])}]
            if len(refunds) != 1:
                raise UserError(_('The refund transaction must identify one source refund document.'))
            refund = refunds[0]
            parts, refund_events = refund_components(refund, events, currency.name,
                                                     payload.get('currency', currency.name), currency.compare_amounts)
        except ValueError as error:
            raise UserError(str(error)) from error
        for row in refund_events:
            if source_date(row['timestamp'], timezone) < cutover:
                raise UserError(_('A refund document mixes cash transactions from before and after cutover.'))
            if row['gateway'] != 'shopify_payments':
                raise UserError(_('A historical refund document includes another processor; review its separate settlement.'))
        successful_refunds = {row['id'] for row in events if row['kind'] == 'refund'}
        quantities = {}
        originals = {str(row['id']): row for row in payload.get('line_items', [])}
        for document in payload.get('refunds', []):
            if not successful_refunds.intersection(str(row.get('id')) for row in document.get('transactions', [])):
                continue
            for item in document.get('refund_line_items', []):
                key = str(item.get('line_item_id'))
                try:
                    quantities[key] = quantities.get(key, 0) + money(item.get('quantity'))
                    if key not in originals or quantities[key] > money(originals[key]['quantity']):
                        raise UserError(_('Cumulative refunded quantities exceed the historical order.'))
                except ValueError as error:
                    raise UserError(str(error)) from error
        partners = self.env['shopify.res.partner.ept'].search([
            ('shopify_instance_id', '=', instance.id),
            ('shopify_customer_id', '=', str((payload.get('customer') or {}).get('id') or '')),
        ]).partner_id.commercial_partner_id
        partner = order.partner_id.commercial_partner_id if order else partners
        if len(partner) != 1 or (partners and partners != partner):
            raise UserError(_('Import or correct the Shopify customer mapping before preparing this refund.'))
        receivable = partner.with_company(company).property_account_receivable_id
        if (not receivable or not receivable.reconcile or receivable.account_type != 'asset_receivable'
                or receivable.deprecated or company not in receivable.company_ids):
            raise UserError(_('The customer needs a reconcilable receivable account.'))
        if sales_journal.company_id != company or sales_journal.type != 'sale':
            raise UserError(_('Select a customer invoice journal in the payout company.'))
        credit_date = min(row['date'] for row in refund_events)
        if company.with_context(ignore_exceptions=True)._get_violated_lock_dates(
                fields.Date.to_date(credit_date), True, sales_journal):
            raise UserError(_('The refund date is in a closed accounting period.'))
        credits = self.env['account.move'].search([
            ('shopify_instance_id', '=', instance.id), ('shopify_refund_id', '=', str(refund['id'])),
            ('move_type', '=', 'out_refund'), ('state', '!=', 'cancel'),
        ])
        amount = sum(float(row['amount']) for row in refund_events)
        if len(credits) > 1:
            raise UserError(_('Several credit notes identify this Shopify refund. Review them before proceeding.'))
        if credits and (credits.company_id != company or credits.currency_id != currency
                        or credits.commercial_partner_id != partner
                        or currency.compare_amounts(credits.amount_total, amount)
                        or not credits.is_refund_in_shopify
                        or credits.shopify_source_order_id != self.source_order_id
                        or (credits.shopify_refund_order_id and credits.shopify_refund_order_id != order)):
            raise UserError(_('An existing refund credit has conflicting totals, customer or source identity.'))
        values = self._historical_credit_values(payload, refund, parts, partner, sales_journal, credit_date, order)
        if credits:
            expected, actual = {}, {}
            for command in values['invoice_line_ids']:
                line = command[2]
                expected[line['account_id']] = expected.get(line['account_id'], 0.0) + line['quantity'] * line['price_unit']
            for line in credits.invoice_line_ids:
                actual[line.account_id.id] = actual.get(line.account_id.id, 0.0) + line.price_subtotal
            if (expected.keys() != actual.keys() or credits.invoice_line_ids.tax_ids
                    or any(currency.compare_amounts(expected[key], actual[key]) for key in expected)):
                raise UserError(_('The existing historical credit uses different refund accounts or allocations. Review and correct the credit before matching its cash.'))
        plan_events = []
        for event in refund_events:
            journal = instance.credit_note_payment_journal or instance.shopify_settlement_report_journal_id
            methods = journal.outbound_payment_method_line_ids.filtered(lambda row: row.code == 'manual')
            if (journal.company_id != company or journal.type != 'bank'
                    or (journal.currency_id or company.currency_id) != currency or len(methods) != 1
                    or not methods.payment_account_id or not methods.payment_account_id.reconcile
                    or methods.payment_account_id.deprecated
                    or methods.payment_account_id.account_type not in ('asset_current', 'liability_current')
                    or methods.payment_account_id in (journal.default_account_id, journal.suspense_account_id)
                    or company not in methods.payment_account_id.company_ids
                    or (methods.payment_account_id.currency_id and methods.payment_account_id.currency_id != currency)):
                raise UserError(_('Configure a same-company Manual refund journal with a dedicated outstanding account.'))
            payment = self.env['account.payment'].search([
                ('shopify_instance_id', '=', instance.id), ('shopify_order_transaction_id', '=', event['id']),
            ], limit=2)
            if not payment and self.env['account.payment'].search_count([
                ('company_id', '=', company.id), ('partner_id.commercial_partner_id', '=', partner.id),
                ('journal_id', '=', journal.id), ('currency_id', '=', currency.id),
                ('payment_type', '=', 'outbound'), ('date', '=', event['date']),
                ('amount', '=', float(event['amount'])), ('shopify_order_transaction_id', '=', False),
                ('state', 'not in', ('canceled', 'rejected')),
            ]):
                raise UserError(_('An unidentified refund payment could already represent this cash. Verify and link its transaction ID before retrying.'))
            if payment and (len(payment) != 1 or payment.company_id != company
                            or payment.currency_id != currency or payment.payment_type != 'outbound'
                            or currency.compare_amounts(payment.amount, float(event['amount']))
                            or payment.partner_id.commercial_partner_id != partner or payment.journal_id != journal
                            or payment.move_id.state != 'posted' or payment.state in ('canceled', 'rejected')
                            or payment.shopify_source_order_id != self.source_order_id
                            or payment.shopify_parent_transaction_id != event['parent_id']
                            or payment.shopify_cash_timestamp != event['timestamp']):
                raise UserError(_('An existing refund payment has conflicting accounting or source identity.'))
            if payment:
                liquidity, counterpart, writeoffs = payment._seek_for_lines()
                matches = counterpart.matched_debit_ids | counterpart.matched_credit_ids
                other = (matches.debit_move_id | matches.credit_move_id).move_id - payment.move_id
                if (writeoffs or len(liquidity) != 1 or len(counterpart) != 1
                        or liquidity.account_id != methods.payment_account_id or counterpart.account_id != receivable
                        or other - credits or not credits or credits.state != 'posted'):
                    raise UserError(_('The existing refund payment is allocated outside its historical credit.'))
            elif company.with_context(ignore_exceptions=True)._get_violated_lock_dates(
                    fields.Date.to_date(event['date']), False, journal):
                raise UserError(_('The cash refund date is in a closed accounting period.'))
            plan_events.append(dict(event, journal_id=journal.id, method_id=methods.id,
                                    payment_id=payment.id, payment_write_date=str(payment.write_date),
                                    payment_snapshot=fingerprint([
                                        (line.id, str(line.write_date), line.balance, line.amount_currency,
                                         line.amount_residual, line.amount_residual_currency,
                                         line.matched_debit_ids.ids, line.matched_credit_ids.ids)
                                        for line in payment.move_id.line_ids.sorted('id')])))
        return dict(kind='historical', line_id=self.id, source_order_id=self.source_order_id,
                    order_id=order.id, source_name=payload.get('name') or self.source_order_id,
                    cutover=str(cutover), timezone=timezone, post=post,
                    values=values, amount=amount, events=plan_events, credit_id=credits.id,
                    refund_components=parts,
                    fulfillment_evidence=[dict(status=row.get('status'), created_at=row.get('created_at'),
                                               items=[dict(id=item.get('id'), quantity=item.get('quantity'))
                                                      for item in row.get('line_items', [])])
                                          for row in payload.get('fulfillments', [])],
                    credit_snapshot=fingerprint([(move.id, str(move.write_date), move.state, move.amount_total,
                                                  move.amount_residual,
                                                  [(line.id, str(line.write_date), line.amount_residual,
                                                    line.matched_debit_ids.ids, line.matched_credit_ids.ids)
                                                   for line in move.line_ids.sorted('id')]) for move in credits]),
                    source=fingerprint({'order': payload, 'transactions': transactions}),
                    payout_snapshot=fingerprint([(row.id, str(row.write_date), row.amount, row.is_reconciled,
                                                 str(row.move_id.write_date))
                                                for row in payout.payout_statement_line_ids.sorted('id')]))

    def _historical_credit_values(self, payload, refund, parts, partner, journal, date, order):
        instance, company = self.payout_id.instance_id, self.payout_id.instance_id.shopify_company_id
        lines, tax_amount, resolved = [], 0.0, []
        sources = {str(line['id']): line for line in payload['line_items']}
        for part in parts:
            if part['kind'] == 'item':
                source = sources.get(part['line_id'])
                if not source or money(part['quantity']) > money(source['quantity']):
                    raise UserError(_('A returned item cannot be traced to its original quantity.'))
                products = self.env['shopify.product.product.ept'].with_context(active_test=False).search([
                    ('shopify_instance_id', '=', instance.id), ('variant_id', '=', str(source.get('variant_id') or '')),
                ]).product_id
                name = '%s [%s]' % (source.get('name') or source.get('title') or 'Returned item', source.get('sku') or '')
            else:
                products = instance.shipping_product_id if part['kind'] == 'shipping' else instance.refund_adjustment_product_id
                name = 'Shopify refund %s: %s' % (part['kind'], part.get('reason', refund['id']))
            if len(products) != 1:
                raise UserError(_('Configure one product mapping for each refund item, shipping or adjustment.'))
            product = products.with_company(company)
            account = product.property_account_income_id or product.categ_id.property_account_income_categ_id
            if instance.shopify_fiscal_position_id:
                account = instance.shopify_fiscal_position_id.map_account(account)
            resolved.append((part, name, account))
        component_accounts = self.env['account.account'].browse(sorted({account.id for part, _name, account in resolved
            if part['kind'] != 'adjustment' and account}))
        if len(component_accounts) > 1 and any(part['kind'] == 'adjustment' for part in parts):
            raise UserError(_('The refund adjustment spans multiple revenue accounts. Review and allocate the credit manually before reconciling this refund.'))
        for part, name, account in resolved:
            if part['kind'] == 'adjustment' and component_accounts:
                # Preserve the returned sale's classification, including Readers.
                # The generic adjustment product is only the account fallback
                # when the refund has no item or shipping component to follow.
                account = component_accounts
            if not account or account.deprecated or company not in account.company_ids or account.account_type not in ('income', 'income_other'):
                raise UserError(_('Configure the refund product\'s income/returns account in this company.'))
            # Account-only historical credits do not manufacture an inventory or
            # COGS return for stock movements that were handled outside this order.
            lines.append(Command.create({'name': name, 'account_id': account.id,
                                         'quantity': float(part['quantity']),
                                         'price_unit': float(part['subtotal']) / float(part['quantity']),
                                         'tax_ids': [Command.clear()]}))
            tax_amount += float(part['tax'])
        if not self.payout_id.currency_id.is_zero(tax_amount):
            product = instance.tax_product_id.with_company(company)
            account = instance.credit_tax_account_id or product.property_account_income_id or product.categ_id.property_account_income_categ_id
            if not account or account.deprecated or company not in account.company_ids or account.account_type in ('asset_receivable', 'liability_payable', 'asset_cash'):
                raise UserError(_('Configure the existing Shopify refund tax account.'))
            lines.append(Command.create({'name': 'Shopify refunded tax', 'account_id': account.id,
                                         'quantity': 1, 'price_unit': tax_amount, 'tax_ids': [Command.clear()]}))
        return dict(move_type='out_refund', company_id=company.id, journal_id=journal.id,
                    currency_id=self.payout_id.currency_id.id, partner_id=partner.id, invoice_date=date, date=date,
                    shopify_instance_id=instance.id, shopify_source_order_id=self.source_order_id,
                    shopify_refund_order_id=order.id, shopify_refund_id=str(refund['id']), is_refund_in_shopify=True,
                    ref='Shopify %s / refund %s' % (payload.get('name') or payload['id'], refund['id']),
                    invoice_line_ids=lines)

    def _apply_historical_refund_plan(self, plan, payouts=None):
        instance, company = self.payout_id.instance_id, self.payout_id.instance_id.shopify_company_id
        currency = self.payout_id.currency_id
        credit = self.env['account.move'].browse(plan['credit_id'])
        credit = credit.with_context(tracking_disable=True, mail_create_nosubscribe=True)
        if not credit:
            credit = self.env['account.move'].with_company(company).with_context(
                tracking_disable=True, mail_create_nosubscribe=True).create(plan['values'])
        if currency.compare_amounts(credit.amount_total, plan['amount']):
            raise UserError(_('The prepared credit total differs from Shopify.'))
        if plan['post'] and credit.state == 'draft':
            credit.action_post()
            if credit.date != fields.Date.to_date(plan['values']['date']):
                raise UserError(_('Odoo changed the credit posting date.'))
        payments = self.env['account.payment']
        if credit.state == 'posted':
            for event in plan['events']:
                payment = payments.browse(event['payment_id'])
                if not payment:
                    payment = payments.with_company(company).with_context(
                        tracking_disable=True, mail_create_nosubscribe=True).create({
                        'shopify_instance_id': instance.id, 'shopify_order_transaction_id': event['id'],
                        'shopify_source_order_id': self.source_order_id, 'shopify_cash_order_id': plan['order_id'],
                        'shopify_parent_transaction_id': event['parent_id'], 'shopify_cash_kind': 'refund',
                        'shopify_cash_gateway': event['gateway'], 'shopify_cash_timestamp': event['timestamp'],
                        'partner_id': credit.commercial_partner_id.id, 'partner_type': 'customer',
                        'payment_type': 'outbound', 'journal_id': event['journal_id'],
                        'payment_method_line_id': event['method_id'], 'currency_id': currency.id,
                        'destination_account_id': credit.line_ids.filtered(
                            lambda line: line.account_type == 'asset_receivable').account_id.id,
                        'amount': float(event['amount']), 'date': event['date'],
                        'memo': 'Shopify historical refund %s / %s' % (event['id'], plan['source_name']),
                    })
                    payment.action_post()
                    if payment.move_id.state != 'posted' or payment.move_id.date != fields.Date.to_date(event['date']):
                        raise UserError(_('The refund payment did not post on its source date.'))
                payments |= payment
            receivables = credit.line_ids.filtered(lambda line: line.account_type == 'asset_receivable')
            for payment in payments:
                receivables |= payment._seek_for_lines()[1]
            receivables.filtered(lambda line: not line.reconciled).reconcile()
            if receivables.filtered(lambda line: not line.reconciled):
                raise UserError(_('The refund credit and payments leave a customer balance.'))
        self.env['shopify.payout.repair.audit.ept']._record_repair({
            'payout_ids': [Command.set((payouts or self.payout_id).ids)], 'company_id': company.id,
            'source_order_id': self.source_order_id, 'plan': plan,
            'credit_ids': [Command.set(credit.ids)], 'payment_ids': [Command.set(payments.ids)],
        })
        return credit, payments


class ShopifyPayoutRepair(models.Model):
    _inherit = 'shopify.payout.report.ept'

    repair_audit_ids = fields.Many2many('shopify.payout.repair.audit.ept', 'shopify_payout_repair_audit_rel',
                                      'payout_id', 'audit_id', readonly=True, copy=False)

    def action_bulk_repair_transactions(self):
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_('Only accounting managers can prepare payout repairs.'))
        self.check_access('write')
        wizard = self.env['shopify.payout.repair.ept'].create({'payout_ids': [Command.set(self.ids)]})
        return wizard._reopen()
