"""Provider selection must route real transfers without exposing login cookies."""
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import create_app
from Utils.runtime import configure_runtime
from webapp.settings import validate_settings
from webapp.yt5s import BrowserEnvironmentRequired, BrowserTimeoutError, WebsiteHTTPError, WebsiteTimeoutError, probe, require_browser_environment, resolve, wait_for_site
from web_worker import execute

VIDEO = 'https://www.youtube.com/watch?v=npNnbj46_ik'


class WebsiteWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_logs_progress_and_finishes_when_site_responds(self):
        event = asyncio.Event()
        asyncio.get_running_loop().call_later(0.03, event.set)
        with patch('builtins.print') as log:
            await wait_for_site(event, 1, 'fixture website', interval=0.005)
        self.assertTrue(event.is_set())
        self.assertTrue(log.called)

    async def test_silent_site_has_bounded_wait(self):
        with patch('builtins.print') as log:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(wait_for_site(asyncio.Event(), 0.03, 'fixture', interval=0.005), timeout=1)
        self.assertTrue(log.called)

    async def test_navigation_form_and_verification_timeouts_report_phase_and_cleanup(self):
        for failure, expected in [('navigation', '45 秒'), ('form', '输入框'), ('verification', '验证与解析超时')]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                args = SimpleNamespace(video_id='npNnbj46_ik', quality='360', proxy='', timeout=0.02)
                page = MagicMock()
                page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
                locator = MagicMock(fill=AsyncMock(), click=AsyncMock())
                page.locator.return_value = locator
                if failure == 'navigation':
                    page.goto.side_effect = BrowserTimeoutError('https://example.test/?token=PRIVATE_TEST_TOKEN')
                elif failure == 'form':
                    locator.fill.side_effect = BrowserTimeoutError('fixture missing form')

                async def screenshot(**options):
                    Path(options['path']).write_bytes(b'fixture screenshot')

                page.screenshot = AsyncMock(side_effect=screenshot)
                context = MagicMock(new_page=AsyncMock(return_value=page))
                browser = MagicMock(contexts=[context], close=AsyncMock())
                driver = MagicMock()
                driver.chromium.connect_over_cdp = AsyncMock(return_value=browser)
                playwright = MagicMock()
                playwright.__aenter__ = AsyncMock(return_value=driver)
                playwright.__aexit__ = AsyncMock(return_value=False)
                process = MagicMock()
                process.poll.return_value = None
                report = {'responses': []}
                with patch('webapp.yt5s.async_playwright', return_value=playwright), \
                        patch('webapp.yt5s.subprocess.Popen', return_value=process), \
                        patch('webapp.yt5s.browser_executable', return_value='/fixture/chromium'), \
                        patch('webapp.yt5s.build_opener'), patch('builtins.print'):
                    with self.assertRaisesRegex(RuntimeError, expected) as error:
                        await probe(args, directory, report, download=False)
                self.assertNotIn('PRIVATE_TEST_TOKEN', str(error.exception))
                page.screenshot.assert_not_awaited()
                self.assertFalse((directory / 'page.png').exists())
                self.assertGreaterEqual(report['elapsed_seconds'], 0)
                self.assertIn(report['stage'], report['stage_seconds'])
                browser.close.assert_awaited_once()
                process.terminate.assert_called_once()


class WebsiteDiagnosticsTests(unittest.TestCase):
    def test_failed_resolutions_keep_safe_timings_without_screenshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            async def fail(args, directory, report, **kwargs):
                report.update(stage='opening_site', stage_seconds={'opening_site': 4.2}, elapsed_seconds=5)
                raise RuntimeError('fixture navigation timeout')

            with patch('webapp.yt5s.require_browser_environment', return_value='/fixture/chromium'), \
                    patch('webapp.yt5s.probe', side_effect=fail), patch('builtins.print'):
                with self.assertRaisesRegex(RuntimeError, 'fixture navigation timeout'):
                    resolve(VIDEO, {'proxy_mode': 'direct'}, root, root)
            report = json.loads((root / 'yt5s/worker-0/last-failure.json').read_text(encoding='utf-8'))
            self.assertEqual(report['stage'], 'opening_site')
            self.assertEqual(report['stage_seconds'], {'opening_site': 4.2})
            self.assertEqual(report['elapsed_seconds'], 5)
            self.assertEqual(list(root.rglob('*.png')), [])
            self.assertEqual(list((root / 'yt5s/worker-0').glob('resolve-*')), [])


class WebsiteResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_and_convert_fail_fast_with_http_status_and_no_response_secrets(self):
        cases = [('analyze', 502, {'status': 502}, WebsiteHTTPError, True),
                 ('analyze', 503, None, WebsiteHTTPError, True),
                 ('analyze', 200, {'status': 502}, WebsiteHTTPError, True),
                 ('analyze', 429, {'status': 'error'}, WebsiteHTTPError, True),
                 ('analyze', 403, {'status': 'error'}, WebsiteHTTPError, False),
                 ('analyze', 403, {'result': 'Verification failed PRIVATE_TOKEN'}, RuntimeError, False),
                 ('convert', 502, {'status': 502}, WebsiteHTTPError, True),
                 ('convert', 502, None, WebsiteHTTPError, True)]
        for step, status, data, kind, retryable in cases:
            with self.subTest(step=step, status=status, data=data), tempfile.TemporaryDirectory() as temporary:
                page = MagicMock(goto=AsyncMock(return_value=SimpleNamespace(status=200)), screenshot=AsyncMock())
                locator = MagicMock(fill=AsyncMock(), wait_for=AsyncMock(), inner_text=AsyncMock(return_value='Fixture 0:30'))
                locator.filter.return_value = locator
                locator.locator.return_value = locator
                locator.first = locator
                locator.get_attribute = AsyncMock(side_effect=lambda name: 'https://example.test/cover.jpg' if name == 'src' else None)
                page.locator.return_value = locator
                callbacks = {}
                page.on.side_effect = lambda event, callback: callbacks.update({event: callback})
                clicks = 0

                async def click(**kwargs):
                    nonlocal clicks
                    clicks += 1
                    analyze_success = step == 'convert' and clicks == 1
                    response = MagicMock(url='https://example.test/mates/en/' +
                                         ('analyze/ajax' if analyze_success else 'analyze/ajax' if step == 'analyze' else 'convert'))
                    response.status = 200 if analyze_success else status
                    response.json = AsyncMock(return_value={'status': 'success'} if analyze_success else data)
                    if data is None and not analyze_success:
                        response.json.side_effect = ValueError('non-JSON gateway body PRIVATE_TOKEN')
                    callbacks['response'](response)

                locator.click = AsyncMock(side_effect=click)
                browser = MagicMock(contexts=[MagicMock(new_page=AsyncMock(return_value=page))], close=AsyncMock())
                driver = MagicMock()
                driver.chromium.connect_over_cdp = AsyncMock(return_value=browser)
                playwright = MagicMock(__aenter__=AsyncMock(return_value=driver), __aexit__=AsyncMock(return_value=False))
                process = MagicMock()
                process.poll.return_value = None
                args = SimpleNamespace(video_id='npNnbj46_ik', quality='360', proxy='', timeout=0.1)
                report = {'responses': []}
                with patch('webapp.yt5s.async_playwright', return_value=playwright), \
                        patch('webapp.yt5s.subprocess.Popen', return_value=process), \
                        patch('webapp.yt5s.browser_executable', return_value='/fixture/chromium'), \
                        patch('webapp.yt5s.build_opener'), patch('builtins.print'):
                    with self.assertRaises(kind) as caught:
                        await probe(args, Path(temporary), report, download=False)
                self.assertIs(type(caught.exception), kind)
                self.assertEqual(getattr(caught.exception, 'retryable', False), retryable)
                self.assertNotIn('PRIVATE_TOKEN', str(caught.exception) + json.dumps(report))
                self.assertEqual(report['responses'][-1]['http_status'], status)
                browser.close.assert_awaited_once()
                process.terminate.assert_called_once()


