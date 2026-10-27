"""yt1d website contract, shared capacity, hot policy and verified delivery."""
import asyncio
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_scale
from webapp.downloader import build_ydl_options
from webapp.settings import validate_settings
from webapp.yt1d import media_url, resolve
from webapp.yt5s import browser_executable, probe, response_error, WebsiteHTTPError
from web_worker import execute

VIDEO = 'https://www.youtube.com/watch?v=jNQXAC9IVRw'
MEDIA = 'https://cdn.example.com/video.mp4?token=PRIVATE_TOKEN'


class ResolverTests(unittest.TestCase):
    def test_environment_check_uses_configuration_base_without_node_or_cookie(self):
        config = {'download_methods': ['yt1d'], 'browser_executable': 'browser/chromium'}
        base = Path(tempfile.gettempdir()) / 'yt1d-environment-fixture'
        with patch('webapp.environment.importlib.util.find_spec', return_value=True), \
                patch('webapp.environment.shutil.which', return_value=None), \
                patch('webapp.yt1d.require_environment') as environment, patch('builtins.print'):
            result = execute('check', {'config': config, 'base': str(base)})
        environment.assert_called_once_with(config, base)
        self.assertEqual(result['message'], '环境检查通过')

    def test_browser_proxy_quality_timeout_and_shared_capacity_without_cookie(self):
        for mode, proxy in [('direct', ''), ('manual', 'http://127.0.0.1:18080'),
                            ('clash', 'http://127.0.0.1:19001')]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                capacity = lambda: 2
                config = {'proxy_mode': mode, 'proxy': 'http://127.0.0.1:18080',
                          '_resolved_clash_proxy': 'http://127.0.0.1:19001',
                          'yt1d_quality': '2160', 'yt1d_timeout': 120,
                          'youtube_cookie': 'must-not-be-read', '_disk_state': str(root)}

                async def resolved(args, directory, report, **kwargs):
                    self.assertEqual((args.proxy, args.quality, args.timeout), (proxy, '2160', 120))
                    self.assertEqual((args.provider, args.site_url), ('yt1d', 'https://yt1d.lol/'))
                    return {'id': 'jNQXAC9IVRw', 'url': MEDIA, 'height': 2160}

                with patch('webapp.yt1d.require_environment', return_value='/browser'), \
                        patch('webapp.yt1d.session') as session, patch('webapp.yt1d.probe', side_effect=resolved):
                    result = resolve(VIDEO, config, root, root, worker_slot=3, browser_capacity=capacity)
                session.assert_called_once_with(root / 'yt5s', 3)
                options = session.return_value.use.call_args
                self.assertIs(options.args[1], capacity)
                self.assertEqual(options.kwargs['disk_directory'], str(root))
                self.assertEqual(result['format_id'], 'yt1d-2160')
                download = build_ydl_options({**config, 'download_method': 'yt1d'}, root)
                self.assertNotIn('cookiefile', download)
                self.assertEqual(download['format'], 'best')
                self.assertFalse(download['writesubtitles'])

    def test_validation_rejects_bad_quality_timeout_and_private_media(self):
        for value in ('file:///secret', 'http://127.0.0.1/video', 'http://[::1]/video',
                      'https://localhost/video', 'http://169.254.169.254/video',
                      'https://user:password@cdn.example.com/video', 'https://cdn.example.com:8443/video', None):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                media_url(value)
        self.assertEqual(media_url(MEDIA), MEDIA)
        for invalid in ({'yt1d_quality': 'mp3'}, {'yt1d_timeout': 29}, {'yt1d_timeout': 1801}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_settings({'download_method': 'yt1d', **invalid})

    def test_website_errors_have_retry_semantics_and_no_raw_response(self):
        for step in ('analyze', 'convert'):
            error = response_error(step, 502, {'result': 'PRIVATE_TOKEN'}, 'yt1d')
            self.assertIsInstance(error, WebsiteHTTPError)
            self.assertTrue(error.retryable)
            self.assertIn('yt1d', str(error))
            self.assertNotIn('PRIVATE_TOKEN', str(error))
        for status in ('busy', 'timed_out_error', 'not_response'):
            self.assertTrue(response_error('convert', 200, {'status': status}, 'yt1d').retryable)
        self.assertIsNone(response_error('convert', 200, {'status': 'processing'}, 'yt1d'))
        error = response_error('analyze', 200, {'result': 'Verification failed PRIVATE_TOKEN'}, 'yt1d')
        self.assertIn('yt1d', str(error))
        self.assertNotIn('PRIVATE_TOKEN', str(error))


class WebsiteContractTests(unittest.TestCase):
    """Exercise actual Chromium, DOM selection and the site's AJAX lifecycle."""
    def test_normal_form_conversion_polling_and_highest_available_quality(self):
        try:
            executable = browser_executable()
        except RuntimeError:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as driver:
                executable = driver.chromium.executable_path
            if not Path(executable).is_file():
                self.skipTest('Chrome/Chromium required for website fixture')
        payloads = []

        class Website(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b'''<input id="txt-url"><button id="btn-submit">Go</button><div id="result"></div>
                <script>
                const result = document.querySelector('#result');
                async function analyze() {
                  const r = await fetch('/mates/en/analyze/ajax', {method:'POST'});
                  const data = await r.json(); result.innerHTML = data.result;
                  document.querySelectorAll('[data-convert]').forEach(b => b.onclick = async () => {
                    const r = await fetch('/mates/en/convert', {method:'POST',body:b.dataset.convert});
                    await r.json();
                    setTimeout(() => fetch('/mates/en/convert/status', {method:'POST'}), 50);
                  });
                }
                document.querySelector('#btn-submit').onclick = analyze;
                document.querySelector('#txt-url').onkeypress = e => { if(e.key === 'Enter') analyze(); };
                </script>'''
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                payloads.append((self.path, body))
                if '/analyze/' in self.path:
                    data = {'status': 'success', 'result': '''<h2 id="video_title">Fixture title</h2>
                    <img class="img-thumbnail" src="https://cdn.example.com/cover.jpg">
                    <div class="caption"><p>Duration: 0:19</p></div><table>
                    <tr><td>720p</td><td><button data-convert="720">Download</button></td></tr>
                    <tr><td>2160p60</td><td><button data-convert="2160">Download</button></td></tr>
                    <tr><td>1080p</td><td><button data-convert="1080">Download</button></td></tr>
                    </table>'''}
                elif self.path.endswith('/status'):
                    data = {'status': 'success', 'downloadUrlX': MEDIA}
                else:
                    data = {'status': 'processing', 'type': 'converting', 'percent': '25%'}
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Website)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        args = SimpleNamespace(video_id='jNQXAC9IVRw', quality='best', proxy='', timeout=10,
                               browser_executable=executable, headless=True, provider='yt1d',
                               site_url=f'http://127.0.0.1:{server.server_port}/')
        with tempfile.TemporaryDirectory() as temporary:
            info = asyncio.run(probe(args, Path(temporary), {'responses': []}, download=False))
        self.assertEqual((info['height'], info['duration'], info['title']), (2160, 19, 'Fixture title'))
        self.assertEqual(info['url'], MEDIA)
        self.assertEqual(next(body for path, body in payloads if path == '/mates/en/convert'), b'2160')


class WebTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def save(self, **values):
        response = self.client.put('/api/config', json={'download_methods': ['yt1d'], **values}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)

    def test_page_settings_job_policy_hot_update_and_missing_environment(self):
        self.save(yt1d_quality='1080', yt1d_timeout=90)
        self.assertIn('name="yt1d_quality"', self.client.get('/').text)
        response = self.client.post('/api/jobs', json={'customer': 'A', 'name': 'Yt1d',
            'urls': 'jNQXAC9IVRw\nnpNnbj46_ik'}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        identity = response.json['id']
        with patch('app.require_yt1d_environment'), \
                patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie')):
            response = self.client.post(f'/api/jobs/{identity}/control', json={'action': 'start'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        first = self.store.claim(self.store.owner, actions=('download',))
        self.assertEqual(first['payload']['config']['yt1d_quality'], '1080')
        self.save(yt1d_quality='2160', yt1d_timeout=120)
        second = self.store.claim(self.store.owner, actions=('download',))
        self.assertEqual(second['payload']['config']['yt1d_quality'], '2160')
        self.assertEqual(second['payload']['config']['yt1d_timeout'], 120)
        self.assertEqual(self.store.get(first['id'], private=True)['payload']['config']['yt1d_quality'], '1080')
        from webapp.yt5s import BrowserEnvironmentRequired
        with patch('app.require_yt1d_environment', side_effect=BrowserEnvironmentRequired('yt1d missing browser')):
            response = self.client.post('/api/downloads', json={'urls': '2Gu7q-KUXCk'}, headers=self.headers)
        self.assertEqual(response.status_code, 409, response.json)

    def test_real_transfer_test_cleanup_and_invalid_media_fallback_bill_only_good_file(self):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('FFmpeg required for media fixture')
        source = self.root / 'source'
        source.mkdir()
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'color=c=black:s=64x64:d=0.3', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
            '-shortest', '-c:v', 'libx264', '-c:a', 'aac', str(source / 'good.mp4')], check=True)
        (source / 'bad.mp4').write_text('<html>Upstream failure</html>', encoding='utf-8')
        from PIL import Image
        Image.new('RGB', (16, 16)).save(source / 'cover.jpg')

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(source)))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host = f'http://127.0.0.1:{server.server_port}'
        size = (source / 'good.mp4').stat().st_size
        for mode, video_id, bad in [('local', 'jNQXAC9IVRw', False), ('test', 'npNnbj46_ik', False),
                                    ('local', '2Gu7q-KUXCk', True)]:
            with self.subTest(mode=mode, bad=bad):
                self.save(storage_mode=mode, download_methods=['yt1d', 'downmp3'] if bad else ['yt1d'])
                response = self.client.post('/api/jobs', json={'customer': 'A', 'name': video_id,
                    'urls': video_id, 'price': '1', 'unit': 'GB'}, headers=self.headers)
                self.assertEqual(response.status_code, 201, response.json)
                identity = response.json['id']
                with patch('app.require_yt1d_environment'):
                    response = self.client.post(f'/api/jobs/{identity}/control', json={'action': 'start'}, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.json)
                task = self.store.claim(self.store.owner, actions=('download',))
                info = {'id': video_id, 'title': 'Fixture', 'url': host + ('/bad.mp4' if bad else '/good.mp4'),
                        'ext': 'mp4', 'thumbnail': host + '/cover.jpg', 'extractor': 'youtube', 'extractor_key': 'Youtube'}
                with patch('webapp.yt1d.require_environment'), patch('webapp.yt1d.resolve', return_value=info), \
                        patch('webapp.downmp3.resolve', return_value={**info, 'url': host + '/good.mp4'}), \
                        patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie')):
                    result = execute('download', task['payload'], store=self.store, task_id=task['id'])
                self.store.finish(task['id'], result=result, worker_id=self.store.owner)
                self.assertEqual((result['file_count'], result['file_bytes']), (1, size))
                self.assertEqual(result['download_method'], 'downmp3' if bad else 'yt1d')
                if mode == 'test':
                    self.assertTrue(result['files_deleted'])
                detail = self.client.get('/api/jobs/' + identity).json
                self.assertEqual(detail['file_bytes'], 0 if mode == 'test' else size)
        rows = {r['method']: r for r in self.client.get('/api/statistics').json['methods']}
        self.assertEqual((rows['yt1d']['succeeded'], rows['yt1d']['test_succeeded'], rows['yt1d']['failed']), (1, 1, 1))
        self.assertEqual((rows['yt1d']['file_bytes'], rows['yt1d']['test_file_bytes']), (size, size))
        self.assertEqual(rows['downmp3']['file_bytes'], size)
        self.assertEqual(len(self.client.get('/api/media').json['items']), 2)
        self.assertIn('yt1d', self.client.get('/api/statistics/methods/export').data.decode('utf-8-sig'))
