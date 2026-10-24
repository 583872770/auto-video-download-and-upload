"""Automatic provider fallback, lossless preflight and delivered byte accounting."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yt_dlp

from Download import download_cookie_session
from tests import test_yt5s
from tests.test_yt5s import VIDEO
from tests.test_mysql import MySQLFixture
from tests.test_youtube_auth import COOKIE
from webapp.downloads import DownloadLeaseLost
from webapp.environment import DownloadEnvironmentRequired, require_fallback_environment
from webapp.settings import validate_method_urls
from webapp.yt5s import BrowserEnvironmentRequired, WebsiteHTTPError
from web_worker import execute, drain


class FallbackContract:
    method = test_yt5s.Yt5sTests.method

    def cookie(self):
        response = self.client.post('/api/youtube/cookie', headers=self.headers,
            data={'cookie': (BytesIO(COOKIE), 'cookies.txt')})
        self.assertEqual(response.status_code, 201, response.json)

    def payload(self, **config):
        return {'config': {'path': 'videos', 'proxy_mode': 'direct', 'download_method': 'yt5s_fallback',
                           'youtube_cookie': 'Cookies/youtube.txt', **config},
                'base': str(self.root), 'urls': [VIDEO]}

    def test_selection_and_preflight_for_new_batch_import_and_resume(self):
        self.method('yt5s_fallback')
        self.assertEqual(self.client.get('/').text.count('value="yt5s_fallback"'), 2)
        self.cookie()
        for state in ('not_logged_in', 'network_error', 'valid'):
            with patch('webapp.youtube_auth.probe', return_value=(state, 'Fixture auth')) as check:
                response = self.client.post('/api/downloads', headers=self.headers,
                                            json={'urls': 'npNnbj46_ik\n2Gu7q-KUXCk'})
            self.assertEqual(response.status_code, 202 if state == 'valid' else 409, response.json)
            check.assert_called_once()  # one real preflight per batch, even after cached failure
        self.assertEqual(self.store.counts()['total'], 2)
        self.store.set_downloads_paused(True)
        self.method('yt5s')  # Existing jobs retain their automatic provider.
        with patch('webapp.youtube_auth.probe', return_value=('not_logged_in', 'Fixture expired')):
            response = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(response.status_code, 409, response.json)
        self.assertTrue(self.store.downloads_paused())
        self.assertEqual(self.store.download_queue()['queued'], 2)
        self.method('yt5s_fallback')
        with patch('webapp.youtube_auth.probe', return_value=('not_logged_in', 'Fixture expired')):
            response = self.client.post('/api/downloads/import', headers=self.headers,
                data={'file': (BytesIO(b'npNnbj46_ik\n'), 'videos.txt')})
        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.store.directory / 'imports').exists())

    def test_bulk_import_checks_once_and_retains_provider(self):
        from webapp.bulk import run_import

        self.method('yt5s_fallback')
        self.cookie()
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'Fixture verified')) as check:
            response = self.client.post('/api/downloads/import', headers=self.headers,
                data={'file': (BytesIO(b'npNnbj46_ik\n2Gu7q-KUXCk\n'), 'videos.txt')})
            self.assertEqual(response.status_code, 202, response.json)
            task = self.store.get(response.json['id'], private=True)
            result = run_import(self.store, task['payload'], lambda value: None)
        check.assert_called_once()
        self.assertEqual(result['added'], 2)
        for task in self.store.page(actions=('download',))['items']:
            self.assertEqual(self.store.get(task['id'], private=True)['payload']['config']['download_method'], 'yt5s_fallback')

    def test_missing_tools_or_browser_blocks_batch_import_and_resume(self):
        self.method('yt5s_fallback')
        for failure in (DownloadEnvironmentRequired('missing FFmpeg'), BrowserEnvironmentRequired('missing display')):
            with self.subTest(error=type(failure)), patch('app.require_fallback_environment', side_effect=failure), \
                    patch('webapp.youtube_auth.probe') as check:
                response = self.client.post('/api/downloads', headers=self.headers, json={'urls': 'npNnbj46_ik'})
                self.assertEqual(response.status_code, 409, response.json)
                response = self.client.post('/api/downloads/import', headers=self.headers,
                    data={'file': (BytesIO(b'npNnbj46_ik'), 'videos.txt')})
                self.assertEqual(response.status_code, 409)
                check.assert_not_called()
        self.assertEqual(self.store.counts()['total'], 0)
        self.store.create('download', self.payload())
        self.store.set_downloads_paused(True)
        with patch('app.require_fallback_environment', side_effect=DownloadEnvironmentRequired('missing JS')):
            response = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.store.downloads_paused())

    def test_environment_checks_native_dependencies_and_custom_runtime(self):
        with patch('webapp.environment.shutil.which', return_value=None):
            with self.assertRaisesRegex(DownloadEnvironmentRequired, 'ffmpeg.*ffprobe.*JavaScript'):
                require_fallback_environment({}, self.root)
        with patch('webapp.environment.shutil.which', side_effect=lambda name: name if name in ('ffmpeg', 'ffprobe', '/fixture/node') else None):
            require_fallback_environment({'js_runtimes': {'node': {'path': '/fixture/node'}}}, self.root)
            with self.assertRaises(DownloadEnvironmentRequired):
                require_fallback_environment({'js_runtimes': {}}, self.root)
        for url in ('https://example.test/video.mp4', 'https://www.youtube.com/playlist?list=PLfixture'):
            with self.assertRaises(ValueError):
                validate_method_urls({'download_method': 'yt5s_fallback'}, [url])

    def test_worker_preflight_pauses_without_attempts_or_provider_calls(self):
        self.cookie()
        for failure in ('environment', 'cookie'):
            task = self.store.create('download', self.payload())
            self.store.set_downloads_paused(False)
            environment_error = DownloadEnvironmentRequired('fixture missing tools') if failure == 'environment' else None
            with patch('webapp.environment.require_fallback_environment', side_effect=environment_error), \
                    patch('webapp.youtube_auth.probe', return_value=('not_logged_in', 'Fixture expired')), \
                    patch('webapp.yt5s.resolve') as resolver, patch.object(yt_dlp.YoutubeDL, 'download') as native, \
                    patch('web_worker.os.dup2'):
                drain(self.store.directory)
            self.assertTrue(self.store.downloads_paused())
            self.assertEqual(self.store.get(task['id'])['attempts'], 0)
            self.assertEqual(self.store.get(task['id'])['status'], 'queued')
            resolver.assert_not_called()
            native.assert_not_called()
            self.store.finish(task['id'], error='fixture cleanup')

    def test_primary_has_no_cookie_and_success_never_invokes_native_extractor(self):
        self.cookie()
        events, options = [], []

        def auth(*args):
            events.append('auth')
            return 'valid', 'Fixture verified'

        def resolve(url, config, *args, **kwargs):
            events.append('yt5s')
            self.assertEqual(config['download_method'], 'yt5s')
            return {'id': 'npNnbj46_ik', 'title': 'Fixture'}

        def process(downloader, info, **kwargs):
            options.append(downloader.params)

        with patch('webapp.youtube_auth.probe', side_effect=auth), \
                patch('webapp.yt5s.resolve', side_effect=resolve), \
                patch.object(yt_dlp.YoutubeDL, 'process_ie_result', process), \
                patch.object(yt_dlp.YoutubeDL, 'download') as native:
            result = execute('download', self.payload(), store=self.store)
        self.assertEqual(events, ['auth', 'yt5s'])
        native.assert_not_called()
        self.assertNotIn('cookiefile', options[0])
        self.assertEqual(result['download_method'], 'yt5s')
        self.assertFalse(result['fallback_used'])

    def test_parse_and_transfer_errors_fallback_once_with_isolated_cookie_jar(self):
        self.cookie()
        original = (self.root / 'Cookies/youtube.txt').read_bytes()
        for transfer in (False, True):
            values, sessions = [], []

            def native(downloader, urls):
                self.assertEqual(urls, [VIDEO])
                cookie = Path(downloader.params['cookiefile'])
                sessions.append(cookie)
                self.assertNotEqual(cookie, self.root / 'Cookies/youtube.txt')
                self.assertEqual(downloader.params['proxy'], '')
                self.assertFalse(downloader.params['continuedl'])
                return 0

            with patch('webapp.youtube_auth.probe', return_value=('valid', 'Fixture verified')), \
                    patch('webapp.yt5s.resolve', return_value={'id': 'npNnbj46_ik'},
                          side_effect=None if transfer else WebsiteHTTPError('analyze', 502)) as resolver, \
                    patch.object(yt_dlp.YoutubeDL, 'process_ie_result', side_effect=yt_dlp.utils.DownloadError('fixture transfer HTTP Error 403')), \
                    patch.object(yt_dlp.YoutubeDL, 'download', native):
                result = execute('download', self.payload(), values.append, store=self.store)
            resolver.assert_called_once()
            self.assertTrue(result['fallback_used'])
            self.assertEqual(result['download_method'], 'yt_dlp')
            self.assertEqual(len(sessions), 1)
            self.assertFalse(sessions[0].exists())
            self.assertTrue(any(value.get('stage') == 'switching_provider' for value in values))
        self.assertEqual((self.root / 'Cookies/youtube.txt').read_bytes(), original)

    def test_fallback_reuses_worker_node_and_rotation_without_counting_twice(self):
        self.cookie()
        payload = self.payload(proxy_mode='clash')
        rotation = {'enabled': True, 'id': 'fixture', 'every': 5, 'count_failures': False}
        payload['clash'] = {'directory': str(self.root / 'clash'), 'node': 'A', 'rotation': rotation}
        manager = SimpleNamespace(config={}, reload=lambda: None, rotation=lambda: rotation)
        manager.for_worker = lambda slot: manager
        manager.download_snapshot = lambda *args: {'proxy': 'http://127.0.0.1:18888', 'node': 'B'}
        proxies = []

        @contextmanager
        def session(options):
            proxies.append((options['proxy'], bool(options.get('cookiefile'))))
            with download_cookie_session(options) as copy:
                yield copy

        with patch('webapp.clash.ClashManager', return_value=manager), \
                patch('webapp.youtube_auth.YouTubeAuth.require') as auth, \
                patch('webapp.yt5s.resolve', side_effect=WebsiteHTTPError('analyze', 502)), \
                patch('Download.download_cookie_session', side_effect=session), \
                patch.object(yt_dlp.YoutubeDL, 'download', return_value=0), patch('builtins.print'):
            rotation_count = Mock(return_value=4)
            result = execute('download', payload, store=self.store, worker_slot=2, rotation_completed=rotation_count)
        rotation_count.assert_called_once_with('fixture:2', False)
        for call in auth.call_args_list:
            self.assertEqual(call.kwargs, {'proxy': 'http://127.0.0.1:18888', 'node': 'B'})
        self.assertEqual(proxies, [('http://127.0.0.1:18888', False), ('http://127.0.0.1:18888', True)])
        self.assertTrue(result['fallback_used'])

    def test_auth_rejection_after_primary_failure_keeps_queue(self):
        self.cookie()
        task = self.store.create('download', self.payload())

        def native(downloader, urls):
            downloader.report_warning('The provided YouTube account cookies are no longer valid.')
            return 1

        with patch('webapp.youtube_auth.probe', return_value=('valid', 'Fixture verified')), \
                patch('webapp.yt5s.resolve', side_effect=WebsiteHTTPError('analyze', 502)), \
                patch.object(yt_dlp.YoutubeDL, 'download', native), patch('web_worker.os.dup2'):
            drain(self.store.directory)
        self.assertEqual((self.store.get(task['id'])['status'], self.store.get(task['id'])['attempts']), ('queued', 0))
        self.assertTrue(self.store.downloads_paused())
        methods = {row['method']: row for row in self.client.get('/api/statistics').json['methods']}
        self.assertEqual((methods['yt5s']['attempted'], methods['yt5s']['failed']), (1, 1))
        self.assertEqual((methods['yt_dlp']['attempted'], methods['yt_dlp']['fallback_tasks'], methods['yt_dlp']['unfinished']), (1, 1, 1))

    def test_control_and_local_storage_errors_never_start_fallback(self):
        self.cookie()
        for error in (DownloadLeaseLost('fixture lease lost'), BrowserEnvironmentRequired('fixture browser missing'),
                      KeyboardInterrupt(), OSError(28, 'disk full')):
            with self.subTest(error=type(error)), patch('webapp.youtube_auth.YouTubeAuth.require'), \
                    patch('webapp.yt5s.resolve', side_effect=error), \
                    patch.object(yt_dlp.YoutubeDL, 'download') as native:
                with self.assertRaises(type(error)):
                    execute('download', self.payload(), store=self.store)
                native.assert_not_called()

    def test_both_providers_fail_uses_native_error_and_redacts_fallback_reason(self):
        self.cookie()
        values = []
        task = self.store.create('download', self.payload())
        job = self.store.claim(self.store.owner, actions=('download',))
        with patch('webapp.youtube_auth.YouTubeAuth.require'), \
                patch('webapp.yt5s.resolve', side_effect=RuntimeError('fixture https://example.test/?token=PRIVATE_TOKEN')), \
                patch.object(yt_dlp.YoutubeDL, 'download', side_effect=yt_dlp.utils.DownloadError('fixture HTTP Error 503')), \
                patch('builtins.print') as log:
            with self.assertRaisesRegex(RuntimeError, '503'):
                execute('download', job['payload'], values.append, store=self.store, task_id=task['id'])
        self.assertNotIn('PRIVATE_TOKEN', str(values) + str(log.call_args_list))
        self.store.finish(task['id'], error='HTTP 503', worker_id=self.store.owner)
        methods = {row['method']: row for row in self.client.get('/api/statistics').json['methods']}
        self.assertEqual((methods['yt5s']['attempted'], methods['yt5s']['failed']), (1, 1))
        self.assertEqual((methods['yt_dlp']['attempted'], methods['yt_dlp']['failed'], methods['yt_dlp']['fallback_tasks']), (1, 1, 1))

    def test_real_fallback_transfer_bills_exact_bytes_once_and_test_mode_cleans(self):
        self.cookie()
        content = b'final file fixture\n' * 310

        class Handler(BaseHTTPRequestHandler):
            ranges = []

            def do_GET(self):
                self.ranges.append(self.headers.get('Range'))
                if self.path == '/broken.mp4':
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        original_process = yt_dlp.YoutubeDL.process_ie_result

        def native(downloader, urls):
            info = {'id': 'npNnbj46_ik', 'title': 'Fixture video', 'ext': 'mp4',
                    'url': f'http://127.0.0.1:{server.server_port}/fixture.mp4',
                    'extractor': 'youtube', 'extractor_key': 'Youtube', 'webpage_url': VIDEO}
            # A leftover progressive transfer from the other provider must restart.
            final = Path(downloader.prepare_filename(info))
            final.parent.mkdir(parents=True, exist_ok=True)
            Path(str(final) + '.part').write_bytes(b'DIFFERENT ENCODING' * 10)
            original_process(downloader, info, download=True)
            self.assertEqual(final.read_bytes(), content)
            return downloader._download_retcode

        identity = None
        for mode in ('local', 'local', 'test'):
            self.store.set_runtime({'storage_mode': mode})
            task = self.store.create('download', self.payload(storage_mode=mode))
            job = self.store.claim(self.store.owner, actions=('download',))
            values = []
            with patch('webapp.youtube_auth.probe', return_value=('valid', 'Fixture verified')), \
                    patch('webapp.yt5s.resolve', side_effect=WebsiteHTTPError('analyze', 502) if mode == 'local' else None,
                          return_value={'id': 'npNnbj46_ik', 'title': 'Fixture video', 'ext': 'mp4',
                                        'url': f'http://127.0.0.1:{server.server_port}/broken.mp4',
                                        'extractor': 'youtube', 'extractor_key': 'Youtube', 'webpage_url': VIDEO}) as resolver, \
                    patch.object(yt_dlp.YoutubeDL, 'download', native):
                result = execute('download', job['payload'], values.append, store=self.store, task_id=task['id'])
            self.store.finish(task['id'], result=result, worker_id=self.store.owner)
            if identity is None:
                identity = task['id']
                self.assertEqual((result['file_count'], result['file_bytes'], result['billable_bytes']), (1, len(content), len(content)))
                self.assertTrue(any(value.get('downloaded_bytes') == len(content) for value in values))
                self.assertTrue(self.store.archive_has('youtube npNnbj46_ik'))
            elif mode == 'local':
                resolver.assert_not_called()
                self.assertTrue(result['skipped'])
            else:
                self.assertTrue(result['files_deleted'])
                self.assertEqual(result['billable_bytes'], 0)
        self.assertEqual(len(list((self.root / 'videos').rglob('*.mp4'))), 1)
        self.assertFalse(list((self.store.directory / 'test-downloads').rglob('*.mp4')))
        totals = self.client.get('/api/statistics').json['summary']
        self.assertEqual((totals['file_count'], int(totals['file_bytes'])), (1, len(content)))
        methods = self.client.get('/api/statistics').json['methods']
        by_provider = {row['method']: row for row in methods}
        self.assertEqual((by_provider['yt5s']['attempted'], by_provider['yt5s']['failed'], by_provider['yt5s']['skipped']), (3, 2, 1))
        provider = by_provider['yt_dlp']
        self.assertEqual((provider['attempted'], provider['succeeded'], provider['test_succeeded'], provider['fallback_tasks']), (2, 1, 1, 2))
        self.assertEqual((provider['file_bytes'], provider['test_file_bytes'], provider['downloaded_file_bytes']),
                         (len(content), len(content), len(content) * 2))
        self.assertTrue(all(value is None or value.startswith('bytes=0-') for value in Handler.ranges))


class SQLiteFallbackTests(FallbackContract, unittest.TestCase):
    setUp = test_yt5s.Yt5sTests.setUp


class MySQLFallbackTests(MySQLFixture, FallbackContract, unittest.TestCase):
    def setUp(self):
        browser = patch('webapp.yt5s.browser_executable', return_value='/fixture/chromium')
        display = patch('webapp.yt5s.display_available', return_value=True)
        browser.start()
        display.start()
        self.addCleanup(browser.stop)
        self.addCleanup(display.stop)
        super().setUp()
