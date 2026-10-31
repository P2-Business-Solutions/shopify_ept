"""Bounded source GETs. No Shopify SDK state, persistence or business writes."""
import json
import re
import time
import subprocess
import sys
from urllib.parse import parse_qsl, urlsplit

import requests


class SourceReadError(Exception):
    def __init__(self, code, retry_after=None, partial=None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.partial = partial


class SourceReader:
    MAX_BYTES = 2 * 1024 * 1024
    MAX_TOTAL_BYTES = 8 * 1024 * 1024
    MAX_PAGES = 5

    def __init__(self, session=None, clock=time.monotonic, budget=20):
        self.session = session
        if self.session is not None:
            self.session.trust_env = False
        self.clock = clock
        self.deadline = clock() + min(budget, 20)
        self.total_bytes = 0

    def close(self):
        if self.session is not None:
            self.session.close()

    def _json(self, url, *, auth=None, headers=None, params=None, read_timeout=8):
        if self.session is None:
            return self._isolated_json(url, headers=headers, params=params, read_timeout=read_timeout)
        return self._direct_json(url, auth=auth, headers=headers, params=params, read_timeout=read_timeout)

    def _isolated_json(self, url, *, headers, params, read_timeout):
        """A synchronous, reaped I/O process bounds DNS and headers as well.

        requests' socket timeout alone does not impose a whole-request deadline.
        Credentials travel through a private pipe, never argv, a file or a log.
        """
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise SourceReadError('SOURCE_BUDGET_EXCEEDED')
        payload = {'url': url, 'headers': headers, 'params': params, 'read_timeout': read_timeout, 'budget': remaining}
        try:
            result = subprocess.run([sys.executable, __file__, '--source-get'],
                input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=remaining, check=False,
                env={'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1', 'LANG': 'C.UTF-8'})
        except subprocess.TimeoutExpired:
            raise SourceReadError('SOURCE_BUDGET_EXCEEDED') from None
        except OSError:
            raise SourceReadError('SOURCE_TRANSPORT_UNAVAILABLE') from None
        if result.returncode or len(result.stdout) > 2 * self.MAX_BYTES + 65536:
            raise SourceReadError('SOURCE_FETCH_FAILED')
        try:
            response = json.loads(result.stdout)
            if response.get('errorCode'):
                raise SourceReadError(response['errorCode'], response.get('retryAfter'))
            self.total_bytes += int(response['bytes'])
            if self.total_bytes > self.MAX_TOTAL_BYTES:
                raise SourceReadError('SOURCE_TRUNCATED')
            return response['body'], response['link']
        except (ValueError, KeyError, TypeError):
            raise SourceReadError('SOURCE_FETCH_FAILED') from None

    def _direct_json(self, url, *, auth=None, headers=None, params=None, read_timeout=8):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise SourceReadError('SOURCE_BUDGET_EXCEEDED')
        try:
            with self.session.get(url, auth=auth, headers=headers, params=params,
                                  timeout=(min(3, remaining), min(8, read_timeout, remaining)),
                                  allow_redirects=False, stream=True) as response:
                if response.status_code == 429:
                    raw = response.headers.get('Retry-After', '')
                    retry = min(int(raw), 300) if str(raw).isdigit() else None
                    raise SourceReadError('SOURCE_RATE_LIMITED', retry)
                if response.status_code in (401, 403):
                    raise SourceReadError('SOURCE_AUTH_REQUIRED')
                if response.status_code != 200:
                    raise SourceReadError('SOURCE_FETCH_FAILED')
                data = bytearray()
                # Single-byte yields also bound slow trickle responses; a large
                # buffered read could reset its socket timeout indefinitely.
                for chunk in response.iter_content(chunk_size=1):
                    if self.clock() >= self.deadline:
                        raise SourceReadError('SOURCE_BUDGET_EXCEEDED')
                    data.extend(chunk)
                    self.total_bytes += len(chunk)
                    if len(data) > self.MAX_BYTES or self.total_bytes > self.MAX_TOTAL_BYTES:
                        raise SourceReadError('SOURCE_TRUNCATED')
                return json.loads(data, parse_float=str), response.headers.get('Link', '')
        except (requests.RequestException, ValueError, UnicodeError):
            raise SourceReadError('SOURCE_FETCH_FAILED') from None

    @staticmethod
    def _next_url(links, origin, path):
        parsed = requests.utils.parse_header_links(links) if links else []
        if any(v.get('rel') not in ('next', 'previous') or not v.get('url') for v in parsed):
            raise SourceReadError('SOURCE_PAGINATION_INVALID')
        nexts = [v.get('url') for v in parsed if v.get('rel') == 'next']
        if len(nexts) > 1:
            raise SourceReadError('SOURCE_PAGINATION_INVALID')
        if not nexts:
            return None
        value = nexts[0]
        u = urlsplit(value)
        query = parse_qsl(u.query, keep_blank_values=True)
        if (u.scheme != 'https' or u.netloc != urlsplit(origin).netloc or u.path != path
                or u.username or u.password or u.fragment or not query
                or len({key for key, _ in query}) != len(query)
                or any(key not in ('page_info', 'limit') for key, _ in query)
                or not any(key == 'page_info' and val for key, val in query)
                or any(key == 'limit' and val != '250' for key, val in query)):
            raise SourceReadError('SOURCE_PAGINATION_INVALID')
        return value

    def shopify(self, host, password, order_id, version='2026-01'):
        if not re.fullmatch(r'[1-9][0-9]{0,29}', str(order_id)):
            raise SourceReadError('SOURCE_ID_INVALID')
        origin = 'https://' + host.removeprefix('https://').rstrip('/')
        u = urlsplit(origin)
        if (not u.hostname or u.username or u.password or u.port not in (None, 443)
                or u.path or u.query or u.fragment or not u.hostname.endswith('.myshopify.com')
                or not re.fullmatch(r'20[0-9]{2}-(01|04|07|10)', version)):
            raise SourceReadError('SOURCE_CONFIG_INVALID')
        base = f'{origin}/admin/api/{version}/orders/{order_id}'
        if not password:
            raise SourceReadError('SOURCE_AUTH_REQUIRED')
        headers = {'X-Shopify-Access-Token': password}
        body, _ = self._json(base + '.json', headers=headers)
        order = body.get('order') if isinstance(body, dict) else None
        if (not isinstance(order, dict) or str(order.get('id')) != str(order_id)
                or not isinstance(order.get('refunds'), list)
                or not isinstance(order.get('line_items'), list)):
            raise SourceReadError('SOURCE_ORDER_INCOMPLETE')
        for refund in order['refunds']:
            if (not isinstance(refund, dict) or not refund.get('id')
                    or any(not isinstance(refund.get(key), list) for key in ('refund_line_items', 'transactions'))):
                raise SourceReadError('SOURCE_REFUND_INCOMPLETE')
            if any(not isinstance(tx, dict) or not tx.get('id') for tx in refund['transactions']):
                raise SourceReadError('SOURCE_REFUND_INCOMPLETE')
        path = f'/admin/api/{version}/orders/{order_id}/transactions.json'
        url, params, transactions, seen = origin + path, {'limit': 250}, [], set()
        page_count = 0
        try:
            for page in range(1, self.MAX_PAGES + 1):
                if url in seen:
                    raise SourceReadError('SOURCE_PAGINATION_INVALID')
                seen.add(url)
                body, links = self._json(url, headers=headers, params=params)
                rows = body.get('transactions') if isinstance(body, dict) else None
                if (not isinstance(rows, list) or len(rows) > 250
                        or any(not isinstance(row, dict) or not row.get('id') for row in rows)):
                    raise SourceReadError('SOURCE_TRANSACTION_INCOMPLETE')
                page_count = page
                transactions.extend(rows)
                if len({str(row['id']) for row in transactions}) != len(transactions):
                    raise SourceReadError('SOURCE_TRANSACTION_DUPLICATE')
                url = self._next_url(links, origin, path)
                if not url:
                    documented = {str(tx['id']) for refund in order['refunds'] for tx in refund['transactions']}
                    if not documented.issubset({str(tx['id']) for tx in transactions}):
                        raise SourceReadError('SOURCE_TRANSACTION_INCOMPLETE')
                    return order, transactions, {'transactionPagesFetched': page, 'transactionCount': len(transactions),
                                                 'transactionsComplete': True, 'refundDocuments': 'single_resource_validated'}
                params = None
            raise SourceReadError('SOURCE_TRUNCATED')
        except SourceReadError as error:
            error.partial = (order, transactions, {'transactionPagesFetched': page_count, 'transactionCount': len(transactions), 'transactionsComplete': False, 'refundDocuments': 'single_resource_validated'})
            raise

    def loop(self, api_key, return_id, request_timeout=8):
        if not re.fullmatch(r'[1-9][0-9]{0,29}', str(return_id)):
            raise SourceReadError('LOOP_RETURN_ID_UNSUPPORTED')
        if not api_key:
            raise SourceReadError('SOURCE_AUTH_REQUIRED')
        body, _ = self._json('https://api.loopreturns.com/api/v1/warehouse/return/details',
                             headers={'X-Authorization': api_key},
                             params={'return_id': return_id, 'currency_type': 'shop'},
                             read_timeout=request_timeout if isinstance(request_timeout, (int, float)) and request_timeout > 0 else 8)
        rows = body if isinstance(body, list) else (body.get('returns', [body]) if isinstance(body, dict) else [])
        if (len(rows) != 1 or not isinstance(rows[0], dict) or str(rows[0].get('id')) != str(return_id)
                or not isinstance(rows[0].get('line_items'), list)):
            raise SourceReadError('LOOP_IDENTITY_MISMATCH')
        return rows[0]


def _source_get_main():
    """Internal network-only helper: no Odoo imports, ORM, SDK or writes."""
    reader = None
    try:
        raw = sys.stdin.buffer.read(131073)
        if len(raw) > 131072:
            raise SourceReadError('SOURCE_CONFIG_INVALID')
        request = json.loads(raw)
        if set(request) != {'url', 'headers', 'params', 'read_timeout', 'budget'}:
            raise SourceReadError('SOURCE_CONFIG_INVALID')
        reader = SourceReader(session=requests.Session(), budget=request['budget'])
        body, link = reader._direct_json(request['url'], headers=request['headers'], params=request['params'],
                                         read_timeout=request['read_timeout'])
        result = {'body': body, 'link': link, 'bytes': reader.total_bytes}
    except SourceReadError as error:
        result = {'errorCode': error.code, 'retryAfter': error.retry_after}
    except Exception:
        result = {'errorCode': 'SOURCE_FETCH_FAILED', 'retryAfter': None}
    finally:
        if reader:
            reader.close()
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode())


if __name__ == '__main__':
    if sys.argv[1:] != ['--source-get']:
        raise SystemExit(2)
    _source_get_main()
