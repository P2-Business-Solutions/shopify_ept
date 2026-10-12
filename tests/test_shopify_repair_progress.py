"""Saved payout workspaces, per-case refresh, navigation and permanent history."""
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import patch
try:
    from .test_shopify_payout_repair import TestShopifyPayoutRepair as RepairFixtures
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
except ImportError:
    from test_shopify_payout_repair import TestShopifyPayoutRepair as RepairFixtures
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
if ODOO_AVAILABLE:
    from odoo import Command, fields
    from odoo.exceptions import UserError, AccessError


class TestShopifyRepairProgress(PayoutTestCase):
    _historical = RepairFixtures._historical
    _fixture = RepairFixtures._fixture
    _legacy = RepairFixtures._legacy
    _setup_repair_data = classmethod(RepairFixtures._setup_repair_data.__func__)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_repair_data()

    def test_reopening_payout_resumes_saved_results_without_shopify_reads(self):
        payout, wizard, payload, events = self._historical('resume')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        case = wizard.line_ids
        case.selected = False
        before = (case.detail, case.plan, wizard.result, wizard.cutover_date, case.last_preview_at)
        self.assertFalse(wizard.is_transient())
        self.assertFalse(case.is_transient())
        self.env.cr.execute('UPDATE shopify_payout_repair_ept SET create_date = %s WHERE id = %s',
                            [fields.Datetime.now() - timedelta(days=30), wizard.id])
        self.env.invalidate_all()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=AssertionError('Re-read')):
            action = payout.action_bulk_repair_transactions()
        self.assertEqual(action['res_id'], wizard.id)
        self.assertEqual(wizard.line_ids, case)
        self.assertFalse(case.selected)
        self.assertEqual((case.detail, case.plan, wizard.result, wizard.cutover_date, case.last_preview_at), before)
        self.assertEqual(payout.repair_case_ids, case)
        self.assertEqual(payout.repair_history_ids.operation, 'preview')

    def test_statement_action_opens_same_proposed_fix_and_preserves_saved_settings(self):
        payout, workspace, payload, events = self._historical('statement-exceptions')
        workspace.write({'queue_filter': 'matched', 'issue_filter': 'payments'})
        settings = workspace._settings()
        accounting_count = self.env['account.move'].search_count([])
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            action = payout.action_generate_bank_statement()
        self.assertEqual(action['res_id'], workspace.id)
        self.assertEqual(workspace._settings(), settings)
        self.assertEqual(workspace.queue_filter, 'pending')
        self.assertEqual(workspace.issue_filter, 'all')
        self.assertEqual(workspace.line_ids.status, 'ready', workspace.line_ids.detail)
        self.assertEqual(workspace.line_ids.kind, 'historical')
        self.assertTrue(workspace.line_ids.plan)
        self.assertFalse(workspace.line_ids.payment_ids)
        self.assertFalse(workspace.line_ids.credit_ids)
        # Only the cash statement is created. The proposed credit/payment remains a preview.
        self.assertEqual(self.env['account.move'].search_count([]), accounting_count + 1)
        self.assertEqual(payout.repair_case_ids, workspace.line_ids)
        self.assertIn('retained', workspace.result)

    def test_statement_action_keeps_accounting_if_preview_fails(self):
        payout, workspace, payload, events = self._historical('preview-failure')
        with patch.object(type(workspace), 'action_preview', side_effect=UserError('Preview unavailable')):
            action = payout.action_generate_bank_statement()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(payout.state, 'generated')
        self.assertEqual(len(payout.payout_statement_line_ids), 1)
        self.assertIn('Preview unavailable', payout.reimport_reconciliation_issue)

    def test_refresh_preserves_case_identity_and_prior_issue_history(self):
        payout, wizard, payload, events = self._historical('issue-history')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=UserError('Missing product mapping')):
            wizard.action_preview()
        case = wizard.line_ids
        old_event = payout.repair_history_ids
        self.assertEqual(case.status, 'review')
        self.assertEqual(old_event.detail, 'Missing product mapping')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            case.action_refresh_case()
        self.assertEqual(wizard.line_ids, case)
        self.assertEqual(case.status, 'ready')
        self.assertEqual(old_event.detail, 'Missing product mapping')
        self.assertEqual(len(payout.repair_history_ids), 2)

    def test_guidance_uses_typed_routes_and_unknown_errors_keep_manual_review(self):
        try:
            from ..models.shopify_repair_error import ShopifyRepairBlocked
        except ImportError:
            self.skipTest('Odoo runtime required')
        payout, workspace, payload, events = self._historical('typed-guidance')
        for route, phrase in [('documents', 'Invoices / Credits'), ('payments', 'Review Payments'),
                              ('credit', 'refund allocation'), ('source', 'Open Shopify'),
                              ('customer', 'Import Customer')]:
            with patch.object(type(payout.payout_transaction_ids), '_repair_source',
                              side_effect=ShopifyRepairBlocked('Translated source message', route)):
                workspace.action_preview()
            self.assertEqual(workspace.line_ids.resolution, route)
            self.assertIn(phrase, workspace.line_ids.next_step)
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=UserError('Unexpected source issue')):
            workspace.action_preview()
        self.assertEqual(workspace.line_ids.resolution, 'review')
        self.assertIn('Review Details', workspace.line_ids.next_step)

    def test_actual_fulfillment_blocker_has_source_guidance(self):
        payout, workspace, payload, events = self._historical('fulfillment-guidance')
        payload['fulfillments'] = []
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            workspace.action_preview()
        self.assertEqual(workspace.line_ids.resolution, 'source')
        self.assertIn('Open Shopify', workspace.line_ids.next_step)

    def test_payment_shortcut_rejects_missing_order_before_cash_source_read(self):
        payout, workspace, payload, events = self._historical('missing-order-payment')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            workspace.action_preview()
        with patch.object(type(self.env['sale.order']), '_shopify_cash_source', side_effect=AssertionError('Unexpected read')):
            with self.assertRaises(UserError):
                workspace.line_ids.action_review_payments()

    def test_refresh_case_does_not_rerun_unrelated_orders(self):
        payout, wizard, payload, events = self._historical('individual')
        other = self.env['shopify.payout.report.line.ept'].create(dict(payout_id=payout.id,
            transaction_type='charge', transaction_id='other-balance', source_order_id='other-order', amount=100))
        original = payout.payout_transaction_ids - other
        def source(transaction):
            if transaction == other:
                raise UserError('Other case needs an order')
            return payload, events
        with patch.object(type(original), '_repair_source', source):
            wizard.action_preview()
        case = wizard.line_ids.filtered(lambda line: original in line.transaction_ids)
        unrelated = wizard.line_ids - case
        snapshot = (unrelated.detail, unrelated.last_preview_at, len(unrelated.history_ids))
        def one_source(transaction):
            self.assertEqual(transaction, original)
            return payload, events
        with patch.object(type(original), '_repair_source', one_source):
            case.action_refresh_case()
        self.assertEqual((unrelated.detail, unrelated.last_preview_at, len(unrelated.history_ids)), snapshot)
        self.assertEqual(len(case.history_ids), 2)

    def test_completed_case_and_document_links_survive_refresh_and_reopen(self):
        payout, wizard, payload, events = self._historical('retained')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            wizard.action_apply()
        case, payment, credit = wizard.line_ids, wizard.line_ids.payment_ids, wizard.line_ids.credit_ids
        payout.generate_bank_statement()
        statement = payout.payout_statement_line_ids
        outstanding = payment._seek_for_lines()[0]
        statement._seek_for_lines()[1].account_id = outstanding.account_id
        (outstanding | statement.line_ids.filtered(lambda line: line.account_id == outstanding.account_id)).reconcile()
        statement.move_id.checked = True
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=AssertionError('Matched case re-read')):
            wizard.action_preview()
            payout.action_bulk_repair_transactions()
        self.assertEqual(wizard.line_ids, case)
        self.assertEqual(case.status, 'done')
        self.assertEqual(case.payment_ids, payment)
        self.assertEqual(case.credit_ids, credit)
        self.assertEqual(case.reconciliation_status, 'matched')
        self.assertIn('1 matched', wizard.progress_summary)
        self.assertEqual(case.history_ids.sorted('id', reverse=True).mapped('operation'), ['post_only', 'apply', 'preview'])

    def test_imported_order_becomes_linked_without_running_preview(self):
        payout, wizard, payload, events = self._historical('new-order')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=UserError('Import order')):
            wizard.action_preview()
        case = wizard.line_ids
        self.assertFalse(case.current_order_id)
        order = self.env['sale.order'].create(dict(partner_id=self.partner.id,
            shopify_instance_id=self.instance.id, shopify_order_id=payload['id']))
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=AssertionError('Re-read')):
            payout.action_bulk_repair_transactions()
            self.assertEqual(case.current_order_id, order)
            self.assertEqual(case.action_open_order()['res_id'], order.id)
        self.assertIn('/admin/orders/' + payload['id'], case.action_open_shopify_order()['url'])

    def test_order_repair_populates_payout_accounting_audit_and_history(self):
        order, invoice, payload, events = self._fixture()
        self.instance.refund_adjustment_product_id = self.product
        self._legacy(invoice, 197.10)
        payout = self.env['shopify.payout.report.ept'].create(dict(instance_id=self.instance.id,
            payout_reference_id='order-audit', payout_date=fields.Date.today(), currency_id=order.currency_id.id,
            amount=242.10, payout_transaction_ids=[Command.create(dict(transaction_type='charge',
                transaction_id='order-balance', source_order_id=order.shopify_order_id,
                source_order_transaction_id=events[0]['id'], amount=242.10))]))
        wizard = self.env['shopify.payout.repair.ept'].create(dict(payout_ids=[Command.set(payout.ids)], rematch_payouts=False))
        with patch.object(type(order), '_shopify_cash_source', return_value=(payload, events)):
            wizard.action_preview()
            self.assertEqual(wizard.line_ids.status, 'ready', wizard.line_ids.detail)
            wizard.action_apply()
        audit = payout.repair_audit_ids
        self.assertEqual(audit.order_id, order)
        self.assertEqual(audit.payment_audit_id, order.shopify_payment_audit_ids[:1])
        self.assertEqual(audit.payment_ids, wizard.line_ids.payment_ids)
        self.assertEqual(audit.transaction_ids, payout.payout_transaction_ids)
        self.assertFalse(payout.payout_transaction_ids._historical_collection_entry())
        self.assertEqual(payout.repair_history_ids.mapped('operation'), ['post_only', 'apply', 'preview'])
        payout.generate_bank_statement()
        def ledger_match(model, statement_id, line_ids):
            statement = self.env['account.bank.statement.line'].browse(statement_id)
            lines = self.env['account.move.line'].browse(line_ids)
            statement._seek_for_lines()[1].account_id = lines.account_id
            (lines | statement.line_ids.filtered(lambda line: line.account_id == lines.account_id)).reconcile()
        matcher = nullcontext() if 'bank.rec.widget' in self.env.registry.models else patch.object(
            type(payout), 'shopify_reconcile_bank_statement_line_ept', ledger_match)
        with matcher, patch.object(type(payout), 'get_invoices_for_reconcile', side_effect=AssertionError('Import')):
            payout.process_bank_statement()
        self.assertTrue(payout.payout_statement_line_ids.is_reconciled)
        self.assertEqual(payout.state, 'validated')

    def test_regrouping_cases_archives_old_results_without_removing_history(self):
        payout, wizard, payload, events = self._historical('regroup')
        events[1]['amount'] = '100'
        events.append(dict(events[1], id='regroup-second-refund', amount='115.35'))
        payload['refunds'][0]['transactions'].append(dict(id=events[2]['id']))
        payout.payout_transaction_ids.amount = -100
        self.env['shopify.payout.report.line.ept'].create(dict(payout_id=payout.id,
            transaction_type='refund', transaction_id='second-regroup-balance', amount=-115.35,
            source_order_id=payload['id'], source_order_transaction_id=events[2]['id']))
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', side_effect=UserError('Mapping missing')):
            wizard.action_preview()
        old_cases = wizard.line_ids
        old_history = payout.repair_history_ids
        self.assertEqual(len(old_cases), 2)
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        self.assertEqual(len(wizard.line_ids), 1)
        self.assertFalse(any(old_cases.mapped('active')))
        self.assertEqual(len(payout.repair_case_ids), 1)
        self.assertTrue(all(old_history.exists()))
        self.assertEqual(old_history.mapped('detail'), ['Mapping missing', 'Mapping missing'])

    def test_accountants_can_read_saved_results_but_only_managers_can_refresh(self):
        payout, wizard, payload, events = self._historical('reader')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        accountant = self.env['res.users'].with_context(no_reset_password=True).create(dict(
            name='Saved Repairs Reader', login='saved-repairs-reader', company_id=self.env.company.id,
            company_ids=[Command.set(self.env.company.ids)],
            groups_id=[Command.set([self.env.ref('base.group_user').id, self.env.ref('account.group_account_user').id])]))
        self.assertEqual(wizard.with_user(accountant).read(['result'])[0]['result'], wizard.result)
        self.assertEqual(wizard.line_ids.with_user(accountant).read(['detail'])[0]['detail'], wizard.line_ids.detail)
        self.assertEqual(payout.repair_history_ids.with_user(accountant).read(['operation'])[0]['operation'], 'preview')
        with self.assertRaises(AccessError):
            wizard.line_ids.with_user(accountant).action_refresh_case()
        with self.assertRaises(AccessError):
            wizard.line_ids.with_user(accountant).write({'detail': 'changed'})
        with patch.object(type(self.env['sale.order']), '_shopify_cash_source', side_effect=AssertionError('Unauthorized source read')):
            with self.assertRaises(AccessError):
                wizard.line_ids.with_user(accountant).action_review_payments()

    def test_failed_apply_is_saved_without_claiming_accounting_was_created(self):
        payout, wizard, payload, events = self._historical('failed')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            with patch.object(type(self.env['account.payment']), 'action_post', side_effect=UserError('Payment failed')):
                wizard.action_apply()
        self.assertFalse(payout.repair_audit_ids)
        self.assertEqual(payout.repair_history_ids.mapped('operation'), ['failure', 'preview'])
        self.assertEqual(payout.repair_history_ids[:1].detail, 'Payment failed')
        self.assertFalse(payout.repair_history_ids[:1].payment_ids)
        self.assertEqual(wizard.line_ids.last_error, 'Payment failed')

    def test_saved_history_cannot_be_forged_edited_or_deleted(self):
        payout, wizard, payload, events = self._historical('immutable')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        history = payout.repair_history_ids
        with self.assertRaises(UserError):
            history.write({'detail': 'forged'})
        with self.assertRaises(UserError):
            history.unlink()
        with self.assertRaises(UserError):
            history.create(dict(workspace_id=wizard.id, case_id=wizard.line_ids.id, company_id=self.env.company.id))

    def test_payout_resumes_existing_multi_payout_workspace(self):
        payout, wizard, payload, events = self._historical('multi')
        second = self._payout('multi-second')
        wizard.payout_ids |= second
        self.assertEqual(payout.action_bulk_repair_transactions()['res_id'], wizard.id)
        self.assertEqual((payout | second).action_bulk_repair_transactions()['res_id'], wizard.id)

    def test_newer_bulk_run_supersedes_display_of_older_single_payout_cases(self):
        payout, wizard, payload, events = self._historical('latest-run')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
        old_case = wizard.line_ids
        second = self._payout('latest-second')
        action = (payout | second).action_bulk_repair_transactions()
        latest = self.env['shopify.payout.repair.ept'].browse(action['res_id'])
        def source(transaction):
            if transaction.payout_id == second:
                raise UserError('Other order needs review')
            return payload, events
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', source):
            latest.action_preview()
        self.assertEqual(payout.action_bulk_repair_transactions()['res_id'], latest.id)
        self.assertEqual(payout.repair_case_ids, latest.line_ids.filtered(lambda line: line.payout_ids == payout))
        self.assertNotIn(old_case, payout.repair_case_ids)
        self.assertEqual(len(payout.repair_history_ids), 2)

    def test_refreshing_one_case_does_not_authorize_stale_settings_for_others(self):
        payout, wizard, payload, events = self._historical('settings')
        second, other_wizard, other_payload, other_events = self._historical('other-settings')
        wizard.payout_ids |= second
        def source(transaction):
            return (payload, events) if transaction.payout_id == payout else (other_payload, other_events)
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', source):
            wizard.action_preview()
            case = wizard.line_ids.filtered(lambda line: line.payout_ids == payout)
            other = wizard.line_ids - case
            wizard.cutover_date += timedelta(days=1)
            case.action_refresh_case()
            case.selected = False
            wizard.action_apply()
        self.assertEqual(other.status, 'review')
        self.assertIn('Settings changed for this case', other.detail)
        self.assertFalse(other.credit_ids)


del RepairFixtures
