"""Exercise the actual paging method with SDK-shaped pages, without Odoo."""

import ast
from pathlib import Path
import unittest


source = ast.parse((Path(__file__).resolve().parents[1] / 'models' / 'shopify_payout_report_ept.py').read_text())
model = next(node for node in source.body if isinstance(node, ast.ClassDef))
method = next(node for node in model.body if isinstance(node, ast.FunctionDef)
              and node.name == 'shopify_list_all_transactions')
namespace = {}
exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), '<payout paging>', 'exec'), namespace)
all_pages = namespace['shopify_list_all_transactions']


class Page(list):
    def __init__(self, rows, following=None):
        super().__init__(rows)
        self.following = following

    def has_next_page(self):
        return self.following is not None

    def next_page(self, no_cache=False):
        return self.following


class TestShopifyPayoutImport(unittest.TestCase):
    def test_exactly_250_without_next_link_keeps_every_transaction(self):
        self.assertEqual(all_pages(None, Page(range(250))), list(range(250)))

    def test_first_middle_and_last_pages_are_included_once(self):
        pages = Page(range(250), Page(range(250, 500), Page(range(500, 507))))
        self.assertEqual(all_pages(None, pages), list(range(507)))

    def test_plain_collection_and_empty_collection(self):
        self.assertEqual(all_pages(None, [1, 2]), [1, 2])
        self.assertEqual(all_pages(None, []), [])

