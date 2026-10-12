"""Backlog selection, exact dependencies and evidence on blocked cases."""
import importlib.util
import sys
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

package = types.ModuleType('shopify_review_sources')
package.__path__ = [str(Path(__file__).resolve().parents[1] / 'models')]
sys.modules.setdefault(package.__name__, package)
spec = importlib.util.spec_from_file_location(package.__name__ + '.shopify_repair_evidence',
    Path(package.__path__[0]) / 'shopify_repair_evidence.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)

try:
    from .test_shopify_payout_repair import TestShopifyPayoutRepair as RepairFixtures
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from .test_shopify_historical_collection import TestShopifyHistoricalCollection as CollectionFixtures
    from .test_shopify_opening_refund import TestShopifyOpeningRefund as RefundFixtures
except ImportError:
    from test_shopify_payout_repair import TestShopifyPayoutRepair as RepairFixtures
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from test_shopify_historical_collection import TestShopifyHistoricalCollection as CollectionFixtures
    from test_shopify_opening_refund import TestShopifyOpeningRefund as RefundFixtures
if ODOO_AVAILABLE:
    from odoo import Command
    from odoo.exceptions import UserError, AccessError


class TestShopifySourceReview(unittest.TestCase):
    def setUp(self):
        self.payload = dict(id='order', line_items=[dict(id='item', quantity=2)],
            fulfillments=[dict(id='shipment', status='success', created_at='2026-07-31T23:30:00-04:00',
                               line_items=[dict(id='item', quantity=1)])])
        self.events = [dict(id='charge', order_id='order', kind='sale', status='success',
            currency='USD', gateway='shopify_payments', amount='240', processed_at='2026-07-30T12:00:00-04:00')]

    def review(self):
        return evidence.source_review(self.payload, self.events, 'order', 'USD', '2026-08-01', 'America/New_York')

    def test_partial_fulfillment_shows_exact_missing_quantity_without_passing_proof(self):
        result = self.review()
        self.assertEqual(result['items'][0]['missing'], '1')
        self.assertIn('cannot be proven', result['fulfillment_result'])
        self.assertEqual(result['net'], '240')

    def test_business_timezone_and_failed_fulfillment_do_not_count_as_historical_shipping(self):
        self.payload['fulfillments'][0]['created_at'] = '2026-08-01T04:05:00Z'
        result = self.review()
        self.assertFalse(result['fulfillment'][0]['before_cutover'])
        self.assertEqual(result['items'][0]['fulfilled'], '0')
        self.assertIn('on or after cutover', result['fulfillment_result'])
        self.payload['fulfillments'][0].update(status='failure', created_at='2026-07-31T12:00:00Z')
        self.assertEqual(self.review()['items'][0]['fulfilled'], '0')

    def test_invalid_or_wrong_order_history_has_no_trusted_cash_totals(self):
        self.events[0]['currency'] = 'EUR'
        self.assertNotIn('net', self.review())
        self.assertIn('currency', self.review()['source_issue'])
        self.payload['id'] = 'different'
        self.assertNotIn('gross', self.review())

    def test_refunds_and_complete_fulfillment_keep_separate_cash_totals(self):
        self.payload['fulfillments'][0]['line_items'][0]['quantity'] = 2
        self.events.append(dict(self.events[0], id='refund', parent_id='charge', kind='refund', amount='40'))
        result = self.review()
        self.assertEqual((result['gross'], result['refunds'], result['net']), ('240', '40', '200'))
        self.assertEqual(result['items'][0]['missing'], '0')
        self.assertIn('is proven', result['fulfillment_result'])


class TestShopifyRepairWorkflow(PayoutTestCase):
    _historical = RepairFixtures._historical
    _fixture = RepairFixtures._fixture
    _collection = CollectionFixtures._collection
    _refund = RefundFixtures._refund
    _matcher = CollectionFixtures._matcher
    _setup_repair_data = classmethod(RepairFixtures._setup_repair_data.__func__)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_repair_data()

    def test_pilot_selection_clears_hidden_cases_caps_at_ten_and_never_reads_source(self):
        payout, workspace, payload, events = self._historical('select')
        cases = self.env['shopify.payout.repair.line.ept'].create([
            dict(wizard_id=workspace.id, transaction_ids=[Command.set(payout.payout_transaction_ids.ids)],
                 status='ready', kind='historical', resolution='credit' if n == 12 else 'review', selected=True)
            for n in range(13)])
        review = self.env['shopify.payout.repair.line.ept'].create(dict(wizard_id=workspace.id,
            transaction_ids=[Command.set(payout.payout_transaction_ids.ids)], status='review', resolution='documents'))
        workspace.queue_filter = 'ready'
        workspace.issue_filter = 'review'
        before = self.env['account.move'].search_count([])
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=AssertionError('Unexpected read')):
            workspace.preview_settings = workspace._settings()
            with self.assertRaisesRegex(UserError, 'hidden by the current filters'):
                workspace.action_apply()
            workspace.action_select_pilot()
            self.assertEqual(workspace.line_ids.filtered('selected'), cases[:10])
            self.assertEqual(workspace.visible_line_ids, cases[:12])
            self.assertFalse(cases[12].selected)
            workspace.action_select_ready()
            self.assertEqual(workspace.line_ids.filtered('selected'), cases[:12])
            workspace.action_clear_selection()
            self.assertFalse(any(workspace.line_ids.mapped('selected')))
        self.assertEqual(self.env['account.move'].search_count([]), before)
        self.assertEqual(len(workspace.line_ids), 14)
        workspace.queue_filter, workspace.issue_filter = 'review', 'all'
        self.assertEqual(workspace.visible_line_ids, review)
        # The filtered x2many still writes the same saved child, without replacing the queue.
        workspace.write({'visible_line_ids': [Command.update(review.id, {'selected': False})]})
        self.assertEqual(len(workspace.line_ids), 14)

    def test_matched_cases_are_excluded_from_selection_and_apply_even_if_saved_ready(self):
        payout, workspace, payload, events = self._historical('matched-selection')
        payout.payout_transaction_ids.amount = 0
        workspace.line_ids = [Command.create(dict(transaction_ids=[Command.set(payout.payout_transaction_ids.ids)],
            status='ready', selected=True, kind='historical'))]
        # Test the live status branch without any source/API or accounting mutation.
        with patch.object(type(workspace.line_ids), '_compute_reconciliation_status',
            lambda rows: [setattr(row, 'reconciliation_status', 'matched') for row in rows]):
            workspace.line_ids.invalidate_recordset(['reconciliation_status'])
            workspace.action_select_ready()
            self.assertFalse(workspace.line_ids.selected)
            workspace.line_ids.selected = True
            workspace.preview_settings = workspace._settings()
            before = self.env['account.move'].search_count([])
            workspace.action_apply()
            self.assertEqual(self.env['account.move'].search_count([]), before)
            self.assertIn('already matched', workspace.result)

    def test_shop_cash_shortcuts_target_the_named_dependency_and_only_open_existing_import(self):
        payout, workspace, payload, events = self._historical('dependency')
        self.instance.shopify_host = 'review.myshopify.com'
        transaction = payout.payout_transaction_ids
        transaction.write(dict(transaction_type='credit', adjustment_reason='shop_cash',
            shop_cash_allocations=[dict(order_id='missing-dependency', transaction_id='dep-capture', amount='20')]))
        before = self.env['sale.order'].search_count([])
        with patch.object(type(transaction), '_repair_source', side_effect=AssertionError('Wrong order read')):
            workspace.action_preview()
            case = workspace.line_ids
            self.assertEqual(case.resolution, 'shop_cash')
            self.assertFalse(case.can_import_customer)
            self.assertEqual(case.dependency_order_id, 'missing-dependency')
            self.assertTrue(case.action_open_dependency_order()['url'].endswith('/missing-dependency'))
            action = case.action_review_dependency_import()
            self.assertEqual(action['res_model'], 'shopify.process.import.export')
            self.assertEqual(action['context']['default_shopify_order_ids'], 'missing-dependency')
            self.assertEqual(action['context']['default_shopify_instance_id'], self.instance.id)
        self.assertEqual(self.env['sale.order'].search_count([]), before)
        transaction.shop_cash_allocations = [dict(order_id='changed-dependency', amount='20')]
        with self.assertRaisesRegex(UserError, 'Refresh this case'):
            case.action_review_dependency_import()

    def test_unrelated_source_blocker_never_offers_customer_import(self):
        payout, workspace, payload, events = self._historical('fulfillment')
        payload['fulfillments'] = []
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            workspace.action_preview()
        case = workspace.line_ids
        self.assertEqual(case.resolution, 'source')
        self.assertFalse(case.can_import_customer)
        self.assertEqual(case.review_evidence['items'][0]['missing'], '1')
        self.assertIn('Unproven', case.evidence_html)
        with self.assertRaisesRegex(UserError, 'unmatched refund case'):
            case.action_import_customer()

    def test_blocked_invoice_preview_retains_comparison_once_and_does_not_create_accounting(self):
        order, invoice, payload, events = self._fixture()
        invoice.button_draft()
        payout, workspace, old_payload, old_events = self._historical('documents')
        payout.payout_transaction_ids.write(dict(transaction_type='charge', source_order_id=order.shopify_order_id,
            source_order_transaction_id=events[0]['id'], amount=float(events[0]['amount'])))
        before = self.env['account.move'].search_count([])
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)) as source:
            workspace.action_preview()
        self.assertEqual(source.call_count, 1)
        case = workspace.line_ids
        self.assertEqual(case.resolution, 'documents')
        self.assertEqual(case.status, 'review')
        self.assertFalse(case.selected)
        self.assertEqual(case.review_evidence['gross'], '242.10')
        self.assertEqual(case.review_evidence['refunds'], '45.00')
        self.assertIn('Documents minus Shopify net cash', case.evidence_html)
        self.assertIn('draft', case.evidence_html)
        self.assertEqual(payout.repair_history_ids.review_evidence, case.review_evidence)
        self.assertEqual(self.env['account.move'].search_count([]), before)
        poisoned = deepcopy(case.review_evidence)
        poisoned['documents'][0]['name'] = '<script>alert(1)</script>'
        case.review_evidence = poisoned
        self.assertNotIn('<script>', case.evidence_html)
        self.assertIn('&lt;script&gt;', case.evidence_html)
        self.assertEqual(invoice.state, 'draft')

    def test_charge_fulfillment_blocker_uses_same_typed_source_guidance(self):
        payout, workspace, payload, events = self._historical('charge-proof')
        payout.payout_transaction_ids.write(dict(transaction_type='charge', amount=float(events[0]['amount']),
            source_order_transaction_id=events[0]['id']))
        payload['fulfillments'] = []
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            workspace.action_preview()
        self.assertEqual(workspace.line_ids.resolution, 'source')
        self.assertIn('Open Shopify', workspace.line_ids.next_step)
        self.assertFalse(workspace.line_ids.can_import_customer)

    def test_selection_controls_require_manager_before_changes(self):
        payout, workspace, payload, events = self._historical('permission')
        reader = self.env['res.users'].with_context(no_reset_password=True).create(dict(name='Workflow Reader',
            login='workflow-reader', company_id=self.env.company.id, company_ids=[Command.set(self.env.company.ids)],
            groups_id=[Command.set([self.env.ref('base.group_user').id, self.env.ref('account.group_account_user').id])]))
        with self.assertRaises(AccessError):
            workspace.with_user(reader).action_select_pilot()

    def test_ten_case_pilot_matches_five_collections_and_five_refunds_beside_a_blocker(self):
        sources = {}
        payout, workspace = None, None
        for n in range(10):
            current, current_workspace, payload, events = (self._collection if n < 5 else self._refund)('pilot-%s' % n)
            sources[payload['id']] = (payload, events)
            if not payout:
                payout, workspace = current, current_workspace
            else:
                current.payout_transaction_ids.write({'payout_id': payout.id})
        blocked = self.env['shopify.payout.report.line.ept'].create(dict(payout_id=payout.id,
            transaction_type='charge', transaction_id='pilot-blocked-balance', source_order_id='pilot-blocked-order',
            source_order_transaction_id='pilot-blocked-capture', amount=100))
        payout.amount = sum(payout.payout_transaction_ids.mapped('amount'))
        workspace.rematch_payouts = True
        payout.generate_bank_statement()
        baseline = {model: self.env[model].search_count([])
                    for model in ('sale.order', 'account.payment', 'stock.picking')}
        credits = self.env['account.move'].search_count([('move_type', '=', 'out_refund')])
        def read_source(transaction):
            if transaction == blocked:
                raise UserError('The remaining order needs source review.')
            return sources[transaction.source_order_id]
        with patch.object(type(blocked), '_repair_source', read_source), self._matcher(payout):
            workspace.action_preview()
            self.assertEqual(len(workspace.line_ids.filtered(lambda row: row.status == 'ready')), 10)
            workspace.queue_filter = 'ready'
            workspace.action_select_pilot()
            self.assertEqual(len(workspace.line_ids.filtered('selected')), 10)
            workspace.action_apply()
            completed = workspace.line_ids.filtered(lambda row: row.status == 'done')
            self.assertEqual(len(completed), 10, workspace.result)
            self.assertEqual(set(completed.mapped('reconciliation_status')), {'matched'}, workspace.result)
            self.assertEqual(set(completed.mapped('kind')), {'collection', 'opening_refund'})
            self.assertEqual(len(completed.collection_move_ids), 10)
            self.assertEqual(set(completed.collection_move_ids.mapped('state')), {'posted'})
            self.assertEqual(workspace.line_ids.filtered(lambda row: blocked in row.transaction_ids).status, 'review')
            self.assertNotEqual(payout.state, 'validated')
            self.assertFalse(payout.settlement_move_id)
            moves = self.env['account.move'].search_count([])
            workspace.action_preview()
            self.assertEqual(self.env['account.move'].search_count([]), moves)
            self.assertEqual(len(completed.collection_move_ids), 10)
        for model, count in baseline.items():
            self.assertEqual(self.env[model].search_count([]), count)
        self.assertEqual(self.env['account.move'].search_count([('move_type', '=', 'out_refund')]), credits)
