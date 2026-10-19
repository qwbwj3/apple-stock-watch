"""Offline tests: no Apple/server calls, user config or notification delivery."""
import copy
import io
import json
import math
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import public_cache as m

NOW = 1780000000
CFG = {'stores': ['R609'], 'cities': ['大连'], 'parts': dict(m.SUPPORTED_PARTS)}


def snapshot():
    return {'schemaVersion': 1, 'source': 'apple-stock-public-cache',
            'generatedAtMs': NOW * 1000, 'healthy': True, 'workerAlive': True,
            'cycles': 2, 'freshnessLimitSeconds': 120,
            'snapshot': [
                {'target': {'locale': 'zh_CN', 'storeNumber': 'R609',
                            'partNumber': part, 'storeTitle': '大连恒隆广场',
                            'productName': name},
                 'availability': {'kind': 'in_stock' if i == 0 else 'out_of_stock'},
                 'lastCheckedMs': NOW * 1000}
                for i, (part, name) in enumerate(m.SUPPORTED_PARTS.items())]}


class Response(io.BytesIO):
    status = 200
    def geturl(self):
        return m.URL


class PublicCacheTests(unittest.TestCase):
    def test_full_conversion_and_metadata(self):
        stores = m.convert(snapshot(), CFG, NOW)
        self.assertEqual(stores[0]['number'], 'R609')
        self.assertEqual(len(stores[0]['parts']), 8)
        first = stores[0]['parts']['MK2Q4CH/A']
        self.assertEqual(first['state'], 'available')
        self.assertEqual(first['source'], 'public_cache')
        self.assertEqual(first['source_cycle'], 2)
        self.assertEqual(first['last_checked_ms'], NOW * 1000)
        self.assertEqual(stores[0]['parts']['MK2W4CH/A']['state'], 'unavailable')

    def test_supports_subsets_and_optional_city(self):
        cfg = copy.deepcopy(CFG)
        cfg['parts'] = {'MK2W4CH/A': 'name'}
        self.assertTrue(m.supports(cfg))
        self.assertEqual(set(m.convert(snapshot(), cfg, NOW)[0]['parts']), {'MK2W4CH/A'})
        cfg.pop('cities')
        self.assertTrue(m.supports(cfg))

    def test_unsupported_configs_never_read_network(self):
        for override in ({'stores': []}, {'stores': ['R609', 'R448']},
                         {'stores': ['R448']}, {'cities': ['北京']},
                         {'cities': '大连'}, {'cities': False}, {'parts': {}}, {'parts': {'bad': 'bad'}}):
            cfg = dict(CFG, **override)
            self.assertFalse(m.supports(cfg))
            with mock.patch.object(m, 'read_public') as read:
                with self.assertRaises(m.CacheError):
                    m.query(cfg)
                read.assert_not_called()

    def test_unhealthy_unknown_never_become_out_of_stock(self):
        for field, value in [('healthy', False), ('workerAlive', False),
                             ('source', 'other'), ('schemaVersion', True),
                             ('workerError', 'failed')]:
            data = snapshot()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(m.CacheError):
                m.convert(data, CFG, NOW)
        data = snapshot()
        data['snapshot'][7]['availability']['kind'] = 'unknown'
        cfg = dict(CFG, parts={'MK2Q4CH/A': 'one'})
        with self.assertRaises(m.CacheError):
            m.convert(data, cfg, NOW)

    def test_generated_timestamp_and_cycle_validation(self):
        for value in [True, None, '100', math.nan, math.inf,
                      (NOW - 45.001) * 1000, (NOW + 15.001) * 1000]:
            data = snapshot()
            data['generatedAtMs'] = value
            with self.subTest(value=value), self.assertRaises(m.CacheError):
                m.convert(data, CFG, NOW)
        for field, values in [('cycles', [-1, True, 1.5, math.inf, math.nan, '2']),
                              ('freshnessLimitSeconds', [True, 121, 120.0])]:
            for value in values:
                data = snapshot()
                data[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(m.CacheError):
                    m.convert(data, CFG, NOW)

    def test_row_timestamps_rejected_and_boundaries_accepted(self):
        for value in [True, None, '100', math.nan, math.inf,
                      (NOW - 120.001) * 1000, (NOW + 15.001) * 1000]:
            data = snapshot()
            data['snapshot'][0]['lastCheckedMs'] = value
            with self.subTest(value=value), self.assertRaises(m.CacheError):
                m.convert(data, CFG, NOW)
        data = snapshot()
        data['generatedAtMs'] = (NOW - 45) * 1000
        data['snapshot'][0]['lastCheckedMs'] = (NOW - 120) * 1000
        data['snapshot'][1]['lastCheckedMs'] = (NOW + 15) * 1000
        self.assertEqual(len(m.convert(data, CFG, NOW)), 1)

    def test_complete_exact_coverage_required(self):
        variants = []
        data = snapshot(); data['snapshot'].pop(); variants.append(data)
        data = snapshot(); data['snapshot'].append(copy.deepcopy(data['snapshot'][0])); variants.append(data)
        data = snapshot(); data['snapshot'][7] = copy.deepcopy(data['snapshot'][0]); variants.append(data)
        for field, value in [('locale', 'en_US'), ('storeNumber', 'R448'), ('partNumber', 'unexpected'),
                             ('storeTitle', None), ('productName', '')]:
            data = snapshot(); data['snapshot'][7]['target'][field] = value; variants.append(data)
        for data in variants:
            with self.assertRaises(m.CacheError):
                m.convert(data, CFG, NOW)

    def test_malformed_data_is_controlled_error(self):
        for data in [None, [], {}, {'snapshot': 'bad'}, snapshot()]:
            if data and data.get('schemaVersion'):
                data['snapshot'][0]['target'] = []
            with self.subTest(data=type(data).__name__), self.assertRaises(m.CacheError):
                m.convert(data, CFG, NOW)

    def test_https_proxy_and_redirect_controls(self):
        opener = mock.Mock()
        opener.open.return_value = Response(json.dumps(snapshot()).encode())
        with mock.patch.object(m.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(m.read_public()['cycles'], 2)
        handlers = build.call_args.args
        proxy = next(h for h in handlers if isinstance(h, urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})
        https = next(h for h in handlers if isinstance(h, urllib.request.HTTPSHandler))
        self.assertTrue(https._context.check_hostname)
        import ssl
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        redirect = next(h for h in handlers if isinstance(h, m._NoRedirect))
        self.assertIsNone(redirect.redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/'))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, m.URL)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)
        self.assertEqual(opener.open.call_count, 1)

    def test_response_size_limit(self):
        response = Response(b' ' * (m.MAX_RESPONSE_BYTES + 1))
        with mock.patch.object(m.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = response
            with self.assertRaisesRegex(m.CacheError, '64 KiB'):
                m.read_public()

    def test_redirect_and_429_no_retry_or_fallback(self):
        for code, text in [(302, '重定向'), (429, '不要立即重试'), (503, 'HTTP 503')]:
            error = urllib.error.HTTPError(m.URL, code, 'error', {}, None)
            with mock.patch.object(m.urllib.request, 'build_opener') as build:
                build.return_value.open.side_effect = error
                with self.assertRaisesRegex(m.CacheError, text):
                    m.read_public()
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_invalid_json_duplicate_fields_and_network_failure(self):
        for body in [b'not JSON', b'\xff', b'{"cycles": 1, "cycles": 2}']:
            with mock.patch.object(m.urllib.request, 'build_opener') as build:
                build.return_value.open.return_value = Response(body)
                with self.assertRaises(m.CacheError):
                    m.read_public()
        with mock.patch.object(m.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = Response(b'[]')
            with mock.patch.object(m.json, 'loads', side_effect=RecursionError):
                with self.assertRaises(m.CacheError):
                    m.read_public()
        with mock.patch.object(m.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = urllib.error.URLError('private details')
            with self.assertRaises(m.CacheError) as caught:
                m.read_public()
            self.assertNotIn('private details', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
