"""Targeted Shopify payout import, closed periods and the public wizard/actions."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

try:
    from .test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE
except ImportError:
    from test_shopify_payout_generation import PayoutTestCase, ODOO_AVAILABLE

spec = importlib.util.spec_from_file_location('payout_import_utils',
    Path(__file__).resolve().parents[1] / 'models' / 'shopify_payout_import_utils.py')
utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(utils)

if ODOO_AVAILABLE:
    from odoo import fields
    from odoo.exceptions import UserError
    from .. import shopify


class TestPayoutIdentities(unittest.TestCase):
    def test_ids_accept_commas_newlines_and_deduplicate(self):
        self.assertEqual(utils.parse_payout_ids('140763300066, 140763300067\n140763300066; 00012'),
            ['140763300066', '140763300067', '12'])

    def test_accepts_shopify_payout_gid(self):
        self.assertEqual(utils.parse_payout_ids('gid://shopify/ShopifyPaymentsPayout/123,123'), ['123'])

    def test_rejects_empty_zero_and_non_payout_identities(self):
        for value in (None, '', ' , ; ', '0', '-123', '123,wrong', 'PTR00055',
                      'gid://shopify/Order/123', 'https://example.com/payouts/123'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                utils.parse_payout_ids(value)


class TestSpecificPayoutImport(PayoutTestCase):
    def _report(self, identity, **values):
        data = {'id': int(identity), 'status': 'paid', 'date': fields.Date.to_string(fields.Date.today()),
            'currency': self.env.company.currency_id.name, 'amount': '97.00'}
        data.update(values)
        return SimpleNamespace(to_dict=lambda: data)

    def _transactions(self, **kwargs):
        identity = kwargs['payout_id']
        data = {'id': identity + '-charge', 'type': 'charge', 'source_order_id': identity + '-missing-order',
            'currency': self.env.company.currency_id.name, 'amount': '100.00', 'fee': '3.00', 'net': '97.00'}
        return [SimpleNamespace(to_dict=lambda: data)]

    def test_requests_only_selected_ids_and_preserves_scheduler_checkpoint(self):
        self.instance.payout_last_import_date = '2026-01-01'
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(shopify.Payouts, 'find', side_effect=lambda identity: self._report(identity)) as find, \
                patch.object(shopify.Transactions, 'find', side_effect=self._transactions):
            payouts = self.env['shopify.payout.report.ept'].get_payout_report_by_ids('123,456\n123', self.instance)
        self.assertEqual(find.call_args_list, [call('123'), call('456')])
        self.assertEqual(set(payouts.mapped('payout_reference_id')), {'123', '456'})
        self.assertEqual(len(self._statement_lines(payouts)), 4)
        self.assertEqual(payouts.mapped('state'), ['generated', 'generated'])
        self.assertEqual(str(self.instance.payout_last_import_date), '2026-01-01')

    def test_invalid_or_unpaid_response_creates_no_partial_reports(self):
        for response in (self._report('999'), self._report('456', status='in_transit'), RuntimeError('Not found')):
            with self.subTest(response=response), patch.object(type(self.instance), 'connect_in_shopify'), \
                    patch.object(shopify.Payouts, 'find', side_effect=[self._report('123'), response]):
                with self.assertRaises(UserError):
                    self.env['shopify.payout.report.ept'].get_payout_report_by_ids('123,456', self.instance)
                self.assertFalse(self.env['shopify.payout.report.ept'].search([
                    ('instance_id', '=', self.instance.id), ('payout_reference_id', 'in', ['123', '456'])]))

    def test_invalid_selection_does_not_connect_to_shopify(self):
        with patch.object(type(self.instance), 'connect_in_shopify') as connect:
            with self.assertRaises(UserError):
                self.env['shopify.payout.report.ept'].get_payout_report_by_ids('123,PTR00055', self.instance)
        connect.assert_not_called()

    def test_locked_new_import_keeps_metadata_without_shifted_statement(self):
        self.env.company.fiscalyear_lock_date = fields.Date.today()
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(shopify.Payouts, 'find', return_value=self._report('123')), \
                patch.object(shopify.Transactions, 'find', side_effect=self._transactions):
            payout = self.env['shopify.payout.report.ept'].get_payout_report_by_ids('123', self.instance)
        self.assertEqual(payout.state, 'draft')
        self.assertEqual(payout.payout_date, fields.Date.today())
        self.assertEqual(len(payout.payout_transaction_ids), 2)
        self.assertFalse(self._statement_lines(payout))
        self.assertIn('closed accounting period', payout.reimport_reconciliation_issue)

    def test_specific_import_wizard_opens_requested_report(self):
        wizard = self.env['shopify.process.import.export'].with_context(search_default_remaining_reports=1).create({
            'shopify_operation': 'import_payouts_by_ids', 'shopify_instance_id': self.instance.id,
            'shopify_payout_ids': '123'})
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(shopify.Payouts, 'find', return_value=self._report('123')), \
                patch.object(shopify.Transactions, 'find', side_effect=self._transactions), \
                patch.object(type(self.env['product.product']), 'search_installed_module_ept', return_value=True):
            action = wizard.shopify_execute()
        payout = self.env['shopify.payout.report.ept'].browse(action['res_id'])
        self.assertEqual(payout.payout_reference_id, '123')
        self.assertEqual(action['views'][0][1], 'form')
        self.assertEqual(action['domain'], [('id', 'in', payout.ids)])
        self.assertNotIn('search_default_remaining_reports', action['context'])

    def test_bulk_action_reimports_in_each_store(self):
        other_instance = self.instance.copy({'name': 'Other payout store', 'shopify_host': 'other-payout-test.example.com'})
        first = self._payout('123')
        second = self._payout('456')
        second.instance_id = other_instance
        calls = []
        def capture(model, ids, instance):
            calls.append((ids, instance.id))
            return self.env[model._name]
        action = self.env.ref('shopify_ept.action_reimport_shopify_payouts')
        with patch.object(type(first), 'get_payout_report_by_ids', capture):
            result = action.with_context(active_model=first._name, active_ids=(first | second).ids).run()
        self.assertEqual(set(calls), {('123', self.instance.id), ('456', other_instance.id)})
        self.assertEqual(result['tag'], 'reload')

    def test_import_from_populated_recordset_returns_only_requested_store_report(self):
        existing = self._payout('999')
        with patch.object(type(self.instance), 'connect_in_shopify'), \
                patch.object(shopify.Payouts, 'find', return_value=self._report('123')), \
                patch.object(shopify.Transactions, 'find', side_effect=self._transactions):
            imported = existing.get_payout_report_by_ids('123', self.instance)
        self.assertEqual(imported.mapped('payout_reference_id'), ['123'])
        self.assertEqual(existing.state, 'draft')
        self.assertFalse(self._statement_lines(existing))