class Yt5sTests(unittest.TestCase):
    def setUp(self):
        browser = patch('webapp.yt5s.browser_executable', return_value='/fixture/chromium')
        display = patch('webapp.yt5s.display_available', return_value=True)
        browser.start()
        display.start()
        self.addCleanup(browser.stop)
        self.addCleanup(display.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        self.app = create_app(self.config, self.root / 'state', start_worker=False)
        self.client = self.app.test_client()
        page = self.client.get('/').text
        self.headers = {'X-CSRF-Token': re.search(r'name="csrf-token" content="([^"]+)"', page)[1]}
        self.store = self.app.extensions['task_store']

    def method(self, value):
        result = self.client.put('/api/config', json={'download_method': value}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)

    def test_provider_selection_persists_and_method_two_creates_jobs_without_cookie(self):
        self.assertIn('id="download-method"', self.client.get('/').text)
        self.assertEqual(self.client.get('/api/config').json['config']['download_method'], 'yt_dlp')
        self.assertEqual(self.client.put('/api/config', json={'download_method': 'yt5s'}).status_code, 403)
        self.method('yt5s')
        with patch.object(self.app.extensions['youtube_auth'], 'require', side_effect=AssertionError('must not use Cookie')):
            created = self.client.post('/api/downloads', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
        self.assertEqual(created.status_code, 202, created.json)
        task = self.store.get(created.json['items'][0]['id'], private=True)
        self.assertNotIn('config', task['payload'])
        effective = self.store.effective_download_payload(task['id'], task['payload'])
        self.assertEqual(effective['config']['download_method'], 'yt5s')
        self.assertEqual(self.client.get('/api/status').json['download_method'], 'yt5s')
        bad = self.client.post('/api/downloads', json={'urls': 'https://www.youtube.com/playlist?list=PLtest'}, headers=self.headers)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.store.counts()['total'], 1)
        self.method('yt_dlp')
        result = self.client.post('/api/downloads', json={'urls': '2Gu7q-KUXCk'}, headers=self.headers)
        self.assertEqual(result.status_code, 409)  # Method one still requires login.

    def test_resume_uses_current_provider_after_default_is_changed(self):
        self.method('yt5s')
        self.client.post('/api/downloads', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
        self.store.set_downloads_paused(True)
        self.method('yt_dlp')
        with patch.object(self.app.extensions['youtube_auth'], 'require', return_value={}) as auth:
            result = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        auth.assert_called_once()
        self.assertFalse(self.store.downloads_paused())
        # The inverse uses current method two without requiring old method-one Cookie.
        self.store.create('download', {'config': {'download_method': 'yt_dlp', 'proxy_mode': 'direct'},
                                       'base': str(self.root), 'urls': ['https://youtu.be/2Gu7q-KUXCk']})
        first = self.store.page(actions=('download',), status='queued', order='asc')['items'][0]
        self.store.finish(first['id'], result={'message': 'fixture complete'})
        self.method('yt5s')
        self.store.set_downloads_paused(True)
        result = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(self.store.downloads_paused())

    def test_invalid_provider_and_quality_do_not_persist(self):
        before = self.config.read_bytes()
        for values in ({'download_method': 'made-up'}, {'yt5s_quality': '1080'}):
            result = self.client.put('/api/config', json=values, headers=self.headers)
            self.assertEqual(result.status_code, 400)
            self.assertEqual(self.config.read_bytes(), before)

    def test_missing_browser_rejects_new_jobs_import_and_resume_without_losing_queue(self):
        self.method('yt5s')
        with patch('webapp.yt5s.browser_executable', side_effect=RuntimeError('missing')):
            result = self.client.post('/api/downloads', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
            self.assertEqual(result.status_code, 409, result.json)
            self.assertIn('compose.remote-mysql.yaml', result.json['error'])
            imported = self.client.post('/api/downloads/import', headers=self.headers,
                                        data={'file': (BytesIO(b'npNnbj46_ik\n'), 'videos.txt')})
            self.assertEqual(imported.status_code, 202, imported.json)
            self.assertEqual(self.store.counts()['total'], 1)
            self.assertTrue((self.store.directory / 'imports').exists())
        created = self.client.post('/api/downloads', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
        self.assertEqual(created.status_code, 202, created.json)
        self.store.set_downloads_paused(True)
        self.method('yt5s')  # Resume checks the current page's browser environment.
        with patch('webapp.yt5s.display_available', return_value=False):
            result = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(result.status_code, 409, result.json)
        self.assertIn('显示环境', result.json['error'])
        self.assertEqual(self.store.download_queue(), {'paused': True, 'queued': 1, 'running': 0, 'waiting_lists': 1, 'importing': 1})

    def test_worker_missing_browser_pauses_and_requeues_without_failure(self):
        from web_worker import drain

        task = self.store.create('download', {'config': {'download_method': 'yt5s', 'path': 'videos'},
                                             'base': str(self.root), 'urls': [VIDEO]})
        with patch('web_worker.os.dup2'), patch('web_worker.execute', side_effect=BrowserEnvironmentRequired('missing browser')):
            drain(self.store.directory)
        current = self.store.get(task['id'])
        self.assertEqual((current['status'], current['attempts']), ('queued', 0))
        self.assertEqual(self.store.download_queue(), {'paused': True, 'queued': 1, 'running': 0, 'waiting_lists': 0, 'importing': 0})
        self.assertEqual(self.client.get('/api/tasks?view=history').json['items'], [])

    def test_browser_preflight_resolves_explicit_path_and_reports_missing_display(self):
        with patch('webapp.yt5s.browser_executable', return_value='/fixture/chromium') as browser:
            self.assertEqual(require_browser_environment({'browser_executable': 'browser/chromium'}, self.root),
                             '/fixture/chromium')
            browser.assert_called_once_with(str(self.root / 'browser/chromium'))
            with patch('webapp.yt5s.display_available', return_value=False):
                with self.assertRaisesRegex(BrowserEnvironmentRequired, 'DISPLAY'):
                    require_browser_environment({}, self.root)

    def test_website_timeout_still_uses_configured_retry_backoff(self):
        from web_worker import drain

        task = self.store.create('download', {'config': {'download_method': 'yt5s', 'path': 'videos'},
                                             'base': str(self.root), 'urls': [VIDEO]})
        self.store.update_progress(task['id'], {'stage': 'opening_site'})
        with patch('web_worker.os.dup2'), patch('web_worker.execute', side_effect=WebsiteTimeoutError('网站解析超时')):
            drain(self.store.directory)
        current = self.store.get(task['id'])
        self.assertEqual((current['status'], current['attempts']), ('queued', 1))
        self.assertEqual(current['progress']['stage'], 'retrying')
        self.assertFalse(self.store.downloads_paused())

    def test_website_http_errors_retry_with_backoff_and_respect_attempt_limit(self):
        from web_worker import drain

        for status, maximum, expected in ((502, 3, 'queued'), (503, 1, 'failed'), (403, 3, 'failed')):
            with self.subTest(status=status, maximum=maximum):
                self.client.put('/api/config', json={'download_attempts': maximum}, headers=self.headers)
                task = self.store.create('download', {'config': {'download_method': 'yt5s', 'path': 'videos',
                                                                 'download_attempts': maximum},
                                                     'base': str(self.root), 'urls': [VIDEO]})
                self.store.update_progress(task['id'], {'stage': 'waiting_verification'})
                with patch('web_worker.os.dup2'), patch('web_worker.execute', side_effect=WebsiteHTTPError('analyze', status)), \
                        patch('builtins.print') as log:
                    drain(self.store.directory)
                current = self.store.get(task['id'])
                self.assertEqual((current['status'], current['attempts']), (expected, 1))
                if expected == 'queued':
                    self.assertEqual(current['progress']['stage'], 'retrying')
                    self.assertGreater(current['available_at'], 0)
                    self.assertTrue(any('已安排自动重试' in str(call) for call in log.call_args_list))
                    self.store.finish(task['id'], error='fixture cleanup')
                self.assertFalse(self.store.downloads_paused())

    def test_real_transfer_progress_archive_metadata_catalog_and_test_cleanup(self):
        configure_runtime()
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('FFmpeg required for a real fixture video')
        source = self.root / 'source'
        source.mkdir()
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'color=c=black:s=64x64:d=0.3', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
                        '-shortest', '-c:v', 'libx264', '-c:a', 'aac', str(source / 'fixture.mp4')], check=True)
        from PIL import Image
        Image.new('RGB', (16, 16)).save(source / 'cover.jpg')
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(source)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host = f'http://127.0.0.1:{server.server_port}'
        info = {'id': 'npNnbj46_ik', 'title': 'Fixture video', 'url': host + '/fixture.mp4',
                'ext': 'mp4', 'extractor': 'youtube', 'extractor_key': 'Youtube',
                'thumbnail': host + '/cover.jpg', 'webpage_url': VIDEO}
        values = []
        config = {'path': 'videos', 'proxy_mode': 'direct', 'download_method': 'yt5s',
                  'shard_downloads': True, 'youtube_cookie': 'must-not-be-opened.txt'}
        payload = {'config': config, 'base': str(self.root), 'urls': [VIDEO]}
        with patch('webapp.yt5s.resolve', return_value=info) as resolver, \
                patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie preflight')):
            result = execute('download', payload, values.append, store=self.store, worker_slot=2)
            resolver.assert_called_once()
            self.assertEqual(resolver.call_args.kwargs['worker_slot'], 2)
            capacity = resolver.call_args.kwargs['browser_capacity']
            self.assertEqual(capacity(), 2)
            self.store.set_runtime({'yt5s_browser_count': 3})
            self.assertEqual(capacity(), 3)  # Existing queued snapshots use the live limit.
        self.assertFalse(result['skipped'])
        self.assertTrue(self.store.archive_has('youtube npNnbj46_ik'))
        self.assertTrue(any(row.get('downloaded_bytes', 0) > 0 for row in values))
        media = self.client.get('/api/media').json['items']
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]['title'], 'Fixture video')
        self.assertTrue(list((self.root / 'videos').rglob('*.jpg')))
        metadata = json.loads(next((self.root / 'videos').rglob('*.info.json')).read_text(encoding='utf-8'))
        self.assertEqual(metadata['webpage_url'], VIDEO)
        with patch('webapp.yt5s.resolve', side_effect=AssertionError('archive must skip browser')):
            self.assertTrue(execute('download', payload, store=self.store)['skipped'])
        self.store.set_runtime({**config, 'storage_mode': 'test'})
        with patch('webapp.yt5s.resolve', return_value=dict(info)):
            result = execute('download', {**payload, 'config': {**config, 'storage_mode': 'test'}}, store=self.store)
        self.assertTrue(result['files_deleted'])
        self.assertEqual(len(self.client.get('/api/media').json['items']), 1)
