"""Shopify cash history drives payments; invoices drive revenue and tax."""
from odoo import Command, fields, models, _
from odoo.exceptions import UserError
from .. import shopify
from ..shopify.pyactiveresource.connection import Error as ShopifyError
from .shopify_payment_plan import cash_events, invoice_mode, prove_net_refunds, fingerprint, component_money, money


class SaleOrderPaymentSync(models.Model):
    _inherit = 'sale.order'

    shopify_payment_sync_revision = fields.Integer(copy=False, readonly=True)
    shopify_payment_audit_ids = fields.One2many('shopify.payment.audit.ept', 'order_id', readonly=True)

    def _lock_shopify_cash(self):
        self.ensure_one()
        self.check_access('write')
        self.flush_recordset()
        self.env.cr.execute('SELECT id FROM sale_order WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()

    def _shopify_cash_source(self):
        """Always fetch complete history, never rely on a single webhook page."""
        self.ensure_one()
        if not self.shopify_instance_id or not self.shopify_order_id:
            raise UserError(_('This order is not linked to Shopify.'))
        self.shopify_instance_id.connect_in_shopify()
        try:
            payload = shopify.Order.find(self.shopify_order_id).to_dict()
            result = shopify.Transaction.find(order_id=self.shopify_order_id, limit=250)
            rows = self.env['shopify.payout.report.ept'].shopify_list_all_transactions(result)
        except ShopifyError as error:
            raise UserError(_('Could not read complete Shopify cash history. Check the connection and retry.')) from error
        if str(payload.get('id')) != self.shopify_order_id:
            raise UserError(_('Shopify returned another order.'))
        return payload, [row.to_dict() for row in rows]

    def _shopify_event_method(self, event):
        """Use recording-only manual methods; never send a payment to a provider."""
        workflows = self.shopify_payment_ids.filtered(
            lambda row: row.payment_gateway_id.code == event['gateway']).workflow_id
        if not workflows:
            if self.shopify_payment_gateway_id.code != event['gateway']:
                raise UserError(_('Configure an unambiguous workflow for gateway %s.', event['gateway']))
            workflows = self.auto_workflow_process_id
        journals = workflows.journal_id
        if len(journals) != 1:
            raise UserError(_('Gateway %s must identify one payment journal.', event['gateway']))
        journal = journals
        if event['kind'] == 'refund' and self.shopify_instance_id.credit_note_payment_journal:
            journal = self.shopify_instance_id.credit_note_payment_journal
        direction = 'outbound' if event['kind'] == 'refund' else 'inbound'
        methods = (journal.outbound_payment_method_line_ids if direction == 'outbound'
                   else journal.inbound_payment_method_line_ids).filtered(lambda row: row.code == 'manual')
        if len(methods) != 1 or not methods.payment_account_id:
            raise UserError(_('Configure one Manual %s method with an outstanding account on %s.', direction, journal.name))
        account = methods.payment_account_id
        if (journal.company_id != self.company_id or journal.type != 'bank'
                or (journal.currency_id or self.company_id.currency_id) != self.currency_id
                or not account.reconcile or account.deprecated
                or account.account_type not in ('asset_current', 'liability_current')
                or account in (journal.default_account_id, journal.suspense_account_id)
                or self.company_id not in account.company_ids
                or (account.currency_id and account.currency_id != self.currency_id)):
            raise UserError(_('Use a same-company bank journal in the order currency and a dedicated reconcilable outstanding account.'))
        return methods

    def _shopify_refund_documents(self, payload, events, invoices, credits, mode, embedded_ids=()):
        refund_events = {row['id']: row for row in events if row['kind'] == 'refund'}
        if mode == 'net':
            quantities = {}
            for line in invoices.invoice_line_ids:
                key = line.sale_line_ids.shopify_line_id
                if key:
                    quantities[key] = quantities.get(key, 0) + line.quantity
            prove_net_refunds(payload, events, quantities)
            return []
        prepared = []
        covered = set()
        refunded_quantities = {}
        known_credits = self.env['account.move']
        for refund in payload.get('refunds', []):
            ids = {str(row.get('id')) for row in refund.get('transactions', [])} & refund_events.keys()
            if not ids:
                continue
            if covered & ids:
                raise UserError(_('A refund transaction occurs in more than one refund document.'))
            covered |= ids
            if ids & set(embedded_ids):
                if not ids <= set(embedded_ids):
                    raise UserError(_('A refund document mixes previously embedded and new cash refunds. Review its credit note manually.'))
                continue
            amount = sum(float(refund_events[key]['amount']) for key in ids)
            for item in refund.get('refund_line_items', []):
                key = str(item.get('line_item_id'))
                quantity = money(item.get('quantity'))
                refunded_quantities[key] = refunded_quantities.get(key, 0) + quantity
                original = invoices.invoice_line_ids.filtered(lambda line: line.sale_line_ids.shopify_line_id == key)
                if quantity <= 0 or (original and refunded_quantities[key] > money(sum(original.mapped('quantity')))):
                    raise UserError(_('Cumulative refunded quantities exceed the original invoice. Review the refund documents.'))
            credit = credits.filtered(lambda move: move.shopify_refund_id == str(refund.get('id')))
            if credit:
                if len(credit) != 1 or self.currency_id.compare_amounts(credit.amount_total, amount):
                    raise UserError(_('A posted credit note differs from its Shopify refund transactions.'))
                known_credits |= credit
                continue
            prepared.append({'values': self._shopify_credit_values(
                refund, amount, invoices, min(refund_events[key]['date'] for key in ids),
                payload.get('currency', self.currency_id.name)), 'amount': amount})
        if covered != refund_events.keys() or credits - known_credits:
            raise UserError(_('Cash refunds and existing credit notes must each have a unique Shopify refund link.'))
        return prepared

    def _shopify_credit_values(self, refund, amount, invoices, date, shop_currency):
        """Build supported item/shipping refunds; verify tax and total before posting."""
        if refund.get('order_adjustments') or refund.get('duties') or refund.get('additional_fees'):
            raise UserError(_('Refund adjustments, duties or additional fees need a reviewed credit note linked by Shopify Refund ID.'))
        if len(invoices) != 1:
            raise UserError(_('Create and link the refund credit note manually when an order has multiple invoices.'))
        invoice = invoices
        lines = []
        tax_total = 0.0
        components = []
        for item in refund.get('refund_line_items', []):
            original = invoice.invoice_line_ids.filtered(
                lambda line: line.sale_line_ids.shopify_line_id == str(item.get('line_item_id')))
            components.append((original, float(item['quantity']),
                               float(component_money(item, 'subtotal', self.currency_id.name, shop_currency)),
                               float(component_money(item, 'total_tax', self.currency_id.name, shop_currency))))
        for shipping in refund.get('refund_shipping_lines', []):
            original = invoice.invoice_line_ids.filtered(
                lambda line: line.product_id == self.shopify_instance_id.shipping_product_id)
            subtotal = component_money(shipping, 'subtotal_amount', self.currency_id.name, shop_currency)
            if shipping.get('tax_amount_set') or 'tax_amount' in shipping:
                tax = component_money(shipping, 'tax_amount', self.currency_id.name, shop_currency)
            elif 'tax_lines' in shipping.get('shipping_line', {}) and not shipping['shipping_line']['tax_lines']:
                tax = 0
            else:
                raise UserError(_('Refunded shipping tax is not explicitly available; create a reviewed credit note.'))
            components.append((original, 1.0, float(subtotal), float(tax)))
        for original, qty, subtotal, tax in components:
            if len(original) != 1 or qty <= 0 or qty > original.quantity or subtotal < 0 or tax < 0:
                raise UserError(_('Refund components cannot be uniquely traced to the original invoice.'))
            if original.tax_ids:
                # Price-included taxes need the tax-inclusive base.
                included = all(original.tax_ids.mapped('price_include'))
                if any(original.tax_ids.mapped('price_include')) and not included:
                    raise UserError(_('Mixed tax-included/excluded refund taxes require a reviewed credit note.'))
                price = (subtotal + tax if included else subtotal) / qty
                computed = original.tax_ids.compute_all(price, currency=self.currency_id, quantity=qty,
                                                        product=original.product_id, partner=invoice.partner_id,
                                                        is_refund=True)
                if (self.currency_id.compare_amounts(computed['total_excluded'], subtotal)
                        or self.currency_id.compare_amounts(computed['total_included'], subtotal + tax)):
                    raise UserError(_('Shopify refund tax differs from the original invoice tax configuration.'))
            else:
                price = subtotal / qty
                tax_total += tax
            lines.append(Command.create({
                'product_id': original.product_id.id, 'name': original.name,
                'account_id': original.account_id.id, 'quantity': qty, 'price_unit': price,
                'product_uom_id': original.product_uom_id.id, 'discount': 0,
                'analytic_distribution': original.analytic_distribution,
                'tax_ids': [Command.set(original.tax_ids.ids)],
                'sale_line_ids': [Command.set(original.sale_line_ids.ids)],
            }))
        if tax_total:
            tax_product = self.shopify_instance_id.tax_product_id or self.env.ref('shopify_ept.shopify_tax_product', False)
            original_tax = invoice.invoice_line_ids.filtered(
                lambda line: line.product_id == tax_product and not line.tax_ids)
            if len(original_tax) != 1:
                raise UserError(_('The separate Shopify tax line cannot be identified on the original invoice.'))
            lines.append(Command.create({'product_id': original_tax.product_id.id, 'name': original_tax.name,
                                         'account_id': original_tax.account_id.id, 'quantity': 1,
                                         'price_unit': tax_total, 'tax_ids': [Command.clear()]}))
        if not lines or self.currency_id.compare_amounts(sum(part[2] + part[3] for part in components), amount):
            raise UserError(_('Refund items, shipping and taxes do not explain the successful cash refund.'))
        if self.company_id._get_violated_lock_dates(fields.Date.to_date(date), True, invoice.journal_id):
            raise UserError(_('The refund date is locked. A reviewed accounting correction is required.'))
        return {'move_type': 'out_refund', 'journal_id': invoice.journal_id.id,
                'company_id': self.company_id.id, 'partner_id': invoice.partner_id.id,
                'currency_id': self.currency_id.id, 'invoice_date': date, 'date': date,
                'reversed_entry_id': invoice.id, 'shopify_instance_id': self.shopify_instance_id.id,
                'shopify_refund_id': str(refund['id']), 'is_refund_in_shopify': True,
                'ref': 'Shopify refund %s' % refund['id'], 'invoice_line_ids': lines}

    def _shopify_payment_candidates(self, invoices, events):
        return (invoices.reconciled_payment_ids | invoices.matched_payment_ids
                | self.env['account.payment'].search([
                    '|', ('shopify_cash_order_id', '=', self.id),
                    '&', ('shopify_instance_id', '=', self.shopify_instance_id.id),
                    ('shopify_order_transaction_id', 'in', [row['id'] for row in events]),
                ])).filtered(lambda pay: pay.state not in ('canceled', 'rejected'))

    def _check_shopify_replacement(self, payment, invoices):
        if (not payment.move_id or payment.move_id.state != 'posted' or payment.move_id.inalterable_hash
                or payment.move_id.need_cancel_request):
            raise UserError(_('This legacy payment cannot be safely replaced automatically.'))
        payment.move_id._check_fiscal_lock_dates()
        payment.move_id.line_ids._check_tax_lock_date()
        liquidity, counterpart, writeoffs = payment._seek_for_lines()
        if (len(liquidity) != 1 or writeoffs or len(counterpart) != 1
                or liquidity.matched_debit_ids or liquidity.matched_credit_ids):
            raise UserError(_('The legacy payment has a bank match, write-off or complex journal entry; review it manually.'))
        matches = counterpart.matched_debit_ids | counterpart.matched_credit_ids
        cash_basis = self.env['account.move'].search([('tax_cash_basis_rec_id', 'in', matches.ids)])
        cash_basis._check_fiscal_lock_dates()
        cash_basis.line_ids._check_tax_lock_date()
        other_moves = (matches.debit_move_id | matches.credit_move_id).move_id - payment.move_id
        if other_moves - invoices or payment.invoice_ids - invoices:
            raise UserError(_('The legacy payment is applied outside this order.'))
        payouts = self.env['shopify.payout.report.line.ept'].search([
            ('order_id', '=', self.id)]).payout_id
        if payouts.settlement_move_id:
            raise UserError(_('An affected payout already has a settlement transfer. Review the correction manually.'))

    def _build_shopify_cash_plan(self, repair=False):
        self.ensure_one()
        payload, transactions = self._shopify_cash_source()
        try:
            events = cash_events(transactions, self.shopify_order_id, self.currency_id.name)
            documents = self.invoice_ids.filtered(lambda move: move.state != 'cancel')
            if documents.filtered(lambda move: move.state != 'posted'):
                raise UserError(_('Post or remove draft invoices/credit notes before synchronizing cash transactions.'))
            invoices = documents.filtered(lambda move: move.move_type == 'out_invoice')
            credits = documents.filtered(lambda move: move.move_type == 'out_refund')
            if not invoices or documents.filtered(lambda move: move.company_id != self.company_id
                                                  or move.currency_id != self.currency_id):
                raise UserError(_('Posted invoices in the order company and currency are required.'))
            if documents.invoice_line_ids.sale_line_ids.order_id - self:
                raise UserError(_('An invoice includes another order. Allocate its payments manually before using this repair.'))
            receivable = documents.line_ids.filtered(lambda row: row.account_type == 'asset_receivable').account_id
            if len(receivable) != 1 or not receivable.reconcile:
                raise UserError(_('The order documents must use one reconcilable receivable account.'))
            prior = self.shopify_payment_audit_ids.sorted('id', reverse=True)[:1].plan or {}
            embedded_ids = prior.get('embedded_refund_ids', [])
            refund_events = {row['id']: row for row in events if row['kind'] == 'refund'}
            if (not set(embedded_ids) <= refund_events.keys()
                    or (embedded_ids and sorted(invoices.ids) != sorted(prior.get('invoice_ids', [])))):
                raise UserError(_('The invoice or refunds from the previous net-invoice audit are missing. Review the history.'))
            embedded_amount = sum(money(refund_events[key]['amount']) for key in embedded_ids)
            mode = invoice_mode(events, sum(invoices.mapped('amount_total')), sum(credits.mapped('amount_total')),
                                self.currency_id.compare_amounts, embedded_amount)
            credit_values = self._shopify_refund_documents(payload, events, invoices, credits, mode, embedded_ids)
            if mode == 'net':
                embedded_ids = sorted(refund_events)
        except (KeyError, TypeError, ValueError) as error:
            raise UserError(str(error)) from error
        candidates = self._shopify_payment_candidates(documents, events)
        remaining = candidates
        for event in events:
            method = self._shopify_event_method(event)
            event.update(journal_id=method.journal_id.id, method_id=method.id,
                         outstanding_id=method.payment_account_id.id,
                         direction='outbound' if event['kind'] == 'refund' else 'inbound')
        identified = set(candidates.mapped('shopify_order_transaction_id'))
        for event in events:
            method = self.env['account.payment.method.line'].browse(event['method_id'])
            matched = remaining.filtered(lambda pay: pay.shopify_order_transaction_id == event['id'])
            if not matched:
                matched = remaining.filtered(lambda pay: not pay.shopify_order_transaction_id
                    and pay.payment_type == event['direction'] and pay.currency_id == self.currency_id
                    and pay.journal_id == method.journal_id
                    and pay.date == fields.Date.to_date(event['date'])
                    and self.currency_id.compare_amounts(pay.amount, float(event['amount'])) == 0)
                possible = [row for row in events if row['id'] not in identified
                            and row['direction'] == event['direction'] and row['journal_id'] == event['journal_id']
                            and row['date'] == event['date']
                            and not self.currency_id.compare_amounts(float(row['amount']), float(event['amount']))]
                if matched and len(possible) != 1:
                    raise UserError(_('A legacy payment could represent several identical Shopify transactions. Link its verified transaction ID before retrying.'))
            if len(matched) > 1:
                raise UserError(_('Several legacy payments could match transaction %s. Review their identity.', event['id']))
            if matched:
                if (matched.payment_type != event['direction'] or matched.currency_id != self.currency_id
                        or matched.company_id != self.company_id or matched.journal_id != method.journal_id
                        or matched.date != fields.Date.to_date(event['date'])
                        or self.currency_id.compare_amounts(matched.amount, float(event['amount']))
                        or matched.partner_id.commercial_partner_id != self.partner_id.commercial_partner_id
                        or (matched.shopify_cash_order_id and matched.shopify_cash_order_id != self)
                        or matched.invoice_ids - documents
                        or matched.reconciled_invoice_ids - documents or matched.reconciled_bill_ids
                        or (matched.shopify_instance_id and matched.shopify_instance_id != self.shopify_instance_id)
                        or matched.move_id.state != 'posted'):
                    raise UserError(_('Existing payment for transaction %s has inconsistent accounting; review it.', event['id']))
                for field_name, expected in (
                        ('shopify_parent_transaction_id', event['parent_id']), ('shopify_cash_kind', event['kind']),
                        ('shopify_cash_gateway', event['gateway']), ('shopify_cash_timestamp', event['timestamp'])):
                    if matched[field_name] and matched[field_name] != expected:
                        raise UserError(_('Previously recorded transaction metadata changed. Review Shopify transaction %s.', event['id']))
                liquidity, counterpart, writeoffs = matched._seek_for_lines()
                if (len(liquidity) != 1 or writeoffs or len(counterpart) != 1
                        or counterpart.account_id != receivable
                        or liquidity.account_id != method.payment_account_id
                        or self.currency_id.compare_amounts(abs(liquidity.amount_currency), float(event['amount']))):
                    raise UserError(_('The existing payment does not have the expected outstanding-account entry.'))
                remaining -= matched
            event['payment_id'] = matched.id if matched else False
            if not matched and self.company_id._get_violated_lock_dates(
                    fields.Date.to_date(event['date']), False, method.journal_id):
                raise UserError(_('The transaction date is locked. A reviewed accounting correction is required.'))
        replacements = self.env['account.payment']
        if remaining:
            # Only the demonstrable single net-payment legacy shape is replaced.
            if (not repair or mode != 'net' or len(remaining) != 1 or remaining.payment_type != 'inbound'
                    or remaining.shopify_order_transaction_id or remaining.currency_id != self.currency_id
                    or remaining.company_id != self.company_id
                    or (remaining.shopify_instance_id and remaining.shopify_instance_id != self.shopify_instance_id)
                    or (remaining.shopify_cash_order_id and remaining.shopify_cash_order_id != self)
                    or remaining.journal_id.id not in [row['journal_id'] for row in events if row['direction'] == 'inbound']
                    or remaining.partner_id.commercial_partner_id != self.partner_id.commercial_partner_id
                    or self.currency_id.compare_amounts(remaining.amount, sum(invoices.mapped('amount_total')))
                    or any(row['payment_id'] for row in events if row['direction'] == 'inbound')):
                raise UserError(_('Existing payments do not match Shopify cash history. Use Preview / Repair Shopify Payments; ambiguous cases require accounting review.'))
            self._check_shopify_replacement(remaining, documents)
            replacements = remaining
        # Snapshot allocations as well as payment headers: a new bank match must invalidate a preview.
        items = candidates.move_id.line_ids | documents.line_ids
        snapshot = {
            'revision': self.shopify_payment_sync_revision,
            'order_write_date': str(self.write_date),
            'documents': [(move.id, str(move.write_date), move.state, move.amount_total, move.amount_residual)
                          for move in documents.sorted('id')],
            'payments': [(pay.id, str(pay.write_date), pay.state, pay.amount, pay.shopify_order_transaction_id)
                         for pay in candidates.sorted('id')],
            'items': [(line.id, str(line.write_date), line.balance, line.amount_currency,
                       line.amount_residual, line.amount_residual_currency,
                       line.matched_debit_ids.ids, line.matched_credit_ids.ids) for line in items.sorted('id')],
        }
        return {'order_id': self.id, 'mode': mode, 'events': events, 'credits': credit_values,
                'embedded_refund_ids': embedded_ids, 'invoice_ids': invoices.ids,
                'source_fingerprint': fingerprint({'order': payload, 'transactions': transactions}),
                'receivable_account_id': receivable.id,
                'replace_ids': replacements.ids, 'document_ids': documents.ids,
                'snapshot': fingerprint(snapshot)}

    def _apply_shopify_cash_plan(self, plan):
        """Caller holds the order lock and supplies a freshly rebuilt plan."""
        documents = self.env['account.move'].browse(plan['document_ids'])
        replaced = self.env['account.payment'].browse(plan['replace_ids'])
        existing = self.env['account.payment'].browse([event['payment_id'] for event in plan['events'] if event['payment_id']])
        if (not replaced and not plan['credits'] and len(existing) == len(plan['events'])
                and all(pay.shopify_cash_order_id == self for pay in existing)
                and all(line.reconciled for line in (documents.line_ids | existing.move_id.line_ids).filtered(
                    lambda line: line.account_type == 'asset_receivable'))):
            return existing
        for payment in replaced:
            self._check_shopify_replacement(payment, documents)
            # Preserve the original entry and its reversal instead of deleting history.
            move = payment.move_id
            move.line_ids.remove_move_reconcile()
            reversal = move._reverse_moves([{'date': move.date, 'ref': 'Shopify cash-history repair: %s' % move.name}], cancel=True)
            if reversal.date != move.date:
                raise UserError(_('The reversal date changed; a reviewed accounting correction is required.'))
            payment.write({'state': 'canceled', 'invoice_ids': [Command.clear()]})
            for document in documents:
                document.matched_payment_ids -= payment
            payment.message_post(body=_('Replaced through Shopify cash-history repair; reversal: %s.', reversal.name))
        credits = self.env['account.move']
        for planned_credit in plan['credits']:
            values = planned_credit['values']
            credit = self.env['account.move'].with_company(self.company_id).create(values)
            # The component amounts were checked at preview; posting must preserve them.
            if (not credit.invoice_line_ids
                    or self.currency_id.compare_amounts(credit.amount_total, planned_credit['amount'])):
                raise UserError(_('The calculated credit note total differs from the confirmed Shopify refund.'))
            credit.action_post()
            if credit.date != fields.Date.to_date(values['date']):
                raise UserError(_('Odoo moved the refund date into another accounting period. Review it manually.'))
            credits |= credit
        documents |= credits
        payments = self.env['account.payment']
        for event in plan['events']:
            payment = self.env['account.payment'].browse(event['payment_id'])
            metadata = {'shopify_instance_id': self.shopify_instance_id.id,
                        'shopify_order_transaction_id': event['id'], 'shopify_cash_order_id': self.id,
                        'shopify_parent_transaction_id': event['parent_id'], 'shopify_cash_kind': event['kind'],
                        'shopify_cash_gateway': event['gateway'], 'shopify_cash_timestamp': event['timestamp']}
            if not payment:
                payment = self.env['account.payment'].with_company(self.company_id).create({
                    **metadata, 'partner_id': self.partner_id.commercial_partner_id.id,
                    'partner_type': 'customer', 'payment_type': event['direction'],
                    'journal_id': event['journal_id'], 'payment_method_line_id': event['method_id'],
                    'destination_account_id': plan['receivable_account_id'],
                    'currency_id': self.currency_id.id, 'amount': float(event['amount']),
                    'date': event['date'], 'memo': 'Shopify %s %s / %s' % (event['kind'], event['id'], self.name),
                })
                payment.action_post()
                if (not payment.move_id or payment.move_id.state != 'posted'
                        or payment.move_id.date != fields.Date.to_date(event['date'])):
                    raise UserError(_('The cash transaction could not be posted on its original date.'))
            else:
                payment.write(metadata)
            payments |= payment
        # Reconcile receivables only. Preserve each cash movement for its own payout.
        receivables = documents.line_ids.filtered(lambda line: line.account_type == 'asset_receivable')
        for payment in payments:
            receivables |= payment._seek_for_lines()[1].filtered(lambda line: line.account_type == 'asset_receivable')
        for account in receivables.account_id:
            lines = receivables.filtered(lambda line: line.account_id == account and not line.reconciled)
            if lines:
                lines.reconcile()
        if receivables.filtered(lambda line: not line.reconciled):
            raise UserError(_('The order documents and reconstructed cash transactions leave a customer balance; the synchronization was rolled back.'))
        self.prepare_vals_shopify_multi_payment_refund([], self)
        self.write({'shopify_payment_sync_revision': self.shopify_payment_sync_revision + 1})
        audit = self.env['shopify.payment.audit.ept']._record_sync({
            'order_id': self.id, 'plan': plan, 'payment_ids': [Command.set(payments.ids)],
            'replaced_payment_ids': [Command.set(replaced.ids)], 'credit_note_ids': [Command.set(credits.ids)],
        })
        self.message_post(body=_('Shopify cash transactions synchronized. Audit %s; %s payment(s), %s replacement(s).',
                                 audit.id, len(payments), len(replaced)))
        return payments

    def _sync_shopify_cash(self):
        self._lock_shopify_cash()
        with self.env.cr.savepoint():
            plan = self._build_shopify_cash_plan()
            return self._apply_shopify_cash_plan(plan)

    def prepare_final_list_of_transactions(self, transactions):
        """Gateway records retain successful charges even after a later refund."""
        charges = {}
        for transaction in transactions or []:
            if transaction.get('status') != 'success' or transaction.get('kind') not in ('sale', 'capture'):
                continue
            key = str(transaction.get('id') or '')
            if not key or (key in charges and charges[key] != transaction):
                raise UserError(_('Successful charge identity is missing or inconsistent.'))
            charges[key] = transaction
        return list(charges.values())

    def prepare_vals_shopify_multi_payment_refund(self, order_refunds, order):
        """Recompute refundable amounts by parent ID; webhook retries are idempotent."""
        # The preceding cash sync used complete, current history. Older queue
        # payloads must not restore amounts that have already been refunded.
        cash_payments = self.env['account.payment'].search([
            ('shopify_cash_order_id', '=', order.id), ('state', 'in', ('in_process', 'paid')),
        ])
        if cash_payments:
            order_refunds = [{'transactions': [
                {'id': pay.shopify_order_transaction_id, 'parent_id': pay.shopify_parent_transaction_id,
                 'amount': pay.amount, 'kind': 'refund', 'status': 'success'}
                for pay in cash_payments if pay.shopify_cash_kind == 'refund'
            ]}]
        seen, totals = {}, {}
        for refund in order_refunds or []:
            for transaction in refund.get('transactions', []):
                if transaction.get('kind') != 'refund' or transaction.get('status') != 'success':
                    continue
                key, parent = str(transaction.get('id') or ''), str(transaction.get('parent_id') or '')
                amount = money(transaction.get('amount'))
                if not key or not parent or amount < 0:
                    raise UserError(_('Refund transaction identity is incomplete.'))
                if key in seen:
                    if seen[key] != (parent, amount):
                        raise UserError(_('Conflicting copies of a Shopify refund were received.'))
                    continue
                seen[key] = (parent, amount)
                totals[parent] = totals.get(parent, 0) + amount
        for payment in order.shopify_payment_ids:
            remaining = order.currency_id.round(float(money(payment.amount) - totals.get(payment.payment_transaction_id, 0)))
            if order.currency_id.compare_amounts(remaining, 0) < 0:
                raise UserError(_('Refunds exceed the original gateway payment.'))
            payment.write({'remaining_refund_amount': remaining,
                           'is_fully_refunded': order.currency_id.is_zero(remaining)})
        return True

    def paid_invoice_ept(self, invoices):
        if not self.shopify_instance_id:
            return super().paid_invoice_ept(invoices)
        if not invoices and not self.invoice_ids:
            return True
        self._sync_shopify_cash()
        return True

    def create_shopify_partially_refund(self, refunds_data, order_name, created_by='', shopify_financial_status=''):
        # One path decides whether a credit note is needed and records actual cash.
        if not self.shopify_instance_id:
            return super().create_shopify_partially_refund(refunds_data, order_name, created_by, shopify_financial_status)
        if not self.auto_workflow_process_id.register_payment:
            raise UserError(_('Enable transaction payment recording in the Shopify workflow before importing refunds.'))
        self._sync_shopify_cash()
        return False

    def action_preview_shopify_payments(self):
        if not self.env.user.has_group('account.group_account_manager'):
            raise UserError(_('Only accounting managers can preview and apply Shopify payment repairs.'))
        wizard = self.env['shopify.payment.repair.ept'].create({'order_ids': [Command.set(self.ids)]})
        wizard.action_preview()
        return {'type': 'ir.actions.act_window', 'res_model': wizard._name, 'res_id': wizard.id,
                'name': _('Preview / Repair Shopify Payments'), 'view_mode': 'form', 'target': 'new'}
