"""Targeted customer import unblocks the saved refund preview without sales imports."""
from types import SimpleNamespace
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
    from odoo import Command
    from odoo.exceptions import UserError, AccessError
    from .. import shopify


class TestShopifyRepairCustomer(PayoutTestCase):
    _historical = RefundFixtures._historical
    _matcher = MatchFixtures._matcher

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        RefundFixtures._setup_repair_data.__func__(cls)

    def _case(self, suffix):
        payout, workspace, payload, events = self._historical(suffix)
        payout.payout_status = 'paid'
        customer = dict(id='missing-customer-' + suffix, first_name='Historical', last_name='Customer',
                        email=suffix + '@example.invalid', default_address={}, tags='')
        payload['customer'] = dict(customer)
        return payout, workspace, payload, events, customer

    def _customer(self, customer):
        return patch.object(shopify.Customer, 'find', return_value=SimpleNamespace(to_dict=lambda: customer))

    def _counts(self):
        models = ['sale.order', 'account.move', 'account.payment', 'stock.picking']
        if 'mrp.production' in self.env:
            models.append('mrp.production')
        return {model: self.env[model].search_count([]) for model in models}

    def test_import_refreshes_only_case_and_retries_without_duplicate_customer_or_accounting(self):
        payout, workspace, payload, events, customer = self._case('new')
        self.instance.shopify_last_date_customer_import = '2026-10-01'
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._customer(customer) as find:
            workspace.action_preview()
            case = workspace.line_ids
            self.assertEqual(case.status, 'review')
            self.assertIn('customer mapping', case.detail)
            before = self._counts()
            case.action_import_customer()
            self.assertEqual(case.status, 'ready', case.detail)
            mapping = self.env['shopify.res.partner.ept'].search([
                ('shopify_instance_id', '=', self.instance.id), ('shopify_customer_id', '=', customer['id'])])
            self.assertEqual(len(mapping), 1)
            self.assertEqual(mapping.partner_id.email, customer['email'])
            self.assertEqual(self._counts(), before)
            customer_count = self.env['res.partner'].search_count([])
            case.action_import_customer()
            self.assertEqual(self.env['res.partner'].search_count([]), customer_count)
            self.assertEqual(self._counts(), before)
            self.assertEqual(str(self.instance.shopify_last_date_customer_import), '2026-10-01 00:00:00')
            self.assertEqual(find.call_count, 2)
            self.assertEqual(find.call_args.args, (customer['id'],))
            self.assertEqual(len(payout.repair_history_ids.filtered(lambda history: history.operation == 'customer')), 2)
            self.assertFalse(case.credit_ids)
            self.assertFalse(case.payment_ids)

    def test_reuses_unique_email_and_original_refund_apply_finishes_matching(self):
        payout, workspace, payload, events, customer = self._case('reuse')
        self.partner.email = customer['email'].upper()
        before = self.env['res.partner'].search_count([])
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._customer(customer), self._matcher(payout):
            workspace.action_preview()
            workspace.line_ids.action_import_customer()
            self.assertEqual(workspace.line_ids.plan['values']['partner_id'], self.partner.id)
            self.assertEqual(self.env['res.partner'].search_count([]), before)
            workspace.rematch_payouts = True
            workspace.action_preview()
            workspace.action_apply()
            self.assertEqual(workspace.line_ids.status, 'done', workspace.result)
            self.assertEqual(workspace.line_ids.reconciliation_status, 'matched', workspace.result)
            self.assertTrue(payout.payout_statement_line_ids.is_reconciled)
            self.assertEqual(len(workspace.line_ids.credit_ids), 1)
            self.assertEqual(len(workspace.line_ids.payment_ids), 1)
            self.assertEqual(workspace.line_ids.credit_ids.amount_residual, 0)
            self.assertFalse(self.env['sale.order'].search([('shopify_order_id', '=', payload['id'])]))
            # Finish the bank leg through existing settlement accounting too.
            transit = self.outstanding.copy({'code': 'CUSTTRANS', 'name': 'Customer Repair Transit'})
            bank_account = self.journal.default_account_id.copy({'code': 'CUSTBANK'})
            bank = self.journal.copy({'code': 'CBANK', 'default_account_id': bank_account.id,
                                      'autocheck_on_post': True})
            (bank.inbound_payment_method_line_ids | bank.outbound_payment_method_line_ids).payment_account_id = transit
            transfer = self.env['account.journal'].create(dict(name='Customer Repair Transfer', code='CTR',
                type='general', company_id=self.env.company.id))
            self.instance.write(dict(shopify_payout_bank_journal_id=bank.id,
                shopify_payout_transit_account_id=transit.id, shopify_payout_transfer_journal_id=transfer.id))
            payout.action_create_settlement_transfer()
            bank_line = self.env['account.bank.statement.line'].create(dict(journal_id=bank.id,
                date=payout.payout_date, payment_ref='Shopify payout ' + payout.payout_reference_id,
                amount=payout.amount, counterpart_account_id=transit.id))
            (payout.settlement_line_id | bank_line.line_ids.filtered(lambda line: line.account_id == transit)).reconcile()
            self.assertEqual(payout.settlement_status, 'matched')
            self.assertEqual(payout.settlement_line_id.amount_residual, 0)
            self.assertEqual(payout.settlement_bank_line_ids, bank_line)
            settlement = payout.settlement_move_id
            payout.action_create_settlement_transfer()
            self.assertEqual(payout.settlement_move_id, settlement)

    def test_ambiguous_or_archived_email_does_not_import_or_select_case(self):
        for suffix, active in [('duplicate', True), ('archived', False)]:
            payout, workspace, payload, events, customer = self._case(suffix)
            self.partner.email = customer['email']
            self.env['res.partner'].create(dict(name='Other Customer', email=customer['email'], active=active))
            with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._customer(customer):
                workspace.action_preview()
                with self.assertRaisesRegex(UserError, 'ambiguous, inactive'):
                    workspace.line_ids.action_import_customer()
                self.assertFalse(workspace.line_ids.selected)
                self.assertFalse(self.env['shopify.res.partner.ept'].search([
                    ('shopify_instance_id', '=', self.instance.id), ('shopify_customer_id', '=', customer['id'])]))

    def test_missing_email_or_wrong_customer_response_stays_for_review(self):
        for suffix, change in [('no-email', {'email': False}), ('wrong-id', {'id': 'wrong'})]:
            payout, workspace, payload, events, customer = self._case(suffix)
            response = dict(customer, **change)
            with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._customer(response):
                workspace.action_preview()
                before = self.env['res.partner'].search_count([])
                with self.assertRaises(UserError):
                    workspace.line_ids.action_import_customer()
                self.assertEqual(self.env['res.partner'].search_count([]), before)
                self.assertEqual(workspace.line_ids.status, 'review')

    def test_conflicting_customer_mappings_are_not_changed(self):
        payout, workspace, payload, events, customer = self._case('mapping-conflict')
        other = self.env['res.partner'].create(dict(name='Another Customer'))
        self.env['shopify.res.partner.ept'].create([
            dict(shopify_instance_id=self.instance.id, shopify_customer_id=customer['id'], partner_id=partner.id)
            for partner in (self.partner, other)])
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._customer(customer):
            workspace.action_preview()
            with self.assertRaisesRegex(UserError, 'existing Shopify customer mapping'):
                workspace.line_ids.action_import_customer()

    def test_manager_required_before_reading_customer_source(self):
        payout, workspace, payload, events, customer = self._case('permissions')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            workspace.action_preview()
        accountant = self.env['res.users'].with_context(no_reset_password=True).create(dict(
            name='Customer Import Reader', login='customer-import-reader', company_id=self.env.company.id,
            company_ids=[Command.set(self.env.company.ids)],
            groups_id=[Command.set([self.env.ref('base.group_user').id,
                                   self.env.ref('account.group_account_user').id])]))
        with self._customer(customer) as find, patch.object(type(payout.payout_transaction_ids),
                '_repair_source', side_effect=AssertionError('Unauthorized source read')) as source:
            with self.assertRaises(AccessError):
                workspace.line_ids.with_user(accountant).action_import_customer()
        source.assert_not_called()
        find.assert_not_called()

    def test_regular_manager_import_keeps_user_provenance(self):
        payout, workspace, payload, events, customer = self._case('manager')
        manager = self.env['res.users'].with_context(no_reset_password=True).create(dict(
            name='Customer Import Manager', login='customer-import-manager', company_id=self.env.company.id,
            company_ids=[Command.set(self.env.company.ids)],
            groups_id=[Command.set([self.env.ref('base.group_user').id,
                                   self.env.ref('account.group_account_manager').id,
                                   self.env.ref('base.group_partner_manager').id,
                                   self.env.ref('shopify_ept.group_shopify_ept').id])]))
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._customer(customer):
            workspace.action_preview()
            workspace.line_ids.with_user(manager).action_import_customer()
        self.assertEqual(workspace.line_ids.status, 'ready', workspace.line_ids.detail)
        mapping = self.env['shopify.res.partner.ept'].search([
            ('shopify_instance_id', '=', self.instance.id), ('shopify_customer_id', '=', customer['id'])])
        self.assertEqual(mapping.create_uid, manager)
        self.assertEqual(mapping.partner_id.create_uid, manager)
        self.assertEqual(payout.repair_history_ids.filtered(lambda history: history.operation == 'customer').create_uid, manager)

    def test_remaining_accounting_blocker_is_retained_after_customer_import(self):
        payout, workspace, payload, events, customer = self._case('locked')
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)), self._customer(customer):
            workspace.action_preview()
            self.env.company.fiscalyear_lock_date = payout.payout_date
            before = self._counts()
            workspace.line_ids.action_import_customer()
            self.assertEqual(workspace.line_ids.status, 'review')
            self.assertFalse(workspace.line_ids.selected)
            self.assertIn('closed accounting period', workspace.line_ids.detail)
            self.assertEqual(self._counts(), before)
            self.assertTrue(payout.repair_history_ids.filtered(lambda history: history.operation == 'customer'))

    def test_charge_case_cannot_use_refund_customer_shortcut(self):
        payout, workspace, payload, events, customer = self._case('charge')
        payout.payout_transaction_ids.transaction_type = 'charge'
        with patch.object(type(payout.payout_transaction_ids), '_repair_source', return_value=(payload, events)):
            workspace.action_preview()
        with self._customer(customer) as find, self.assertRaisesRegex(UserError, 'unmatched refund case'):
            workspace.line_ids.action_import_customer()
        find.assert_not_called()
