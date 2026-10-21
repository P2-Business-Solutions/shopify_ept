"""Old-system receipts must clear opening cash, without repeating revenue or AR."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
if ODOO_AVAILABLE:
    from odoo import Command, fields
    from odoo.exceptions import UserError


class TestShopifyHistoricalCollection(PayoutTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref('account.group_account_manager')
        cls.outstanding = cls.env['account.account'].create(dict(name='Historical Collection Outstanding',
            code='HCOUT', account_type='asset_current', reconcile=True, company_ids=[Command.set(cls.env.company.ids)]))
        cls.journal.inbound_payment_method_line_ids.payment_account_id = cls.outstanding
        cls.partner = cls.env['res.partner'].create(dict(name='Historical Collection Customer'))
        cls.env['shopify.res.partner.ept'].create(dict(partner_id=cls.partner.id,
            shopify_instance_id=cls.instance.id, shopify_customer_id='historical-customer'))

    def _collection(self, suffix='one'):
        date = fields.Date.today()
        cutover = date - timedelta(days=10)
        source_id = 'old-order-' + suffix
        currency = self.env.company.currency_id
        event = dict(id='old-capture-' + suffix, order_id=source_id, currency=currency.name,
            gateway='shopify_payments', status='success', kind='sale', amount='287.83',
            processed_at=(cutover - timedelta(days=1)).isoformat() + 'T23:30:00-04:00')
        payload = dict(id=source_id, name='#134775-' + suffix, currency=currency.name,
            customer=dict(id='historical-customer'), line_items=[dict(id='old-item', quantity=1)],
            fulfillments=[dict(status='success', created_at=event['processed_at'],
                               line_items=[dict(id='old-item', quantity=1)])])
        payout = self.env['shopify.payout.report.ept'].create(dict(instance_id=self.instance.id,
            payout_reference_id='old-payout-' + suffix, payout_date=date, amount=287.83,
            currency_id=currency.id, payout_transaction_ids=[Command.create(dict(transaction_type='charge',
                transaction_id='old-balance-' + suffix, source_order_id=source_id,
                source_order_transaction_id=event['id'], amount=287.83))]))
        wizard = self.env['shopify.payout.repair.ept'].create(dict(payout_ids=[Command.set(payout.ids)],
            cutover_date=cutover, timezone='America/New_York', clear_historical_collections=True,
            historical_collection_account_id=self.journal.default_account_id.id, rematch_payouts=False))
        return payout, wizard, payload, [event]

    def _matcher(self, payout):
        if 'bank.rec.widget' in self.env.registry.models:
            return nullcontext()
        def match(model, statement_id, line_ids):
            statement = self.env['account.bank.statement.line'].browse(statement_id)
            lines = self.env['account.move.line'].browse(line_ids)
            statement._seek_for_lines()[1].account_id = lines.account_id
            (lines | statement.line_ids.filtered(lambda row: row.account_id == lines.account_id)).reconcile()
        return patch.object(type(payout), 'shopify_reconcile_bank_statement_line_ept', match)

    def _payment(self, event, identified=True):
        return self.env['account.payment'].create(dict(partner_id=self.partner.id, partner_type='customer',
            payment_type='inbound', journal_id=self.journal.id,
            payment_method_line_id=self.journal.inbound_payment_method_line_ids[:1].id,
            amount=float(event['amount']), currency_id=self.env.company.currency_id.id,
            date=event['processed_at'][:10], shopify_instance_id=self.instance.id if identified else False,
            shopify_order_transaction_id=event['id'] if identified else False))

    def test_preview_requires_opening_balance_confirmation_and_creates_no_accounting(self):
        payout, wizard, payload, events = self._collection()
        wizard.clear_historical_collections = False
        count = self.env['account.move'].search_count([])
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')
            self.assertIn('opening-balance collection', wizard.line_ids.detail)
            self.assertIn('#134775', wizard.line_ids.detail)
            wizard.clear_historical_collections = True
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'ready')
            self.assertEqual(wizard.line_ids.kind, 'collection')
            self.assertIn(self.outstanding.name, wizard.line_ids.review_html)
            self.assertIn(self.journal.default_account_id.name, wizard.line_ids.review_html)
        self.assertEqual(self.env['account.move'].search_count([]), count)
        self.assertFalse(payout.repair_audit_ids)

    def test_existing_statement_matches_without_duplicating_opening_cash_or_customer_accounting(self):
        payout, wizard, payload, events = self._collection()
        account = self.journal.default_account_id
        baseline = sum(self.env['account.move.line'].search([('account_id', '=', account.id)]).mapped('balance'))
        payments = self.env['account.payment'].search_count([])
        orders = self.env['sale.order'].search_count([])
        moves = self.env['stock.picking'].search_count([])
        invoices = self.env['account.move'].search_count([('move_type', '!=', 'entry')])
        payout.generate_bank_statement()
        statement = payout.payout_statement_line_ids
        wizard.rematch_payouts = True
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            wizard.action_preview()
            wizard.action_apply()
            clearing = wizard.line_ids.collection_move_ids
            self.assertEqual(wizard.line_ids.status, 'done')
            self.assertEqual(clearing.state, 'posted')
            self.assertEqual(clearing.date, payout.payout_date)
            self.assertEqual(clearing.line_ids.filtered(lambda row: row.account_id == account).balance, -287.83)
            self.assertTrue(clearing.line_ids.filtered(lambda row: row.account_id == self.outstanding).reconciled)
            self.assertEqual(payout.payout_statement_line_ids, statement)
            self.assertTrue(statement.is_reconciled)
            self.assertEqual(payout.state, 'validated')
            self.assertEqual(sum(self.env['account.move.line'].search([('account_id', '=', account.id)]).mapped('balance')), baseline)
            self.assertEqual(self.env['account.payment'].search_count([]), payments)
            self.assertEqual(self.env['sale.order'].search_count([]), orders)
            self.assertEqual(self.env['stock.picking'].search_count([]), moves)
            self.assertEqual(self.env['account.move'].search_count([('move_type', '!=', 'entry')]), invoices)
            wizard.open_only = False
            wizard.action_preview()
            wizard.action_apply()
            self.assertEqual(wizard.line_ids.collection_move_ids, clearing)
            self.assertEqual(len(payout.repair_audit_ids), 1)

    def test_clearing_matches_a_statement_generated_after_repair(self):
        payout, wizard, payload, events = self._collection()
        wizard.rematch_payouts = True
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            wizard.action_preview()
            wizard.action_apply()
        self.assertTrue(payout.payout_statement_line_ids.is_reconciled)
        self.assertEqual(payout.state, 'validated')

    def test_payout_reprocess_reuses_reviewed_collection_without_importing_order(self):
        payout, wizard, payload, events = self._collection()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            wizard.action_apply()
        payout.generate_bank_statement()
        with self._matcher(payout), patch.object(type(payout), 'get_invoices_for_reconcile', side_effect=AssertionError('Import')):
            payout.process_bank_statement()
        self.assertTrue(payout.payout_statement_line_ids.is_reconciled)

    def test_changed_source_or_closed_period_stays_unposted(self):
        payout, wizard, payload, events = self._collection()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            payload['note'] = 'source changed'
            wizard.action_apply()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertFalse(payout.repair_audit_ids)
        with patch.object(type(payout), '_check_payout_reimport_period', side_effect=UserError('Period closed')):
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'review')

    def test_settings_change_and_new_order_require_fresh_review(self):
        payout, wizard, payload, events = self._collection()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            wizard.clear_historical_collections = False
            with self.assertRaises(UserError):
                wizard.action_apply()
            wizard.clear_historical_collections = True
            self.env['sale.order'].create(dict(partner_id=self.partner.id, shopify_instance_id=self.instance.id,
                shopify_order_id=payload['id']))
            wizard.action_apply()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertFalse(payout.repair_audit_ids)

    def test_post_failure_rolls_back_clearing_and_audit(self):
        payout, wizard, payload, events = self._collection()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            with patch.object(type(self.env['account.move']), 'action_post', side_effect=UserError('Post failed')):
                wizard.action_apply()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertFalse(payout.repair_audit_ids)
        self.assertFalse(self.env['account.move'].search([('shopify_legacy_collection_transaction_id', '=', events[0]['id'])]))

    def test_identified_and_unidentified_existing_receipts_block_duplicate_clearing(self):
        for identified in (True, False):
            payout, wizard, payload, events = self._collection(str(identified))
            self._payment(events[0], identified=identified)
            with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
                wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')
            self.assertIn('payment may already record', wizard.line_ids.detail)

    def test_current_collection_and_current_fulfillment_are_not_historical_cash(self):
        for kind in ('collection', 'fulfillment'):
            payout, wizard, payload, events = self._collection(kind)
            timestamp = wizard.cutover_date.isoformat() + 'T10:00:00-04:00'
            if kind == 'collection':
                events[0]['processed_at'] = timestamp
            else:
                payload['fulfillments'][0]['created_at'] = timestamp
            with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
                wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')
            self.assertIn('on/after cutover' if kind == 'collection' else 'on or after cutover', wizard.line_ids.detail)

    def test_unidentified_receipt_dated_on_payout_also_blocks_duplicate_clearing(self):
        payout, wizard, payload, events = self._collection()
        self._payment(events[0], identified=False).date = payout.payout_date
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('payment may already record', wizard.line_ids.detail)

    def test_missing_transaction_identity_is_reviewed_without_fetching_an_unspecified_order(self):
        payout, wizard, payload, events = self._collection()
        payout.payout_transaction_ids.write({'source_order_id': False, 'source_order_transaction_id': False})
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=AssertionError('Unspecified order')):
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('Reimport the payout', wizard.line_ids.detail)

    def test_cutover_uses_business_timezone_for_late_night_collections(self):
        payout, wizard, payload, events = self._collection()
        events[0]['processed_at'] = wizard.cutover_date.isoformat() + 'T02:30:00Z'
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'ready')

    def test_exact_collection_identity_amount_currency_and_gateway_are_required(self):
        for field, value in (('id', 'other-capture'), ('amount', '200.00'), ('currency', 'ZZZ'), ('gateway', 'paypal')):
            payout, wizard, payload, events = self._collection(field)
            events[0][field] = value
            with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
                wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')
            self.assertFalse(payout.repair_audit_ids)

    def test_wrong_opening_account_and_missing_outstanding_configuration_need_review(self):
        payout, wizard, payload, events = self._collection()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.historical_collection_account_id = self.outstanding
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')
            wizard.historical_collection_account_id = self.journal.default_account_id
            self.journal.inbound_payment_method_line_ids.payment_account_id = False
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')

    def test_duplicate_payout_identity_cannot_clear_the_same_cash_twice(self):
        payout, wizard, payload, events = self._collection()
        payout.payout_transaction_ids.copy(dict(payout_id=payout.id, transaction_id='duplicate-balance'))
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.mapped('status'), ['review', 'review'])
        self.assertFalse(payout.repair_audit_ids)

    def test_reversed_or_altered_clearing_entry_cannot_be_reused(self):
        for operation in ('reverse', 'alter'):
            payout, wizard, payload, events = self._collection(operation)
            with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
                wizard.action_preview()
                wizard.action_apply()
                move = wizard.line_ids.collection_move_ids
                if operation == 'reverse':
                    reversal = move._reverse_moves([{'date': move.date}], cancel=False)
                    reversal.action_post()
                else:
                    move.button_draft()
                    move.line_ids.filtered(lambda row: row.account_id == self.journal.default_account_id).account_id = self.journal.suspense_account_id
                    move.action_post()
                wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')
            self.assertEqual(len(payout.repair_audit_ids), 1)

    def test_receipt_matched_to_another_statement_is_not_reallocated(self):
        payout, wizard, payload, events = self._collection()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            wizard.action_apply()
            line = wizard.line_ids.collection_move_ids.line_ids.filtered(lambda row: row.account_id == self.outstanding)
            other = self.env['account.bank.statement.line'].create(dict(date=payout.payout_date,
                amount=payout.amount, journal_id=self.journal.id, payment_ref='other deposit'))
            with self._matcher(payout):
                payout.shopify_reconcile_bank_statement_line_ept(other.id, line.ids)
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('allocated outside', wizard.line_ids.detail)
        self.assertTrue(other.is_reconciled)

    def test_unreviewed_current_collection_remains_open_alongside_ready_historical_receipt(self):
        payout, wizard, payload, events = self._collection()
        current_payload, current_events = deepcopy(payload), deepcopy(events)
        current_payload['id'] = current_events[0]['order_id'] = 'new-order'
        current_events[0]['id'] = 'new-capture'
        current_events[0]['processed_at'] = wizard.cutover_date.isoformat() + 'T10:00:00-04:00'
        self.env['shopify.payout.report.line.ept'].create(dict(payout_id=payout.id, transaction_type='charge',
            transaction_id='new-balance', source_order_id='new-order', source_order_transaction_id='new-capture', amount=287.83))
        sources = {payload['id']: (payload, events), 'new-order': (current_payload, current_events)}
        wizard.rematch_payouts = True
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', lambda row: sources[row.source_order_id]), \
                self._matcher(payout), patch.object(type(payout), 'get_invoices_for_reconcile', side_effect=AssertionError('Unreviewed import')):
            wizard.action_preview()
            wizard.action_apply()
        self.assertEqual(wizard.line_ids.mapped('status'), ['done', 'review'])
        self.assertEqual(payout.state, 'partially_processed')
        self.assertEqual(len(payout.payout_statement_line_ids.filtered('is_reconciled')), 1)

    def test_later_order_import_cannot_create_customer_cash_again(self):
        payout, wizard, payload, events = self._collection()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            wizard.action_apply()
        order = self.env['sale.order'].create(dict(partner_id=self.partner.id,
            shopify_instance_id=self.instance.id, shopify_order_id=payload['id']))
        with self.assertRaisesRegex(UserError, 'cleared from opening balances'):
            order._build_shopify_cash_plan(repair=True)

    def _evidence_csv(self, payout, wizard, events, **changes):
        import base64
        import csv
        import io
        row = dict(order_id=payout.payout_transaction_ids.source_order_id, transaction_id=events[0]['id'],
            document_type='invoice', document_number='5134960', status='PAID', currency=payout.currency_id.name,
            document_amount='287.83', allocated_amount='287.83',
            document_date=str(wizard.cutover_date - timedelta(days=2)), allocation_reference='application-1')
        row.update(changes)
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
        return base64.b64encode(stream.getvalue().encode())

    def test_a2000_evidence_attaches_exact_paid_allocation_without_accounting_and_enriches_preview(self):
        payout, workspace, payload, events = self._collection('evidence')
        wizard = self.env['shopify.opening.evidence.import.ept'].create(dict(repair_id=workspace.id,
            csv_file=self._evidence_csv(payout, workspace, events), reviewed=True))
        before = self.env['account.move'].search_count([])
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            wizard.action_apply()
            workspace.action_preview()
        evidence = self.env['shopify.opening.evidence.ept'].search([('transaction_id', '=', events[0]['id'])])
        self.assertEqual(evidence.document_number, '5134960')
        self.assertTrue(evidence.attachment_id)
        self.assertEqual(workspace.line_ids.plan['a2000_evidence']['id'], evidence.id)
        self.assertEqual(self.env['account.move'].search_count([]), before)
        with self.assertRaises(UserError):
            evidence.write({'allocated_amount': 1})

    def test_a2000_paid_header_does_not_replace_exact_cash_allocation_or_document_kind(self):
        payout, workspace, payload, events = self._collection('bad-evidence')
        wizard = self.env['shopify.opening.evidence.import.ept'].create(dict(repair_id=workspace.id,
            csv_file=self._evidence_csv(payout, workspace, events, document_amount='735', allocated_amount='735'), reviewed=True))
        with self.assertRaisesRegex(UserError, 'exact cash amount'):
            wizard.action_preview()
        wizard.csv_file = self._evidence_csv(payout, workspace, events, document_type='credit_memo')
        with self.assertRaises(UserError):
            wizard.action_preview()

    def test_opening_evidence_action_opens_empty_upload_form(self):
        payout, workspace, payload, events = self._collection('upload-form')
        action = workspace.action_import_opening_evidence()
        importer = self.env['shopify.opening.evidence.import.ept'].browse(action['res_id'])
        self.assertEqual(importer.repair_id, workspace)
        self.assertFalse(importer.csv_file)
        importer.reviewed = True
        with self.assertRaisesRegex(UserError, 'Upload the reviewed'):
            importer.action_preview()

    def test_changed_a2000_attachment_blocks_cash_repair(self):
        import base64
        payout, workspace, payload, events = self._collection('changed-file')
        importer = self.env['shopify.opening.evidence.import.ept'].create(dict(repair_id=workspace.id,
            csv_file=self._evidence_csv(payout, workspace, events), reviewed=True))
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            importer.action_preview()
            importer.action_apply()
            evidence = self.env['shopify.opening.evidence.ept'].search([('transaction_id', '=', events[0]['id'])])
            evidence.attachment_id.datas = base64.b64encode(b'changed')
            workspace.action_preview()
        self.assertEqual(workspace.line_ids.status, 'review')
        self.assertIn('evidence file changed', workspace.line_ids.detail)
