"""Connectivity diagnostics distinguish HTTP restrictions from transport errors."""
from io import BytesIO
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from webapp.clash import ClashError, ClashManager


class YouTubeConnectivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manager = ClashManager(temporary.name)
        snapshot = patch.object(self.manager, 'snapshot', return_value={
            'proxy': 'http://127.0.0.1:1234', 'node': 'Fixture Node'})
        snapshot.start()
        self.addCleanup(snapshot.stop)

    def test_http_restrictions_are_not_reported_as_offline_nodes(self):
        for code, expected in ((403, '访问被拒绝'), (429, '限流'), (503, '测试请求未通过')):
            with self.subTest(code=code):
                error = HTTPError('https://www.youtube.com/generate_204', code,
                                  'PRIVATE_UPSTREAM_DETAIL', {}, BytesIO(b'PRIVATE_RESPONSE'))
                opener = MagicMock()
                opener.open.side_effect = error
                with patch('webapp.clash.build_opener', return_value=opener):
                    with self.assertRaises(ClashError) as result:
                        self.manager.test_youtube()
                self.assertIn(f'HTTP {code}', str(result.exception))
                self.assertIn(expected, str(result.exception))
                self.assertIn('不代表节点离线', str(result.exception))
                self.assertNotIn('PRIVATE', str(result.exception))

    def test_transport_errors_identify_timeout_tls_or_server_path_without_leaking_details(self):
        cases = [(TimeoutError('PRIVATE'), '超时'), (URLError(TimeoutError('PRIVATE')), '超时'),
                 (URLError(ssl.SSLCertVerificationError('PRIVATE')), '证书'),
                 (URLError('PRIVATE_PROXY_CREDENTIALS'), '服务端代理')]
        for error, expected in cases:
            with self.subTest(expected=expected), patch('webapp.clash.build_opener') as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(ClashError) as result:
                    self.manager.test_youtube()
                self.assertIn(expected, str(result.exception))
                self.assertNotIn('PRIVATE', str(result.exception))

    def test_success_still_reports_node_and_latency_through_managed_proxy(self):
        with patch('webapp.clash.build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.status = 204
            result = self.manager.test_youtube()
        self.assertTrue(result['ok'])
        self.assertEqual(result['node'], 'Fixture Node')
        self.assertGreaterEqual(result['ms'], 0)
