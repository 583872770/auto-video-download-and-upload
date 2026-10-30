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
                               (HTTPError('https://youtube.com/', 429, 'PRIVATE', {}, None), 'verification_required')]:
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


class AuthContract:
    def cookie(self, content=COOKIE):
        result = self.client.post('/api/youtube/cookie', data={'cookie': (BytesIO(content), 'cookies.txt')}, headers=self.headers)
        self.assertEqual(result.status_code, 201, result.json)
        from Utils.config import load_config
        return load_config(self.config)[0]

    def post(self, path, data=None):
        return self.client.post(path, json=data, headers=self.headers)

    def test_missing_or_anonymous_cookie_blocks_before_creating_jobs(self):
        self.assertEqual(self.client.post('/api/youtube/auth/check').status_code, 403)
        response = self.post('/api/downloads', {'urls': 'npNnbj46_ik'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['auth']['state'], 'missing')
        self.assertEqual(self.store.counts()['total'], 0)
        self.assertTrue(self.store.downloads_paused())
        self.cookie(COOKIE.replace(b'SID', b'PREF'))
        with patch('webapp.youtube_auth.probe') as check:
            response = self.post('/api/youtube/auth/check')
            check.assert_not_called()
        self.assertEqual(response.json['auth']['state'], 'not_logged_in')
        (self.root / 'Cookies/youtube.txt').write_bytes(COOKIE.replace(b'\t0\tSID', b'\t1\tSID'))
        with patch('webapp.youtube_auth.probe') as check:
            response = self.post('/api/youtube/auth/check')
            check.assert_not_called()
        self.assertEqual(response.json['auth']['state'], 'invalid')

    def test_failed_resume_retains_jobs_and_verified_resume_does_not_consume_attempts(self):
        task = self.store.create('download', {'config': {}, 'base': str(self.root), 'urls': ['https://youtu.be/npNnbj46_ik']})
        self.store.set_downloads_paused(True)
        self.cookie()
        with patch('webapp.youtube_auth.probe', return_value=('not_logged_in', 'Fixture not logged in')):
            response = self.post('/api/downloads/resume')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.download_queue(), {'paused': True, 'queued': 1, 'running': 0, 'waiting_lists': 0, 'importing': 0})
        self.assertEqual(self.store.get(task['id'])['attempts'], 0)
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'Fixture verified')):
            self.assertEqual(self.post('/api/downloads/resume').status_code, 200)
        self.assertFalse(self.store.downloads_paused())
        self.assertEqual(self.store.get(task['id'])['status'], 'queued')

    def test_unexpected_probe_error_blocks_without_exposing_details(self):
        self.cookie()
        with patch('webapp.youtube_auth.probe', side_effect=RuntimeError('PRIVATE_COOKIE_DETAIL')):
            response = self.post('/api/downloads', {'urls': 'npNnbj46_ik'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['auth']['state'], 'unconfirmed')
        self.assertNotIn('PRIVATE_COOKIE_DETAIL', response.text)
        self.assertEqual(self.store.counts()['total'], 0)
        self.assertTrue(self.store.downloads_paused())

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
            self.assertEqual(network.call_count, 5)
        public = self.client.get('/api/status').text
        self.assertNotIn('TEST_ONLY_COOKIE_SECRET', public)
        self.assertNotIn('UPDATED_FIXTURE', public)

    def test_worker_confirmed_login_failure_requeues_and_stops_without_download_or_failure(self):
        import yt_dlp
        from web_worker import drain
        config = self.cookie()
        task = self.store.create('download', {'config': config, 'base': str(self.root),
                                             'urls': ['https://www.youtube.com/watch?v=npNnbj46_ik']})
        with patch('webapp.youtube_auth.probe', return_value=('not_logged_in', 'Fixture login rejected')):
            with patch('web_worker.os.dup2'), patch.object(yt_dlp.YoutubeDL, 'download') as download:
                drain(self.store.directory)
                download.assert_not_called()
        current = self.store.get(task['id'])
        self.assertEqual((current['status'], current['attempts']), ('queued', 0))
        self.assertEqual(current['progress']['stage'], 'checking_login')
        self.assertEqual(self.store.download_queue(), {'paused': True, 'queued': 1, 'running': 0, 'waiting_lists': 0, 'importing': 0})
        self.assertEqual(self.client.get('/api/tasks?view=history').json['items'], [])

    def test_worker_network_preflight_recovers_without_manual_restart_or_consuming_attempts(self):
        import yt_dlp
        from web_worker import drain
        config = self.cookie()
        task = self.store.create('download', {'config': config, 'base': str(self.root),
                                             'urls': ['https://youtu.be/npNnbj46_ik']})
        with patch('webapp.youtube_auth.probe', return_value=('network_error', 'Fixture timeout')), \
                patch('web_worker.os.dup2'), patch.object(yt_dlp.YoutubeDL, 'download') as download:
            drain(self.store.directory)
            download.assert_not_called()
        current = self.store.get(task['id'])
        self.assertEqual((current['status'], current['attempts']), ('queued', 0))
        self.assertEqual(current['progress']['stage'], 'retrying')
        self.assertGreater(current['available_at'], time.time() + 20)
        self.assertFalse(self.store.downloads_paused())
        self.assertIsNone(YouTubeAuth(self.store).block_reason())
        after_retry = time.time() + 31
        with patch('webapp.youtube_auth.time.time', return_value=after_retry), \
                patch('webapp.youtube_auth.probe', return_value=('valid', 'Fixture recovered')), \
                patch('webapp.downloader.require_download_tools'), patch('web_worker.os.dup2'), \
                patch.object(yt_dlp.YoutubeDL, 'download', return_value=0) as download:
            drain(self.store.directory)
            download.assert_called_once()
        self.assertEqual(self.store.get(task['id'])['status'], 'succeeded')
        self.assertFalse(self.store.downloads_paused())

    def test_repeated_proxy_preflight_failures_cool_the_route_without_invalidating_cookie(self):
        from web_worker import execute
        from webapp.clash import ClashManager
        from webapp.proxy_health import ProxyHealth
        config = {**self.cookie(), 'proxy_mode': 'clash'}
        manager = self.app.extensions['clash']
        snapshot = {'directory': str(manager.directory), 'node': 'Bad', 'proxy': manager.proxy_url}
        with patch.object(ClashManager, 'download_snapshot', return_value=snapshot), \
                patch('webapp.youtube_auth.probe', return_value=('network_error', 'Fixture timeout')):
            for _ in range(3):
                with self.assertRaises(YouTubeAuthRequired):
                    execute('download', {'config': config, 'base': str(self.root), 'clash': snapshot,
                                        'urls': ['https://youtu.be/npNnbj46_ik']}, store=self.store)
        health = ProxyHealth(manager.directory)
        self.assertFalse(health.eligible('Bad', 'yt_dlp'))
        self.assertEqual(health.select(['Bad', 'Good'], providers=['yt_dlp']), 'Good')
        self.assertFalse(self.store.downloads_paused())
        self.assertIsNone(YouTubeAuth(self.store).block_reason())

    def test_login_verified_before_extractor_and_cookie_rejection_stops_later_jobs(self):
        import yt_dlp
        from web_worker import execute
        config = self.cookie()
        events = []

        def positive(*args):
            events.append('login')
            return 'valid', 'Fixture verified'

        def download(downloader, urls):
            events.append('download')
            downloader.report_warning('The provided YouTube account cookies are no longer valid.')
            return 1

        with patch('webapp.youtube_auth.probe', side_effect=positive), patch('webapp.downloader.require_download_tools'):
            with patch.object(yt_dlp.YoutubeDL, 'download', download):
                with self.assertRaises(YouTubeAuthRequired):
                    execute('download', {'config': config, 'base': str(self.root), 'urls': ['https://youtu.be/npNnbj46_ik']}, store=self.store)
        self.assertEqual(events, ['login', 'download'])
        self.assertEqual(YouTubeAuth(self.store).status()['state'], 'verification_required')


class SQLiteFixture(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


class SQLiteAuthTests(AuthContract, SQLiteFixture):
    pass




