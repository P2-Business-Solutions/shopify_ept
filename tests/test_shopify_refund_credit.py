"""Existing reviewed credits settle exact payout refunds without duplicate cash."""
from copy import deepcopy
from unittest.mock import patch

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from .test_shopify_payout_repair import TestShopifyPayoutRepair as RefundFixtures
    from .test_shopify_historical_collection import TestShopifyHistoricalCollection as MatchFixtures
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
    from test_shopify_payout_repair import TestShopifyPayoutRepair as RefundFixtures
    from test_shopify_historical_collection import TestShopifyHistoricalCollection as MatchFixtures
if ODOO_AVAILABLE:
    from odoo import Command, fields
    from odoo.exceptions import UserError


class TestShopifyRefundCredit(PayoutTestCase):
    _historical = RefundFixtures._historical
    _matcher = MatchFixtures._matcher

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        RefundFixtures._setup_repair_data.__func__(cls)

    def _case(self, suffix='credit'):
        payout, workspace, payload, events = self._historical(suffix)
        credit = self.env['account.move'].create(dict(move_type='out_refund', journal_id=self.sales_journal.id,
            partner_id=self.partner.id, currency_id=payout.currency_id.id,
            invoice_date=fields.Date.today(), invoice_line_ids=[Command.create(dict(
                name='Reviewed historical return', account_id=self.revenue.id, quantity=1,
                price_unit=215.35, tax_ids=[Command.clear()]))]))
        credit.action_post()
        wizard = self.env['shopify.refund.credit.link.ept'].create(dict(
            transaction_id=payout.payout_transaction_ids.id, credit_note_id=credit.id,
            review_note='Reviewed Loop credit; original sale belongs to the old system.'))
        return payout, workspace, payload, events, credit, wizard

    def test_link_and_reconcile_existing_credit_without_order_or_new_payment(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            workspace.action_preview()
            cases = workspace.line_ids
            before_payments = self.env['account.payment'].search_count([])
            before_credits = self.env['account.move'].search_count([('move_type', '=', 'out_refund')])
            wizard.action_preview()
            self.assertFalse(credit.shopify_refund_reference_ids)
            wizard.action_apply()
            self.assertEqual(credit.amount_residual, 0)
            self.assertTrue(payout.payout_statement_line_ids.is_reconciled)
            self.assertEqual(payout.payout_statement_line_ids.refund_invoice_id, credit)
            self.assertEqual(credit.shopify_refund_reference_ids.transaction_id, events[1]['id'])
            self.assertEqual(credit.shopify_refund_id, payload['refunds'][0]['id'])
            self.assertEqual(self.env['account.payment'].search_count([]), before_payments)
            self.assertEqual(self.env['account.move'].search_count([('move_type', '=', 'out_refund')]), before_credits)
            self.assertEqual(cases.reconciliation_status, 'matched')
            self.assertEqual(cases.credit_ids, credit)
            self.assertTrue(payout.repair_audit_ids)
            # Repeat preview/apply, bulk refresh and matching retain the exact credit.
            wizard.action_preview()
            wizard.action_apply()
            workspace.open_only = False
            workspace.rematch_payouts = True
            workspace.action_preview()
            self.assertEqual(workspace.line_ids.kind, 'credit_link')
            self.assertIn(credit.display_name, workspace.line_ids.review_html)
            workspace.action_apply()
            self.assertEqual(len(credit.shopify_refund_reference_ids), 1)
            self.assertEqual(self.env['account.payment'].search_count([]), before_payments)

    def test_unknown_credit_source_id_is_not_treated_as_a_legacy_alias(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        credit.shopify_refund_id = 'another-refund'
        with self.assertRaisesRegex(UserError, 'different Shopify source'):
            payout.payout_transaction_ids._refund_credit_link_plan(credit, (payload, events))

    def test_customer_email_can_prove_existing_credit_without_shopify_mapping(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        self.env['shopify.res.partner.ept'].search([('partner_id', '=', self.partner.id)]).unlink()
        self.partner.email = 'loop-credit@example.com'
        payload['customer']['email'] = self.partner.email
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            self.assertEqual(wizard.plan['credit_id'], credit.id)
            self.env['res.partner'].create(dict(name='Ambiguous email', email=self.partner.email))
            with self.assertRaisesRegex(UserError, 'unique mapping or email'):
                wizard.action_preview()

    def test_rejects_changed_source_after_preview(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            events[1]['processed_at'] = fields.Date.today().isoformat() + 'T11:00:00-04:00'
            with self.assertRaisesRegex(UserError, 'changed after preview'):
                wizard.action_apply()
        self.assertFalse(credit.shopify_refund_reference_ids)
        self.assertEqual(credit.amount_residual, credit.amount_total)

    def test_rejects_wrong_customer_draft_amount_gateway_and_closed_period(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        line = payout.payout_transaction_ids
        bad_payload = deepcopy(payload)
        self.env['shopify.res.partner.ept'].search([('partner_id', '=', self.partner.id)]).partner_id = self.env['res.partner'].create(dict(name='Wrong customer'))
        with self.assertRaisesRegex(UserError, 'credit customer differs'):
            line._refund_credit_link_plan(credit, (bad_payload, events))
        self.env['shopify.res.partner.ept'].search([('shopify_customer_id', '=', 'customer')]).partner_id = self.partner
        bad = deepcopy(events)
        bad[1]['gateway'] = bad[0]['gateway'] = 'paypal'
        with self.assertRaisesRegex(UserError, 'Shopify Payments refund'):
            line._refund_credit_link_plan(credit, (payload, bad))
        with patch.object(type(payout), '_check_payout_reimport_period', side_effect=UserError('closed period')):
            with self.assertRaisesRegex(UserError, 'closed period'):
                line._refund_credit_link_plan(credit, (payload, events))
        credit.button_draft()
        with self.assertRaisesRegex(UserError, 'posted customer credit'):
            line._refund_credit_link_plan(credit, (payload, events))
        credit.invoice_line_ids.price_unit = 200
        credit.action_post()
        with self.assertRaisesRegex(UserError, 'credit total must equal'):
            line._refund_credit_link_plan(credit, (payload, events))

    def test_reuses_exact_payment_and_refuses_unidentified_allocation(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        payment = self.env['account.payment'].create(dict(partner_id=self.partner.id, partner_type='customer',
            payment_type='outbound', journal_id=self.journal.id, amount=credit.amount_total,
            payment_method_line_id=self.journal.outbound_payment_method_line_ids[:1].id,
            currency_id=credit.currency_id.id, date=fields.Date.today()))
        payment.action_post()
        (payment._seek_for_lines()[1] | credit.line_ids.filtered(lambda line: line.account_type == 'asset_receivable')).reconcile()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            with self.assertRaisesRegex(UserError, 'unidentified payment'):
                wizard.action_preview()
            payment.write(dict(shopify_instance_id=self.instance.id, shopify_order_transaction_id=events[1]['id']))
            wizard.action_preview()
            count = self.env['account.payment'].search_count([])
            wizard.action_apply()
            self.assertTrue(payment._seek_for_lines()[0].reconciled)
            self.assertTrue(payout.payout_statement_line_ids.is_reconciled)
            self.assertEqual(self.env['account.payment'].search_count([]), count)

    def test_split_refund_document_settles_one_credit_across_payouts(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        events[1]['amount'] = '100'
        second_event = dict(events[1], id='refund-second', amount='115.35')
        events.append(second_event)
        payload['refunds'][0]['transactions'].append(dict(id=second_event['id']))
        payout.payout_transaction_ids.amount = -100
        payout.amount = -100
        second = self.env['shopify.payout.report.ept'].create(dict(instance_id=self.instance.id,
            payout_reference_id='second-split', payout_date=fields.Date.today(), currency_id=credit.currency_id.id,
            amount=-115.35, payout_transaction_ids=[Command.create(dict(transaction_type='refund', amount=-115.35,
                transaction_id='split-balance-second', source_order_id=payload['id'], source_order_transaction_id=second_event['id']))]))
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            wizard.action_preview()
            wizard.action_apply()
        self.assertEqual(len(credit.shopify_refund_reference_ids), 2)
        self.assertEqual(credit.amount_residual, 0)
        self.assertTrue(all((payout | second).payout_statement_line_ids.mapped('is_reconciled')))

    def test_matching_failure_retains_credit_reference_and_audit_for_retry(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            wizard.action_preview()
            with patch.object(type(payout), 'shopify_reconcile_bank_statement_line_ept', side_effect=UserError('matching test failure')):
                wizard.action_apply()
            self.assertEqual(wizard.state, 'review')
        self.assertTrue(credit.shopify_refund_reference_ids)
        self.assertIn('matching test failure', wizard.result)
        self.assertFalse(payout.payout_statement_line_ids.is_reconciled)
        self.assertEqual(credit.amount_residual, credit.amount_total)
        self.assertTrue(payout.repair_audit_ids)
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            wizard.action_apply()
        self.assertEqual(credit.amount_residual, 0)
        self.assertEqual(wizard.state, 'done')

    def test_conflicting_credit_and_source_reference_cannot_be_reassigned(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        plan = payout.payout_transaction_ids._refund_credit_link_plan(credit, (payload, events))
        payout.payout_transaction_ids._register_refund_credit(plan, 'Reviewed')
        reference = credit.shopify_refund_reference_ids
        with self.assertRaises(UserError):
            reference.write(dict(amount=1))
        with self.assertRaises(UserError):
            reference.unlink()
        replacement = credit.copy()
        replacement.action_post()
        with self.assertRaisesRegex(UserError, 'Another credit note'):
            payout.payout_transaction_ids._refund_credit_link_plan(replacement, (payload, events))
        payout.payout_transaction_ids.source_order_id = 'wrong-order'
        with self.assertRaisesRegex(UserError, 'conflicts with its reviewed'):
            payout.payout_transaction_ids._refund_credit_reference()

    def test_duplicate_payout_refund_and_unreviewed_application_are_blocked(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        with self.assertRaisesRegex(UserError, 'Preview the source refund'):
            wizard.action_apply()
        self.env['shopify.payout.report.line.ept'].create(dict(
            payout_id=payout.id, transaction_type='refund', transaction_id='duplicate-balance',
            source_order_id=payload['id'], source_order_transaction_id=events[1]['id'], amount=-215.35))
        with self.assertRaisesRegex(UserError, 'Several payout rows'):
            payout.payout_transaction_ids[:1]._refund_credit_link_plan(credit, (payload, events))

    def test_exact_unallocated_refund_payment_is_reused_and_allocated(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        payment = self.env['account.payment'].create(dict(partner_id=self.partner.id, partner_type='customer',
            payment_type='outbound', journal_id=self.journal.id, amount=credit.amount_total,
            payment_method_line_id=self.journal.outbound_payment_method_line_ids[:1].id,
            currency_id=credit.currency_id.id, date=fields.Date.today(),
            shopify_instance_id=self.instance.id, shopify_order_transaction_id=events[1]['id']))
        payment.action_post()
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._matcher(payout):
            wizard.action_preview()
            wizard.action_apply()
        self.assertEqual(credit.amount_residual, 0)
        self.assertTrue(payment._seek_for_lines()[0].reconciled)
        self.assertEqual(credit.reconciled_payment_ids, payment)

    def test_changed_posted_credit_is_not_matched_from_old_reference(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        plan = payout.payout_transaction_ids._refund_credit_link_plan(credit, (payload, events))
        payout.payout_transaction_ids._register_refund_credit(plan, 'Reviewed')
        credit.button_draft()
        with self.assertRaisesRegex(UserError, 'credit note changed after review'):
            payout.payout_transaction_ids._refund_credit_reference()

    def test_existing_opening_cash_treatment_blocks_new_credit_assignment(self):
        payout, workspace, payload, events, credit, wizard = self._case()
        self.env['account.move'].create(dict(journal_id=self.journal.id,
            shopify_instance_id=self.instance.id, shopify_source_order_id=payload['id'],
            shopify_legacy_collection_transaction_id=events[1]['id']))
        with self.assertRaisesRegex(UserError, 'opening-balance cash treatment'):
            payout.payout_transaction_ids._refund_credit_link_plan(credit, (payload, events))
