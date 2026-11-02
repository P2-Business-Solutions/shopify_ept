"""Run the actual inventory processor against a local Shopify transport double."""
import ast
from contextlib import nullcontext
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('inventory_export_utils', ROOT / 'models/shopify_inventory_utils.py')
UTILS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UTILS)


def load_methods(path, names, namespace):
    source = ast.parse(path.read_text())
    model = next(node for node in source.body if isinstance(node, ast.ClassDef))
    methods = [node for node in model.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), str(path), 'exec'), namespace)
    return type('InventoryExport', (), {name: namespace[name] for name in names})


SDK_NAMESPACE = {'json': json}
GraphQL = load_methods(ROOT / 'shopify/resources/graphql.py', {'__init__', 'merge_headers', 'execute'}, SDK_NAMESPACE)
NAMESPACE = {name: getattr(UTILS, name) for name in (
    'chunked', 'shopify_gid', 'prepare_inventory_set_input', 'inventory_user_error_indexes',
    'INVENTORY_SET_QUANTITIES_MUTATION', 'INVENTORY_LEVELS_QUERY', 'INVENTORY_ACTIVATE_MUTATION')}
NAMESPACE.update(json=json, uuid=uuid, UserError=ValueError, OperationalError=type('OperationalError', (Exception,), {}),
    HTTPError=urllib.error.HTTPError, URLError=urllib.error.URLError,
    fields=SimpleNamespace(Datetime=SimpleNamespace(now=lambda: '2026-11-02 12:00:00')))
METHODS = {'process_export_stock_queue_data', '_process_inventory_batch', '_process_inventory_location',
    '_set_inventory_location', '_verify_inventory_exports', '_confirm_inventory_exports',
    '_read_inventory_location', '_execute_inventory_mutation', '_execute_inventory_graphql'}
InventoryExport = load_methods(ROOT / 'models/export_stock_queue_line_ept.py', METHODS, NAMESPACE)


class Record(SimpleNamespace):
    def write(self, values):
        for name, value in values.items():
            setattr(self, name, value)


class Lines(list):
    def filtered(self, predicate):
        return Lines(line for line in self if predicate(line))

    def mapped(self, name):
        result = Lines()
        for line in self:
            value = getattr(line, name)
            if not any(value is item or value == item for item in result):
                result.append(value)
        return result

    def __or__(self, other):
        return Lines([*self, *other] if isinstance(other, Lines) else [*self, other])

    def __sub__(self, other):
        return Lines(line for line in self if not any(line is item for item in other))

    def write(self, values):
        for line in self:
            line.write(values)

    def invalidate_recordset(self, fields=None):
        pass

    def _record_successful_inventory_exports(self):
        for line in self:
            line.export_recorded = True

    _confirm_inventory_exports = NAMESPACE['_confirm_inventory_exports']


