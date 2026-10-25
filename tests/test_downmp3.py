"""API resolution, provider routing, real transfers and exact delivery accounting."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from tests import test_scale
from webapp.downmp3 import Downmp3Error, api_url, media_url, request_json, resolve
from webapp.downloader import build_ydl_options
from webapp.settings import validate_method_urls
from web_worker import drain, execute

VIDEO = 'https://www.youtube.com/watch?v=npNnbj46_ik'
CDN = 'https://bernice23.savenow.to/video.mp4?signature=PRIVATE_SIGNATURE'


class Downmp3ResolverTests(unittest.TestCase):
    def test_api_creation_polling_metadata_and_selected_proxy_without_cookie(self):
        stages = []
        responses = [{'success': True, 'id': 'fixture', 'info': {'title': 'Fixture title', 'image': 'https://i.ytimg.com/cover.jpg'},
                      'progress_url': 'https://p.savenow.to/api/progress?id=fixture'},
                     {'success': 0, 'progress': 250},
                     {'success': 1, 'progress': 1000, 'download_url': CDN}]
        config = {'download_method': 'downmp3', 'downmp3_quality': '1080', 'proxy_mode': 'clash',
                  '_resolved_clash_proxy': 'http://127.0.0.1:19001', 'youtube_cookie': 'must-not-be-opened.txt'}
        with patch('webapp.downmp3.build_opener') as opener, \
                patch('webapp.downmp3.request_json', side_effect=responses) as request, \
                patch('webapp.downmp3.time.sleep'), patch('builtins.print'):
            info = resolve(VIDEO, config, Path('.'), lambda *value: stages.append(value))
        self.assertEqual(info['title'], 'Fixture title')
        self.assertEqual((info['url'], info['height'], info['ext']), (CDN, 1080, 'mp4'))
        self.assertEqual(info['extractor_key'], 'Youtube')
        self.assertEqual(opener.call_args.args[0].proxies,
                         {'http': 'http://127.0.0.1:19001', 'https': 'http://127.0.0.1:19001'})
        parameters = parse_qs(urlsplit(request.call_args_list[0].args[1]).query)
        self.assertEqual(parameters['format'], ['1080'])
        self.assertEqual(parameters['url'], [VIDEO])
        self.assertIn(('waiting_api_conversion', 25), stages)
        self.assertEqual(stages[-1], ('extracting', None))
        options = build_ydl_options(config, Path('.'))
        self.assertNotIn('cookiefile', options)
        self.assertFalse(options['writesubtitles'])
        self.assertNotIn('+', options['format'])

    def test_api_errors_are_bounded_and_do_not_expose_response_or_signed_urls(self):
        opener = MagicMock()
        for status, retry in ((429, True), (502, True), (403, False), (404, False)):
            with self.subTest(status=status):
                opener.open.side_effect = HTTPError(CDN, status, 'PRIVATE_SIGNATURE', {}, None)
                with self.assertRaises(Downmp3Error) as caught:
                    request_json(opener, 'https://p.savenow.to/api/progress', 30)
                self.assertEqual(caught.exception.retryable, retry)
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn('PRIVATE_SIGNATURE', str(caught.exception))
        opener.open.side_effect = URLError(CDN)
        with self.assertRaises(Downmp3Error) as caught:
            request_json(opener, 'https://p.savenow.to/api/progress', 30)
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn('PRIVATE_SIGNATURE', str(caught.exception))
        opener.open.side_effect = None
        for body in (b'<html>PRIVATE_SIGNATURE</html>', b'[]', b'a' * (1024 * 1024 + 1)):
            opener.open.return_value.__enter__.return_value.read.return_value = body
            with self.assertRaises(Downmp3Error) as caught:
                request_json(opener, 'https://p.savenow.to/api/progress', 30)
            self.assertNotIn('PRIVATE_SIGNATURE', str(caught.exception))

    def test_failed_conversion_missing_result_and_timeout_do_not_poll_forever(self):
        cases = [([{'success': False}], '创建'),
                 ([{'success': True}], '编号'),
                 ([{'id': 'fixture'}, {'success': 0, 'error': 'PRIVATE_SIGNATURE'}], '转换失败'),
                 ([{'id': 'fixture'}, {'success': 1}], '没有返回')]
        for responses, message in cases:
            with self.subTest(message=message), patch('webapp.downmp3.request_json', side_effect=responses), \
                    patch('builtins.print'):
                with self.assertRaisesRegex(Downmp3Error, message) as caught:
                    resolve(VIDEO, {'proxy_mode': 'direct'}, Path('.'))
                self.assertNotIn('PRIVATE_SIGNATURE', str(caught.exception))
        with patch('webapp.downmp3.time.monotonic', side_effect=[0, 0, 31]), \
                patch('webapp.downmp3.request_json', return_value={'id': 'fixture'}), patch('builtins.print'):
            with self.assertRaisesRegex(Downmp3Error, '超时') as caught:
                resolve(VIDEO, {'downmp3_timeout': 30, 'proxy_mode': 'direct'}, Path('.'))
        self.assertTrue(caught.exception.retryable)

    def test_untrusted_progress_addresses_and_invalid_videos_are_rejected(self):
        for value in ('http://p.savenow.to/api/progress', 'https://example.com/api/progress',
                      'https://p.savenow.to@127.0.0.1/api/progress', 'https://p.savenow.to:8443/api/progress'):
            with self.subTest(value=value), self.assertRaises(Downmp3Error):
                api_url(value)
        for value in ('file:///private', 'http://127.0.0.1/video', 'http://169.254.169.254/video',
                      'http://localhost/video', 'https://user:secret@example.com/video'):
            with self.subTest(value=value), self.assertRaises(Downmp3Error):
                media_url(value)
        for value in ('https://youtube.com/playlist?list=PLfixture', 'https://example.com/video.mp4'):
            with self.assertRaisesRegex(ValueError, 'downmp3'):
                validate_method_urls({'download_method': 'downmp3'}, [value])


class Downmp3WebTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def select(self, **values):
        result = self.client.put('/api/config', json={'download_method': 'downmp3', **values}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)

    def test_page_configuration_queue_import_and_resume_do_not_require_browser_or_cookie(self):
        page = self.client.get('/').text
        self.assertIn('id="download-method" class="provider-choices"', page)
        self.assertIn('id="settings-download-method" class="provider-choices"', page)
        self.assertIn('name="downmp3_quality"', page)
        self.select(downmp3_quality='480', downmp3_timeout='180')
        before = self.config.read_bytes()
        for invalid in ({'downmp3_quality': 'mp3'}, {'downmp3_timeout': 0}, {'downmp3_timeout': 1801}):
            response = self.client.put('/api/config', json=invalid, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.config.read_bytes(), before)
        with patch('webapp.yt5s.require_browser_environment', side_effect=AssertionError('no browser')), \
                patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie')):
            created = self.client.post('/api/downloads', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
            self.assertEqual(created.status_code, 202, created.json)
            self.store.set_downloads_paused(True)
            resumed = self.client.post('/api/downloads/resume', headers=self.headers)
            self.assertEqual(resumed.status_code, 200, resumed.json)
            self.select()
            imported = self.client.post('/api/downloads/import', data={'file': (BytesIO(b'2Gu7q-KUXCk'), 'list.txt')}, headers=self.headers)
            self.assertEqual(imported.status_code, 202, imported.json)
        task = self.store.get(created.json['items'][0]['id'], private=True)
        self.assertNotIn('config', task['payload'])
        self.assertEqual(self.store.effective_download_payload(task['id'], task['payload'])['config']['downmp3_quality'], '480')

    def test_missing_probe_blocks_before_task_creation(self):
        self.select()
        with patch('webapp.environment.shutil.which', return_value=None):
            response = self.client.post('/api/downloads', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
            self.assertEqual(response.status_code, 409, response.json)
            self.assertIn('ffprobe', response.json['error'])
        self.assertEqual(self.store.counts()['total'], 0)

    def test_switch_to_four_routes_both_old_and_new_pending_jobs_to_downmp3(self):
        import yt_dlp
        old = self.store.create('download', {'config': {'download_method': 'yt5s', 'proxy_mode': 'direct',
                                                       'path': 'videos'},
                                             'base': str(self.root), 'urls': [VIDEO]})
        self.store.set_downloads_paused(True)
        self.select()
        with patch('app.require_browser_environment'):
            resumed = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(resumed.status_code, 200, resumed.json)
        new = self.client.post('/api/downloads', json={'urls': '2Gu7q-KUXCk'}, headers=self.headers)
        self.assertEqual(new.status_code, 202, new.json)
        for task, expected in ((old, 'downmp3'), (new.json['items'][0], 'downmp3')):
            with self.subTest(provider=expected), \
                    patch('webapp.yt5s.resolve', return_value={'id': 'fixture'}) as yt5s, \
                    patch('webapp.downmp3.resolve', return_value={'id': 'fixture'}) as downmp3, \
                    patch.object(yt_dlp.YoutubeDL, 'process_ie_result'), \
                    patch.object(yt_dlp.YoutubeDL, 'download') as native, patch('builtins.print') as log:
                values = []
                payload = self.store.get(task['id'], private=True)['payload']
                result = execute('download', payload, values.append, store=self.store, task_id=task['id'])
                self.assertEqual(result['download_method'], expected)
                self.assertTrue(all(value['download_method'] == expected
                                    for value in values if 'download_method' in value))
                self.assertEqual(yt5s.call_count, int(expected == 'yt5s'))
                self.assertEqual(downmp3.call_count, int(expected == 'downmp3'))
                native.assert_not_called()
                self.assertTrue(any('本任务下载方式：' in str(call) and expected in str(call)
                                    for call in log.call_args_list))

    def test_mode_four_environment_check_only_requires_the_probe_binary(self):
        with patch('webapp.environment.shutil.which', side_effect=lambda name: '/fixture/ffprobe' if name == 'ffprobe' else None), \
                patch('webapp.environment.importlib.util.find_spec', return_value=True), patch('builtins.print'):
            result = execute('check', {'config': {'download_method': 'downmp3'}, 'base': str(self.root)})
        self.assertEqual(result['message'], '环境检查通过')

    def test_provider_http_failures_use_existing_retry_policy(self):
        for retryable, expected in ((True, 'queued'), (False, 'failed')):
            task = self.store.create('download', {'config': {'download_method': 'downmp3', 'path': 'videos'},
                                                'base': str(self.root), 'urls': [VIDEO]})
            with patch('web_worker.os.dup2'), patch('web_worker.execute', side_effect=Downmp3Error('API failure', retryable=retryable)), \
                    patch('builtins.print'), patch('web_worker.traceback.print_exc'):
                drain(self.store.directory)
            self.assertEqual(self.store.get(task['id'])['status'], expected)
            self.store.finish(task['id'], error='fixture cleanup')

    def test_real_transfer_cover_catalog_test_cleanup_and_provider_billing(self):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('FFmpeg required to generate a real video fixture')
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
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host = f'http://127.0.0.1:{server.server_port}'
        size = (source / 'fixture.mp4').stat().st_size
        for mode, video_id in (('local', 'npNnbj46_ik'), ('test', '2Gu7q-KUXCk')):
            self.select(storage_mode=mode)
            created = self.client.post('/api/downloads', json={'urls': video_id}, headers=self.headers)
            self.assertEqual(created.status_code, 202, created.json)
            task = self.store.claim(self.store.owner)
            self.assertIsNotNone(task)
            info = {'id': video_id, 'title': 'Fixture video', 'url': host + '/fixture.mp4', 'ext': 'mp4',
                    'thumbnail': host + '/cover.jpg', 'extractor': 'youtube', 'extractor_key': 'Youtube'}
            values = []
            with patch('webapp.downmp3.resolve', return_value=info), \
                    patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie')):
                result = execute('download', task['payload'], values.append, store=self.store, task_id=task['id'])
            self.assertEqual(result['file_bytes'], size)
            self.assertEqual(result['download_method'], 'downmp3')
            self.assertTrue(any(value.get('downloaded_bytes', 0) > 0 for value in values))
            self.store.finish(task['id'], result=result, worker_id=self.store.owner)
            if mode == 'local':
                catalog = self.client.get('/api/media').json['items']
                self.assertEqual(len(catalog), 1)
                self.assertTrue(catalog[0]['cover'])
                self.assertTrue(self.store.archive_has('youtube ' + video_id))
            else:
                self.assertTrue(result['files_deleted'])
                self.assertFalse(list((self.store.directory / 'test-downloads').rglob('*.mp4')))
                self.assertFalse(self.store.archive_has('youtube ' + video_id))
        rows = {item['method']: item for item in self.client.get('/api/statistics').json['methods']}
        provider = rows['downmp3']
        self.assertEqual((provider['attempted'], provider['succeeded'], provider['test_succeeded']), (2, 1, 1))
        self.assertEqual((provider['file_bytes'], provider['test_file_bytes']), (size, size))
        self.assertEqual(provider['success_rate'], 100)
        exported = self.client.get('/api/statistics/methods/export').data.decode('utf-8-sig')
        self.assertIn('downmp3', exported)
        # A site error page saved under an MP4 name must not become a delivery.
        (source / 'invalid.mp4').write_bytes(b'<html>Upstream error</html>')
        self.select(storage_mode='local')
        self.client.post('/api/downloads', json={'urls': 'WMu6ANTg8mk'}, headers=self.headers)
        task = self.store.claim(self.store.owner)
        info = {**info, 'id': 'WMu6ANTg8mk', 'url': host + '/invalid.mp4'}
        with patch('webapp.downmp3.resolve', return_value=info), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, '校验'):
                execute('download', task['payload'], store=self.store, task_id=task['id'])
        self.store.finish(task['id'], error='invalid video', worker_id=self.store.owner)
        self.assertFalse(self.store.archive_has('youtube WMu6ANTg8mk'))
        self.assertEqual(len(self.client.get('/api/media').json['items']), 1)
        summary = self.client.get('/api/statistics').json['summary']
        self.assertEqual((summary['file_bytes'], summary['test_file_bytes']), (size, size))
