"""Positive login evidence, cached preflight and lossless queue blocking."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import ssl
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError, HTTPError

from tests import test_scale
from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired, logged_in, probe

COOKIE = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tTEST_ONLY_COOKIE_SECRET\n'


class AuthParserTests(unittest.TestCase):
    def test_requires_explicit_server_login_flag(self):
        for document, expected in [('<script>ytcfg.set({"LOGGED_IN":true})</script>', True),
                                   ('ytcfg.set({"other":1});ytcfg.set( {"LOGGED_IN":false});', False),
                                   ('ytcfg.set({"LOGGED_IN":"true"})', None),
                                   ('ytcfg.set(broken);ytcfg.set({"LOGGED_IN":true});', True),
                                   ('<div id="avatar-btn">Signed in</div>', None)]:
            with self.subTest(document=document):
                self.assertIs(logged_in(document), expected)

    def test_probe_uses_cookie_jar_current_proxy_and_does_not_mutate_source(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = 'https://www.youtube.com/feed/history?hl=en'
        response.read.return_value = b'ytcfg.set({"LOGGED_IN":true})'
        opener = Mock()
        opener.open.return_value = response
        with patch('webapp.youtube_auth.build_opener', return_value=opener) as build:
            self.assertEqual(probe(COOKIE, 'http://127.0.0.1:18888')[0], 'valid')
            proxy_handler, cookie_handler = build.call_args.args
            self.assertEqual(proxy_handler.proxies['https'], 'http://127.0.0.1:18888')
            cookies = list(cookie_handler.cookiejar)
            self.assertEqual(len(cookies), 1)
            self.assertIsNone(cookies[0].expires)  # Netscape 0 means a session cookie.
            self.assertTrue(cookies[0].secure)
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 15)

    def test_network_restrictions_are_not_reported_as_cookie_expiry(self):
        for failure, state in [(URLError('http://user:PRIVATE@host'), 'network_error'),
                               (HTTPError('https://youtube.com/', 429, 'PRIVATE', {}, None), 'rate_limited')]:
            opener = Mock()
            opener.open.side_effect = failure
            with patch('webapp.youtube_auth.build_opener', return_value=opener):
                result = probe(COOKIE, '')
            self.assertEqual(result[0], state)
            self.assertNotIn('PRIVATE', result[1])
            self.assertNotIn('过期', result[1])

    def test_transient_probe_diagnostics_identify_cause_without_disclosing_proxy_or_cookie(self):
        for failure, message, fields in [
                (URLError(TimeoutError('PRIVATE')), '连接超时', {'cause_type': 'TimeoutError'}),
                (URLError(ssl.SSLCertVerificationError('PRIVATE')), '证书校验失败',
                 {'cause_type': 'SSLCertVerificationError'}),
                (HTTPError('https://youtube.com/?token=PRIVATE', 503, 'PRIVATE', {}, None),
                 'HTTP 503', {'http_status': 503})]:
            with self.subTest(failure=type(failure).__name__, message=message):
                opener = Mock()
                opener.open.side_effect = failure
                with patch('webapp.youtube_auth.build_opener', return_value=opener), \
                        patch('webapp.diagnostics.event') as event:
                    state, description = probe(COOKIE, 'http://user:PRIVATE@proxy.example:1234')
                self.assertEqual(state, 'network_error')
                self.assertIn(message, description)
                self.assertNotIn('PRIVATE', description)
                event.assert_called_once()
                self.assertEqual(event.call_args.args, ('youtube_auth_probe_unavailable',))
                self.assertTrue(event.call_args.kwargs['proxy_configured'])
                for key, value in fields.items():
                    self.assertEqual(event.call_args.kwargs[key], value)
                diagnostic = json.dumps(event.call_args.kwargs)
                self.assertNotIn('PRIVATE', diagnostic)
                self.assertNotIn('TEST_ONLY_COOKIE_SECRET', diagnostic)

    def test_probe_403_preserves_status_without_claiming_robot_detection(self):
        opener = Mock()
        opener.open.side_effect = HTTPError('https://youtube.com/?token=PRIVATE', 403, 'PRIVATE', {}, None)
        with patch('webapp.youtube_auth.build_opener', return_value=opener):
            result = probe(COOKIE, '')
        self.assertEqual(result[0], 'verification_required')
        self.assertEqual(result.http_status, 403)
        self.assertIn('具体原因尚未确认', result[1])
        self.assertNotIn('PRIVATE', result[1])
        self.assertNotIn('机器人', result[1])

    def test_probe_redirect_is_distinct_from_a_recorded_403(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.geturl.return_value = 'https://accounts.google.com/?token=PRIVATE'
        opener = Mock()
        opener.open.return_value = response
        with patch('webapp.youtube_auth.build_opener', return_value=opener):
            result = probe(COOKIE, '')
        self.assertEqual(result[0], 'verification_required')
        self.assertEqual(result.verification_reason, 'redirect')
        self.assertIsNone(result.http_status)
        self.assertNotIn('PRIVATE', result[1])


class AuthContract:
    def cookie(self, content=COOKIE):
        from tests.account_cookie_fixture import AccountCookieProfiles
        AccountCookieProfiles(self.root, self.store).seed_default(content)
        result = self.client.put('/api/config', json={'youtube_cookie_pool': ['d'*32]}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        return {**self.app.extensions['task_store'].runtime(), 'youtube_cookie': str(self.root / 'Cookies/profiles' / ('d'*32 + '.txt'))}

    def post(self, path, data=None):
        return self.client.post(path, json=data, headers=self.headers)




    def test_cache_is_shared_between_workers_and_changes_with_cookie_and_node(self):
        config = self.cookie()

        def positive(*args):
            time.sleep(0.05)
            return 'valid', 'Fixture verified'

        with patch('webapp.youtube_auth.probe', side_effect=positive) as network:
            def check(_):
                return YouTubeAuth(self.store).check(config, self.root, proxy='http://127.0.0.1:18888', node='A')
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(check, range(8)))
            self.assertTrue(all(result['state'] == 'valid' for result in results))
            self.assertEqual(network.call_count, 1)
            auth = YouTubeAuth(self.store)
            auth.require(config, self.root, proxy='http://127.0.0.1:18888', node='B')
            self.assertEqual(network.call_count, 2)
            auth.require(config, self.root, proxy='http://127.0.0.1:18888', node='B', force=True)
            self.assertEqual(network.call_count, 3)
            self.cookie(COOKIE.replace(b'TEST_ONLY_COOKIE_SECRET', b'UPDATED_FIXTURE'))
            auth.require(config, self.root, proxy='http://127.0.0.1:18888', node='B')
            self.assertEqual(network.call_count, 4)
            with patch('webapp.youtube_auth.time.time', return_value=time.time() + 301):
                auth.require(config, self.root, proxy='http://127.0.0.1:18888', node='B')
            self.assertEqual(network.call_count, 4)
            with patch('webapp.youtube_auth.time.time', return_value=time.time() + 1801):
                auth.require(config, self.root, proxy='http://127.0.0.1:18888', node='B')
            self.assertEqual(network.call_count, 5)
        public = self.client.get('/api/status').text
        self.assertNotIn('TEST_ONLY_COOKIE_SECRET', public)
        self.assertNotIn('UPDATED_FIXTURE', public)







class SQLiteFixture(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


class SQLiteAuthTests(AuthContract, SQLiteFixture):
    pass
