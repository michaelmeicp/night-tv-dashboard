import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import refresh_public_data as refresh

NOW = datetime(2026, 11, 1, 2, tzinfo=timezone.utc)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.html = (Path(__file__).resolve().parents[1] / 'index.html').read_text()
        self.snapshot = json.loads(refresh.SNAPSHOT.search(self.html)[2])

    def test_all_fail_retains_observations_and_success_times(self):
        previous = copy.deepcopy(self.snapshot)
        previous['refresh'] = {'sources': {'crypto_quotes': {'last_success_at': '2026-10-30T19:55:24Z'}}}
        def failed(url):
            raise URLError('offline')
        result = refresh.refresh(previous, failed, NOW)
        for name in ['market', 'watchlist', 'weather']:
            for before, after in zip(previous['queries'][name]['rows'], result['queries'][name]['rows']):
                for field in ['value', 'quote_timestamp', 'fetch_timestamp']:
                    self.assertEqual(before.get(field), after.get(field))
        self.assertEqual(result['refresh']['sources']['crypto_quotes']['last_success_at'], '2026-10-30T19:55:24Z')
        self.assertEqual(result['refresh']['sources']['crypto_quotes']['status'], 'error')
        self.assertEqual(result['refresh']['sources']['crypto_quotes']['error'], 'network_error')
        for key, query in previous['queries'].items():
            if key.startswith('history_'):
                self.assertEqual(query, result['queries'][key])

    def test_http_denial_is_not_retried(self):
        calls = []
        def denied(url):
            calls.append(url)
            raise HTTPError(url, 403, 'Forbidden', {}, None)
        result = refresh.refresh(self.snapshot, denied, NOW)
        self.assertEqual(calls.count(refresh.QUOTE_URL), 1)
        self.assertEqual(result['refresh']['sources']['crypto_quotes']['error'], 'http_403')

    def test_partial_quote_response_is_atomic(self):
        payload = {'bitcoin': {'usd': 80000, 'usd_24h_change': 1, 'last_updated_at': NOW.timestamp()}}
        def partial(url):
            if url == refresh.QUOTE_URL:
                return json.dumps(payload).encode()
            raise URLError('offline')
        result = refresh.refresh(self.snapshot, partial, NOW)
        self.assertEqual(self.snapshot['queries']['market']['rows'][0]['value'], result['queries']['market']['rows'][0]['value'])
        self.assertEqual(result['refresh']['sources']['crypto_quotes']['error'], 'invalid_data')

    def test_quote_success_changes_only_valid_target_rows(self):
        payload = {key: {'usd': value, 'usd_24h_change': 0, 'last_updated_at': NOW.timestamp() - 60} for key, value in [('bitcoin', 80000), ('binancecoin', 700)]}
        def source(url):
            if url == refresh.QUOTE_URL:
                return json.dumps(payload).encode()
            raise URLError('offline')
        result = refresh.refresh(self.snapshot, source, NOW)
        self.assertEqual(result['queries']['market']['rows'][0]['value'], 80000)
        self.assertEqual(result['queries']['market']['rows'][0]['quote_timestamp'], '2026-11-01T01:59:00Z')
        self.assertEqual(result['queries']['market']['rows'][0]['fetch_timestamp'], '2026-11-01T02:00:00Z')
        self.assertEqual(result['queries']['watchlist'], self.snapshot['queries']['watchlist'] | {'source': result['queries']['watchlist']['source']})
        self.assertEqual(result['refresh']['sources']['crypto_quotes']['status'], 'success')

    def test_bad_prices_and_future_times_rejected(self):
        for value in [float('nan'), float('inf'), True, 0, -2, '80000']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                refresh.crypto_quote({'bitcoin': {'usd': value, 'usd_24h_change': 0, 'last_updated_at': NOW.timestamp()}}, 'bitcoin', NOW)
        with self.assertRaises(ValueError):
            refresh.crypto_quote({'bitcoin': {'usd': 80000, 'usd_24h_change': 0, 'last_updated_at': NOW.timestamp() + 86400}}, 'bitcoin', NOW)

    def test_crypto_duplicate_dates_rejected(self):
        point = [NOW.timestamp() * 1000 - 86400000, 100]
        with self.assertRaises(ValueError):
            refresh.crypto_history({'prices': [point, point, point]}, NOW)

    def test_wrong_currency_and_duplicate_fx_rejected(self):
        with self.assertRaises(ValueError):
            refresh.fx_history({'base': 'EUR', 'rates': {}}, 'HKD', NOW)
        payload = [{'base': 'USD', 'quote': 'HKD', 'date': '2026-10-30', 'rate': 8}]
        with self.assertRaises(ValueError):
            refresh.fx_history(payload, 'MOP', NOW)
        payload = [{'base': 'USD', 'quote': 'MOP', 'date': '2026-10-30', 'rate': 8}] * 3
        with self.assertRaises(ValueError):
            refresh.fx_history(payload, 'MOP', NOW)

    def test_fx_window_and_dates_are_real_observations(self):
        payload = {'base': 'USD', 'rates': {day: {'HKD': value} for day, value in [('2026-09-30', 7.8), ('2026-10-28', 7.81), ('2026-10-29', 7.82), ('2026-10-30', 7.83)]}}
        rows = refresh.fx_history(payload, 'HKD', NOW)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['date'], '2026-10-28')
        self.assertEqual(rows[-1]['value'], 7.83)

    def test_rebuild_updates_hash_and_keeps_runtime_identical(self):
        result = refresh.rebuild(self.html, self.snapshot)
        import hashlib
        encoded = refresh.SNAPSHOT.search(result)[2]
        digest = re.search(r'<meta name="data-app-snapshot-sha256" content="([^"]+)', result)[1]
        self.assertEqual(digest, hashlib.sha256(encoded.encode()).hexdigest())
        # The immutable bundled runtime and generated mounting code are untouched.
        before = refresh.SNAPSHOT.sub('SNAPSHOT', self.html)
        after = refresh.SNAPSHOT.sub('SNAPSHOT', result)
        before = re.sub(r'data-app-snapshot-sha256" content="[^"]+', 'HASH', before)
        after = re.sub(r'data-app-snapshot-sha256" content="[^"]+', 'HASH', after)
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