class TestShopifyInventoryExport(unittest.TestCase):
    def setUp(self):
        self.instance = Record(id=1, shopify_password='inventory-store-token', connect_in_shopify=Mock())
        self.queue = Record(id=7, shopify_instance_id=self.instance, is_process_queue=False,
                            invalidate_recordset=Mock())
        self.line = self._line(1)
        self.lines = Lines([self.line])
        self.model = InventoryExport()
        self.model.filtered = self.lines.filtered
        self.model.browse = lambda ids=(): Lines(line for line in self.lines if line.id in ids)
        self.model._refresh_inventory_lines = lambda instance, lines: lines
        self.model._cr = SimpleNamespace(commit=Mock(), execute=Mock(), fetchone=Mock(return_value=(True,)),
                                        rollback=Mock(), savepoint=nullcontext)
        self.model.env = SimpleNamespace(context={}, cr=self.model._cr)
        def with_context(**values):
            self.model.env.context.update(values)
            return self.model
        self.model.with_context = with_context
        self.model._fail_inventory_lines = Mock(side_effect=lambda instance, lines, detail:
            Lines([lines] if isinstance(lines, Record) else lines).write({'state': 'failed', 'error': detail}))
        self.shared_headers = {'User-Agent': 'Inventory test', 'X-Shopify-Access-Token': 'other-store-token'}
        sdk = SimpleNamespace(GraphQL=GraphQL, ShopifyResource=SimpleNamespace(
            get_site=lambda: 'https://inventory-test.myshopify.com/admin/api/2026-01',
            get_headers=lambda: self.shared_headers))
        self.remote = {('123', '456'): 2}
        self.overrides = []
        self.rejection = None
        self.null_adjustment = False
        self.mismatch = False
        self.remote_variant = '789'
        self.tracked = True
        self.location_active = True
        self.urlopen = Mock(side_effect=self._response)
        SDK_NAMESPACE.update(shopify=sdk, urllib=SimpleNamespace(
            request=SimpleNamespace(Request=urllib.request.Request, urlopen=self.urlopen), error=urllib.error))
        NAMESPACE.update(shopify=sdk, time=SimpleNamespace(sleep=Mock(), monotonic=Mock(return_value=0)))

    def _line(self, identity, location='456', quantity=5, state='draft'):
        return Record(id=identity, inventory_item_id='123', location_id=location, quantity=quantity,
            state=state, export_stock_queue_id=self.queue, export_recorded=False,
            shopify_product_id=Record(variant_id='789', inventory_management='shopify'))

    def _node(self, item_id, location_id):
        key = (item_id.rsplit('/', 1)[-1], location_id.rsplit('/', 1)[-1])
        quantity = self.remote.get(key)
        return {'id': item_id, 'tracked': self.tracked,
            'variant': {'id': UTILS.shopify_gid('ProductVariant', self.remote_variant)},
            'inventoryLevel': None if quantity is None else {'location': {'id': location_id},
                'quantities': [{'name': 'available', 'quantity': quantity}]}}

    def _response(self, request, timeout=None):
        self.assertEqual(timeout, 20)
        if request.get_header('X-shopify-access-token') != self.instance.shopify_password:
            raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, io.BytesIO())
        if self.overrides:
            response = self.overrides.pop(0)
            if isinstance(response, Exception):
                raise response
            return SimpleNamespace(read=lambda: json.dumps(response).encode())
        payload = json.loads(request.data)
        variables = payload['variables']
        if payload['operationName'] == 'InventoryLevels':
            data = {'location': {'id': variables['locationId'], 'isActive': self.location_active},
                'nodes': [self._node(identity, variables['locationId']) for identity in variables['ids']]}
        elif payload['operationName'] == 'InventoryActivate':
            key = (variables['inventoryItemId'].rsplit('/', 1)[-1], variables['locationId'].rsplit('/', 1)[-1])
            self.remote[key] = variables['available']
            data = {'inventoryActivate': {'userErrors': [], 'inventoryLevel': {
                'item': {'id': variables['inventoryItemId']}, 'location': {'id': variables['locationId']}}}}
        else:
            quantities = variables['input']['quantities']
            errors = self.rejection or []
            self.rejection = None
            if not errors and not self.null_adjustment:
                for change in quantities:
                    key = (change['inventoryItemId'].rsplit('/', 1)[-1], change['locationId'].rsplit('/', 1)[-1])
                    self.assertEqual(change['changeFromQuantity'], self.remote[key])
                    self.remote[key] = change['quantity'] + (1 if self.mismatch else 0)
            data = {'inventorySetQuantities': {'userErrors': errors,
                'inventoryAdjustmentGroup': None if errors or self.null_adjustment else {'createdAt': 'now'}}}
        body = json.dumps({'data': data}).encode()
        return SimpleNamespace(read=lambda: body)

    def _operations(self):
        return [json.loads(call.args[0].data)['operationName'] for call in self.urlopen.call_args_list]

    def test_authentication_and_readback_without_changing_shared_headers(self):
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.line.state, 'done')
        self.assertTrue(self.line.export_recorded)
        self.assertEqual(self.remote[('123', '456')], 5)
        self.assertEqual(self.shared_headers['X-Shopify-Access-Token'], 'other-store-token')
        self.assertEqual(self._operations(), ['InventoryLevels', 'InventorySet', 'InventoryLevels'])

    def test_already_correct_inventory_is_confirmed_without_a_mutation(self):
        self.remote[('123', '456')] = 5
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.line.state, 'done')
        self.assertEqual(self._operations(), ['InventoryLevels'])

    def test_two_locations_receive_their_own_quantities(self):
        self.lines.append(self._line(2, '457', 8))
        self.remote[('123', '457')] = 1
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.remote, {('123', '456'): 5, ('123', '457'): 8})
        self.assertTrue(all(line.state == 'done' for line in self.lines))

    def test_indexed_error_does_not_mark_untouched_rows_successful(self):
        self.lines.append(self._line(2))
        self.lines[1].inventory_item_id = '124'
        self.remote[('124', '456')] = 3
        self.rejection = [{'field': ['input', 'quantities', '1'], 'message': 'Invalid inventory item'}]
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.line.state, 'done')
        self.assertEqual(self.lines[1].state, 'failed')
        self.assertEqual(self.remote, {('123', '456'): 5, ('124', '456'): 3})
        self.assertEqual(self._operations().count('InventorySet'), 2)

    def test_null_adjustment_is_not_success(self):
        self.null_adjustment = True
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.line.state, 'failed')
        self.assertIn('adjustment group', self.line.error)
        self.assertFalse(self.line.export_recorded)

    def test_readback_mismatch_is_not_success(self):
        self.mismatch = True
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.line.state, 'failed')
        self.assertFalse(self.line.export_recorded)

    def test_wrong_variant_never_receives_inventory(self):
        self.remote_variant = '999'
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.line.state, 'failed')
        self.assertEqual(self._operations(), ['InventoryLevels'])

    def test_untracked_inventory_is_reported_without_disabling_odoo_tracking(self):
        self.tracked = False
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.line.state, 'failed')
        self.assertEqual(self.line.shopify_product_id.inventory_management, 'shopify')

    def test_inactive_destination_never_receives_inventory(self):
        self.location_active = False
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.line.state, 'failed')
        self.assertEqual(self._operations(), ['InventoryLevels'])

    def test_missing_inventory_level_is_activated_at_exact_mapped_location(self):
        self.remote.clear()
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.remote, {('123', '456'): 5})
        self.assertEqual(self.line.state, 'done')
        self.assertEqual(self._operations(), ['InventoryLevels', 'InventoryActivate', 'InventoryLevels'])

    def test_throttling_retries_identical_authenticated_request_and_key(self):
        self.overrides = [{'errors': [{'extensions': {'code': 'THROTTLED'}}]}]
        self.model._execute_inventory_mutation(self.instance, {'quantities': []}, 'same-key')
        requests = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].data, requests[1].data)
        NAMESPACE['time'].sleep.assert_called_once_with(1)

    def test_http_throttling_honors_bounded_retry_after(self):
        self.overrides = [urllib.error.HTTPError('https://inventory-test.myshopify.com', 429, 'Throttled',
                                               {'Retry-After': '300'}, io.BytesIO())]
        self.model._execute_inventory_mutation(self.instance, {'quantities': []}, 'same-key')
        NAMESPACE['time'].sleep.assert_called_once_with(5)

    def test_network_timeout_retries_same_request(self):
        self.overrides = [TimeoutError('read timed out')]
        self.model._execute_inventory_mutation(self.instance, {'quantities': []}, 'same-key')
        requests = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual(requests[0].data, requests[1].data)

    def test_rejected_credentials_are_not_retried_or_marked_done(self):
        self.overrides = [urllib.error.HTTPError('https://inventory-test.myshopify.com', 401, 'Unauthorized', {}, io.BytesIO())]
        self.model._process_inventory_batch(self.instance, self.queue, self.lines)
        self.assertEqual(self.line.state, 'failed')
        self.assertFalse(self.line.export_recorded)
        self.assertEqual(self.urlopen.call_count, 1)
        NAMESPACE['time'].sleep.assert_not_called()

    def test_manual_retry_processes_failed_and_draft_lines_only(self):
        self.line.state = 'failed'
        for identity, state in enumerate(('draft', 'done', 'cancel'), start=2):
            self.lines.append(self._line(identity, state=state))
        self.model._process_inventory_batch = Mock()
        self.model.process_export_stock_queue_data()
        self.assertEqual([line.id for line in self.model._process_inventory_batch.call_args.args[2]], [1, 2])
        self.assertFalse(self.queue.is_action_require)

    def test_progress_is_committed_before_an_interrupted_second_batch(self):
        self.lines.extend(self._line(index) for index in range(2, 252))
        committed = []
        self.model._cr.commit.side_effect = lambda: committed.append(sum(line.state == 'done' for line in self.lines))
        def process(instance, queue, lines):
            if lines[0].id > 250:
                raise KeyboardInterrupt('worker stopped')
            lines.write({'state': 'done'})
        self.model._process_inventory_batch = process
        with self.assertRaises(KeyboardInterrupt):
            self.model.process_export_stock_queue_data()
        self.assertEqual(committed, [0, 250])

    def test_time_budget_yields_after_committing_one_batch(self):
        self.lines.extend(self._line(index) for index in range(2, 252))
        def process(instance, queue, lines):
            lines.write({'state': 'done'})
            NAMESPACE['time'].monotonic.return_value = 300
        self.model._process_inventory_batch = process
        self.model.process_export_stock_queue_data()
        self.assertEqual(sum(line.state == 'done' for line in self.lines), 250)
        self.assertEqual(self.lines[-1].state, 'draft')

    def test_overlapping_instance_run_leaves_lines_available_for_retry(self):
        self.model._cr.fetchone.return_value = (False,)
        self.model.process_export_stock_queue_data()
        self.assertEqual(self.line.state, 'draft')
        self.urlopen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
