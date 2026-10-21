"""Refunds already recorded in migration must clear cash without new returns."""
from datetime import timedelta
from unittest.mock import patch

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from .test_shopify_historical_collection import TestShopifyHistoricalCollection as CollectionFixtures
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from test_shopify_historical_collection import TestShopifyHistoricalCollection as CollectionFixtures
if ODOO_AVAILABLE:
    from odoo import Command
    from odoo.exceptions import UserError


class TestShopifyOpeningRefund(PayoutTestCase):
    _collection = CollectionFixtures._collection
    _matcher = CollectionFixtures._matcher
    _evidence_csv = CollectionFixtures._evidence_csv

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref('account.group_account_manager')
        cls.outstanding, cls.outgoing = cls.env['account.account'].create([
            dict(name=name, code=code, account_type='asset_current', reconcile=True,
                 company_ids=[Command.set(cls.env.company.ids)])
            for name, code in [('Opening Receipts', 'OBREC'), ('Opening Refunds', 'OBPAY')]])
        cls.journal.inbound_payment_method_line_ids.payment_account_id = cls.outstanding
        cls.journal.outbound_payment_method_line_ids.payment_account_id = cls.outgoing
        cls.partner = cls.env['res.partner'].create(dict(name='Opening Refund Customer'))
        cls.env['shopify.res.partner.ept'].create(dict(partner_id=cls.partner.id,
            shopify_instance_id=cls.instance.id, shopify_customer_id='historical-customer'))

    def _refund(self, suffix='one', amount=283.30):
        payout, wizard, payload, events = self._collection(suffix)
        # Capture and refund precede cutover; only the payout deduction is later.
        events[0]['amount'] = '1006.22'
        events[0]['processed_at'] = (wizard.cutover_date - timedelta(days=5)).isoformat() + 'T10:00:00-04:00'
        payload['fulfillments'][0]['created_at'] = events[0]['processed_at']
        events.append(dict(events[0], id='old-refund-' + suffix, kind='refund', amount=str(amount),
            parent_id=events[0]['id'],
            processed_at=(wizard.cutover_date - timedelta(days=1)).isoformat() + 'T14:23:00-04:00'))
        payload['refunds'] = [dict(id='old-refund-doc-' + suffix,
                                  transactions=[dict(id=events[1]['id'])])]
        payout.payout_transaction_ids.write(dict(transaction_type='refund', amount=-amount,
            source_order_transaction_id=events[1]['id']))
        payout.amount = -amount
        wizard.rematch_payouts = True
        return payout, wizard, payload, events

    def _attach_refund_evidence(self, payout, wizard, events):
        importer = self.env['shopify.opening.evidence.import.ept'].create(dict(
            repair_id=wizard.id, reviewed=True,
            csv_file=self._evidence_csv(payout, wizard, events[1:],
                document_type='credit_memo', document_number='5001234',
                document_amount='310.20', allocated_amount='283.30')))
        importer.action_preview()
        importer.action_apply()
        return self.env['shopify.opening.evidence.ept'].search([
            ('instance_id', '=', self.instance.id), ('transaction_id', '=', events[1]['id'])])

    def test_attached_a2000_refund_allocation_previews_applies_and_reuses_exact_match(self):
        payout, wizard, payload, events = self._refund('a2000')
        payout.generate_bank_statement()
        counts = {model: self.env[model].search_count([])
                  for model in ('account.move', 'account.payment', 'sale.order', 'stock.picking')}
        credits = self.env['account.move'].search_count([('move_type', '=', 'out_refund')])
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            evidence = self._attach_refund_evidence(payout, wizard, events)
            wizard.action_preview()
            case = wizard.line_ids
            self.assertEqual(case.status, 'ready', case.detail)
            self.assertEqual(case.kind, 'opening_refund')
            self.assertEqual(case.opening_evidence_id, evidence)
            self.assertEqual(case.plan['a2000_evidence']['id'], evidence.id)
            self.assertEqual(case.plan['a2000_evidence']['allocation'], 'application-1')
            self.assertEqual(evidence.document_amount, 310.20)
            self.assertEqual(evidence.allocated_amount, 283.30)
            for model, count in counts.items():
                self.assertEqual(self.env[model].search_count([]), count)
            wizard.action_apply()
            self.assertEqual(case.status, 'done', case.detail)
            self.assertEqual(case.reconciliation_status, 'matched')
            move = case.collection_move_ids
            self.assertEqual(move.state, 'posted')
            self.assertEqual(move.date, payout.payout_date)
            self.assertEqual(move.line_ids.filtered(lambda line: line.account_id == self.outgoing).balance, -283.30)
            self.assertTrue(payout.payout_statement_line_ids.is_reconciled)
            self.assertEqual(payout.state, 'validated')
            self.assertEqual(len(payout.repair_audit_ids), 1)
            self.assertEqual(payout.repair_audit_ids.plan['a2000_evidence']['id'], evidence.id)
            wizard.open_only = False
            wizard.action_preview()
            wizard.action_apply()
            self.assertEqual(case.collection_move_ids, move)
            self.assertEqual(len(payout.repair_audit_ids), 1)
        for model in ('account.payment', 'sale.order', 'stock.picking'):
            self.assertEqual(self.env[model].search_count([]), counts[model])
        self.assertEqual(self.env['account.move'].search_count([('move_type', '=', 'out_refund')]), credits)

    def test_changed_a2000_refund_file_after_preview_blocks_accounting(self):
        import base64
        payout, wizard, payload, events = self._refund('a2000-file-changed')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            evidence = self._attach_refund_evidence(payout, wizard, events)
            wizard.action_preview()
            evidence.attachment_id.datas = base64.b64encode(b'changed after preview')
            wizard.action_apply()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('evidence file changed', wizard.line_ids.detail)
        self.assertFalse(wizard.line_ids.collection_move_ids)
        self.assertFalse(payout.repair_audit_ids)

    def test_changed_source_after_a2000_refund_preview_still_blocks_accounting(self):
        payout, wizard, payload, events = self._refund('a2000-source-changed')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            self._attach_refund_evidence(payout, wizard, events)
            wizard.action_preview()
            payload['note'] = 'changed after accounting review'
            wizard.action_apply()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('changed after preview', wizard.line_ids.detail)
        self.assertFalse(wizard.line_ids.collection_move_ids)
        self.assertFalse(payout.repair_audit_ids)

    def test_one_apply_posts_and_matches_opening_refund_without_repeating_return_or_cash(self):
        payout, wizard, payload, events = self._refund()
        payout.generate_bank_statement()
        statement = payout.payout_statement_line_ids
        bank = self.journal.default_account_id
        baseline = sum(self.env['account.move.line'].search([('account_id', '=', bank.id)]).mapped('balance'))
        counts = {model: self.env[model].search_count([])
                  for model in ('account.payment', 'sale.order', 'stock.picking')}
        credits = self.env['account.move'].search_count([('move_type', '=', 'out_refund')])
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.kind, 'opening_refund', wizard.line_ids.detail)
            self.assertEqual(wizard.line_ids.status, 'ready')
            self.assertIn(self.outgoing.display_name, wizard.line_ids.review_html)
            self.assertIn('no new credit note', wizard.line_ids.detail)
            wizard.action_apply()
        move = wizard.line_ids.collection_move_ids
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.date, payout.payout_date)
        self.assertEqual(move.line_ids.filtered(lambda line: line.account_id == bank).balance, 283.30)
        self.assertEqual(move.line_ids.filtered(lambda line: line.account_id == self.outgoing).balance, -283.30)
        self.assertTrue(move.line_ids.filtered(lambda line: line.account_id == self.outgoing).reconciled)
        self.assertTrue(statement.is_reconciled)
        self.assertEqual(payout.payout_statement_line_ids, statement)
        self.assertEqual(payout.state, 'validated')
        self.assertFalse(payout.reimport_reconciliation_issue)
        self.assertEqual(wizard.line_ids.reconciliation_status, 'matched')
        self.assertIn('1 applied case(s) fully matched', wizard.result)
        self.assertEqual(sum(self.env['account.move.line'].search([('account_id', '=', bank.id)]).mapped('balance')), baseline + 283.30)
        for model, count in counts.items():
            self.assertEqual(self.env[model].search_count([]), count)
        self.assertEqual(self.env['account.move'].search_count([('move_type', '=', 'out_refund')]), credits)

    def test_post_only_reports_pending_and_a_repeat_apply_reuses_and_finishes(self):
        payout, wizard, payload, events = self._refund()
        wizard.rematch_payouts = False
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            wizard.action_preview()
            wizard.action_apply()
            move = wizard.line_ids.collection_move_ids
            self.assertEqual(wizard.line_ids.reconciliation_status, 'pending')
            self.assertIn('1 still awaiting reconciliation', wizard.result)
            self.assertIn('matching was disabled', wizard.result)
            wizard.rematch_payouts = True
            wizard.action_preview()
            wizard.action_apply()
            self.assertEqual(wizard.line_ids.collection_move_ids, move)
            self.assertEqual(len(payout.repair_audit_ids), 1)
            self.assertEqual(wizard.line_ids.reconciliation_status, 'matched')
            wizard.open_only = False
            wizard.action_preview()
            wizard.action_apply()
            self.assertEqual(wizard.line_ids.collection_move_ids, move)
            self.assertEqual(len(payout.repair_audit_ids), 1)

    def test_regular_processing_matches_existing_refund_without_importing_order(self):
        payout, wizard, payload, events = self._refund()
        wizard.rematch_payouts = False
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            wizard.action_apply()
        payout.generate_bank_statement()
        with self._matcher(payout), patch.object(type(payout), 'get_invoices_for_reconcile', side_effect=AssertionError('Import')):
            payout.process_bank_statement()
        self.assertTrue(payout.payout_statement_line_ids.is_reconciled)

    def test_opening_balance_confirmation_and_outgoing_account_are_required(self):
        payout, wizard, payload, events = self._refund()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.clear_historical_collections = False
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')
            self.assertIn('already included in migration balances', wizard.line_ids.detail)
            wizard.clear_historical_collections = True
            self.journal.outbound_payment_method_line_ids.payment_account_id = False
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('outstanding receipts/payments', wizard.line_ids.detail)
        self.assertFalse(payout.repair_audit_ids)

    def test_existing_credit_or_refund_payment_blocks_duplicate_migration_accounting(self):
        for existing in ('credit', 'payment', 'unidentified'):
            payout, wizard, payload, events = self._refund(existing)
            if existing == 'credit':
                sales = self.env['account.journal'].create(dict(name='Opening Credit', code='OBSALE',
                    type='sale', company_id=self.env.company.id))
                self.env['account.move'].create(dict(move_type='out_refund', journal_id=sales.id,
                    partner_id=self.partner.id, shopify_instance_id=self.instance.id,
                    shopify_refund_id=payload['refunds'][0]['id']))
            else:
                self.env['account.payment'].create(dict(partner_id=self.partner.id, partner_type='customer',
                    payment_type='outbound', journal_id=self.journal.id,
                    payment_method_line_id=self.journal.outbound_payment_method_line_ids[:1].id,
                    amount=283.30, date=payout.payout_date, currency_id=payout.currency_id.id,
                    shopify_instance_id=self.instance.id if existing == 'payment' else False,
                    shopify_order_transaction_id=events[1]['id'] if existing == 'payment' else False))
            with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
                wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')
            self.assertFalse(payout.repair_audit_ids)

    def test_changed_source_and_closed_payout_leave_refund_unposted(self):
        payout, wizard, payload, events = self._refund()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            payload['note'] = 'changed after review'
            wizard.action_apply()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertFalse(payout.repair_audit_ids)
        with patch.object(type(payout), '_check_payout_reimport_period', side_effect=UserError('Period closed')):
            wizard.action_preview()
        self.assertIn('Period closed', wizard.line_ids.detail)

    def test_source_refund_is_classified_in_business_timezone_and_mixed_document_is_reviewed(self):
        payout, wizard, payload, events = self._refund()
        events[1]['processed_at'] = wizard.cutover_date.isoformat() + 'T02:30:00Z'
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.kind, 'opening_refund')
            events.append(dict(events[1], id='later-refund', amount='10',
                processed_at=wizard.cutover_date.isoformat() + 'T10:00:00-04:00'))
            payload['refunds'][0]['transactions'].append(dict(id='later-refund'))
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.status, 'review')
        self.assertIn('mixes cash transactions', wizard.line_ids.detail)

    def test_duplicate_refund_payout_rows_and_missing_refund_documents_are_reviewed(self):
        payout, wizard, payload, events = self._refund()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            payload['refunds'] = []
            wizard.action_preview()
            self.assertIn('one source refund document', wizard.line_ids.detail)
            payload['refunds'] = [dict(id='restored-document', transactions=[dict(id=events[1]['id'])])]
            payout.payout_transaction_ids.copy(dict(transaction_id='duplicate-refund-balance', transaction_type='payment_refund'))
            wizard.action_preview()
        self.assertEqual(wizard.line_ids.mapped('status'), ['review', 'review'])
        self.assertFalse(payout.repair_audit_ids)

    def test_matching_failure_is_visible_and_does_not_roll_back_other_ready_cases(self):
        payout, wizard, payload, events = self._refund('failed-match')
        other, _wizard, other_payload, other_events = self._refund('matched')
        other.payout_transaction_ids.payout_id = payout
        sources = {payload['id']: (payload, events), other_payload['id']: (other_payload, other_events)}
        def match(model, statement_id, line_ids):
            statement = self.env['account.bank.statement.line'].browse(statement_id)
            if statement.payout_line_id.source_order_id == payload['id']:
                raise UserError('Exact refund match failed')
            return adapter(model, statement_id, line_ids)
        # Capture whichever adapter the runtime provides: real widget or real AML reconciliation.
        with self._matcher(payout):
            adapter = type(payout).shopify_reconcile_bank_statement_line_ept
            with patch.object(type(payout), 'shopify_reconcile_bank_statement_line_ept', match), \
                    patch.object(type(payout.payout_transaction_ids), '_repair_source', lambda row: sources[row.source_order_id]):
                wizard.action_preview()
                wizard.action_apply()
        self.assertEqual(len(wizard.line_ids.collection_move_ids), 2)
        self.assertEqual(len(payout.payout_statement_line_ids.filtered('is_reconciled')), 1)
        self.assertEqual(payout.state, 'partially_processed')
        self.assertIn('Exact refund match failed', wizard.result)
        self.assertIn('1 still awaiting reconciliation', wizard.result)
        self.assertIn('Exact refund match failed', payout.reimport_reconciliation_issue)

    def test_four_refunds_complete_together_preserving_existing_charge_matches(self):
        payout, wizard, payload, events = self._refund('first', 283.30)
        sources = {payload['id']: (payload, events)}
        for suffix, amount in [('second', 279.80), ('third', 277.24), ('fourth', 248.72)]:
            other, _wizard, other_payload, other_events = self._refund(suffix, amount)
            other.payout_transaction_ids.payout_id = payout
            sources[other_payload['id']] = (other_payload, other_events)
        receipt, _wizard, receipt_payload, receipt_events = self._collection('existing-charge')
        receipt.payout_transaction_ids.payout_id = payout
        sources[receipt_payload['id']] = (receipt_payload, receipt_events)
        payout.amount = 287.83 - 1089.06
        payout.generate_bank_statement()
        statements = payout.payout_statement_line_ids
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', lambda row: sources[row.source_order_id]), self._matcher(payout):
            wizard.action_preview()
            wizard.line_ids.filtered(lambda line: line.kind == 'opening_refund').selected = False
            wizard.action_apply()
            matched_receipt = statements.filtered(lambda line: line.shopify_transaction_type == 'charge')
            self.assertTrue(matched_receipt.is_reconciled)
            original_entry = matched_receipt.move_id
            original_lines = original_entry.line_ids
            original_matches = original_lines.matched_debit_ids | original_lines.matched_credit_ids
            wizard.action_preview()
            self.assertEqual(len(wizard.line_ids), 5)
            self.assertEqual(len(wizard.line_ids.filtered(lambda line: line.status == 'ready')), 4)
            self.assertEqual(wizard.line_ids.filtered(lambda line: line.kind == 'collection').reconciliation_status, 'matched')
            wizard.action_apply()
        self.assertTrue(all(statements.mapped('is_reconciled')))
        self.assertEqual(payout.payout_statement_line_ids, statements)
        self.assertEqual(payout.state, 'validated')
        self.assertEqual(matched_receipt.move_id, original_entry)
        self.assertEqual(original_entry.line_ids, original_lines)
        self.assertEqual(original_lines.matched_debit_ids | original_lines.matched_credit_ids, original_matches)
        self.assertIn('4 applied case(s) fully matched', wizard.result)
        self.assertAlmostEqual(sum(wizard.line_ids.collection_move_ids.line_ids.filtered(
            lambda line: line.account_id == self.outgoing).mapped('balance')), -1089.06)

    def test_final_payout_validation_failure_keeps_exact_refund_matches(self):
        payout, wizard, payload, events = self._refund()
        payout.amount = -280
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            wizard.action_preview()
            wizard.action_apply()
        self.assertTrue(payout.payout_statement_line_ids.is_reconciled)
        self.assertTrue(wizard.line_ids.collection_move_ids.line_ids.filtered(
            lambda line: line.account_id == self.outgoing).reconciled)
        self.assertEqual(wizard.line_ids.reconciliation_status, 'matched')
        self.assertEqual(payout.state, 'partially_processed')
        self.assertIn('does not balance', payout.reimport_reconciliation_issue)
        self.assertIn('does not balance', wizard.result)

    def test_pre_cutover_source_period_can_be_closed_but_payout_must_stay_open(self):
        payout, wizard, payload, events = self._refund()
        self.env.company.fiscalyear_lock_date = wizard.cutover_date - timedelta(days=1)
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'ready')
            wizard.action_apply()
        self.assertEqual(wizard.line_ids.collection_move_ids.date, payout.payout_date)
        self.assertTrue(payout.payout_statement_line_ids.is_reconciled)

    def test_mismatched_refund_proof_or_missing_identity_never_creates_clearing(self):
        for field, value in [('amount', '15'), ('gateway', 'paypal'), ('parent_id', 'missing-parent')]:
            payout, wizard, payload, events = self._refund(field)
            events[1][field] = value
            with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
                wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'review')
            self.assertFalse(payout.repair_audit_ids)
        payout.payout_transaction_ids.source_order_transaction_id = False
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=AssertionError('Missing identity')):
            wizard.action_preview()
        self.assertIn('Reimport the payout', wizard.line_ids.detail)


del CollectionFixtures
