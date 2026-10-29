"""Bergung website resolution, provider routing and verified delivery."""
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
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as BrowserError

from tests import test_scale
from webapp.bergung import BergungError, open_site, probe, resolve, result_info
from webapp.downloader import build_ydl_options
from webapp.settings import validate_settings
from webapp.yt5s import browser_executable
from webapp.yt5s_browser import NativeBrowser
from web_worker import execute


VIDEO = 'https://www.youtube.com/watch?v=QkCWK_YJ7uY'
CDN = 'https://cdn.example.com/video.webm?token=PRIVATE_TOKEN'


class ResultTests(unittest.TestCase):
    def test_result_types_and_untrusted_urls(self):
        info = result_info({'status': 'tunnel', 'url': CDN, 'filename': 'Example title.webm'},
                           'QkCWK_YJ7uY', 'max')
        self.assertEqual((info['url'], info['ext'], info['title']), (CDN, 'webm', 'Example title'))
        self.assertEqual(info['extractor_key'], 'Youtube')
        for data in ({'status': 'redirect', 'url': 'http://127.0.0.1/video.mp4', 'filename': 'video.mp4'},
                     {'status': 'tunnel', 'url': CDN, 'filename': 'video.exe'},
                     {'status': 'picker', 'picker': []},
                     {'status': 'local-processing', 'tunnel': [CDN]},
                     {'status': 'error', 'error': {'code': 'error.api.auth.jwt.missing'}}):
            with self.subTest(data=data['status']), self.assertRaises(BergungError) as caught:
                result_info(data, 'QkCWK_YJ7uY', 'max')
            self.assertNotIn('PRIVATE_TOKEN', str(caught.exception))
        with self.assertRaises(BergungError) as caught:
            result_info({'status': 'error', 'error': {'code': 'error.api.upstream'}},
                        'QkCWK_YJ7uY', 'max', 503)
        self.assertTrue(caught.exception.retryable)

    def test_configuration_and_cookie_policy(self):
        validate_settings({'download_method': 'bergung', 'bergung_quality': '4320', 'bergung_timeout': 300})
        for values in ({'bergung_quality': '999'}, {'bergung_timeout': 29}, {'bergung_timeout': 1801}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_settings({'download_method': 'bergung', **values})
        options = build_ydl_options({'download_method': 'bergung', 'youtube_cookie': 'missing.txt'}, Path('.'))
        self.assertNotIn('cookiefile', options)
        self.assertEqual(options['format'], 'best')
        self.assertFalse(options['writesubtitles'])

    def test_browser_capacity_proxy_and_quality(self):
        config = {'bergung_quality': '2160', 'bergung_timeout': 240, 'proxy_mode': 'clash',
                  '_resolved_clash_proxy': 'http://127.0.0.1:19001', '_resolved_clash_node': 'fixture',
                  'youtube_cookie': 'missing.txt'}

        async def checked(args, owner, on_stage):
            self.assertEqual((args.quality, args.timeout, args.proxy),
                             ('2160', 240, 'http://127.0.0.1:19001'))
            self.assertEqual(args.site_url, 'https://bergung.hoffnungfuerdiezukunft.net/')
            return {'id': 'QkCWK_YJ7uY', 'url': CDN}

        capacity = lambda: 3
        with patch('webapp.bergung.require_environment', return_value='/browser'), \
                patch('webapp.bergung.session') as session, patch('webapp.bergung.probe', side_effect=checked):
            result = resolve(VIDEO, config, Path('.'), Path('state'), worker_slot=2, browser_capacity=capacity)
        self.assertEqual(result['id'], 'QkCWK_YJ7uY')
        self.assertIs(session.return_value.use.call_args.args[1], capacity)
        self.assertEqual(session.return_value.use.call_args.args[4][-1], 'fixture')

    def test_browser_connection_closed_is_retryable(self):
        with patch('webapp.bergung.require_environment', return_value='/browser'), \
                patch('webapp.bergung.session'), \
                patch('webapp.bergung.probe', side_effect=BrowserError(
                    'Page.goto: net::ERR_CONNECTION_CLOSED at https://bergung.hoffnungfuerdiezukunft.net/settings/video')):
            with self.assertRaises(BergungError) as caught:
                resolve(VIDEO, {'proxy_mode': 'direct'}, Path('.'), Path('state'))
        self.assertTrue(caught.exception.retryable)
        self.assertIn('当前网络或代理节点', str(caught.exception))
        self.assertNotIn('Page.goto', str(caught.exception))

    def test_transient_navigation_is_retried_once(self):
        page = SimpleNamespace(goto=AsyncMock(side_effect=[BrowserError('net::ERR_CONNECTION_CLOSED'),
                                                    SimpleNamespace(status=200)]))
        with patch('webapp.bergung.asyncio.sleep', new_callable=AsyncMock) as sleep:
            response = asyncio.run(open_site(page, 'https://bergung.hoffnungfuerdiezukunft.net/settings/video'))
        self.assertEqual(response.status, 200)
        self.assertEqual(page.goto.await_count, 2)
        sleep.assert_awaited_once_with(1)

    def test_merge_contract_rejects_unsafe_or_incomplete_tasks(self):
        data = {'status':'local-processing', 'type':'merge', 'service':'youtube',
                'tunnel':[CDN, 'https://cdn.example.com/audio.m4a'], 'output':{'filename':'Video.mp4'}}
        info = result_info(data, 'QkCWK_YJ7uY', '360')
        self.assertTrue(info['_cobalt_merge'])
        self.assertEqual([item['vcodec'] for item in info['formats']], ['unknown','none'])
        for invalid in ({**data,'type':'transcode'}, {**data,'tunnel':[CDN]},
                        {**data,'tunnel':[CDN,'http://127.0.0.1/audio']}, {**data,'output':None}):
            with self.subTest(invalid=invalid), self.assertRaises(BergungError):
                result_info(invalid, 'QkCWK_YJ7uY', '360')

    def test_video_error_is_typed_without_poisoning_upstream(self):
        with self.assertRaises(BergungError) as caught:
            result_info({'status':'error','error':{'code':'error.api.content.video.unavailable'}}, 'QkCWK_YJ7uY', '360')
        self.assertEqual(caught.exception.failure_scope, 'video')

    def test_auto_parse_deadline_cancels_work_and_releases_browser_session(self):
        cancelled = []
        async def stuck(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        with patch('webapp.bergung.require_environment',return_value='/browser'), \
             patch('webapp.bergung.session') as session, patch('webapp.bergung.probe',side_effect=stuck):
            with self.assertRaises(BergungError) as caught:
                resolve(VIDEO, {'shared_parser':False,'_resolver_deadline':1}, Path('.'), Path('state'))
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(cancelled,[True])
        self.assertTrue(session.return_value.use.return_value.__exit__.called)


class WebsiteTests(unittest.TestCase):
    def test_chromium_page_settings_and_cobalt_response(self):
        try:
            executable = browser_executable()
        except RuntimeError:
            self.skipTest('Chrome/Chromium required for website fixture')
        requests = []
        widget = [True]

        class Website(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == '/settings/video':
                    body = b'''<button id="setting-button-save-videoQuality-max" aria-pressed="false">max</button>
                    <button id="setting-button-save-youtubeVideoCodec-av1" aria-pressed="false">av1</button>
                    <button id="setting-button-save-youtubeVideoContainer-auto" aria-pressed="true" style="pointer-events:none">auto</button>
                    <script>document.querySelectorAll('button').forEach(button => button.onclick = () => button.setAttribute('aria-pressed', 'true'));</script>'''
                elif self.path == '/settings/local':
                    body = b'''<button id="setting-button-save-localProcessing-disabled" aria-pressed="false">disabled</button>
                    <script>document.querySelector('button').onclick = event => event.target.setAttribute('aria-pressed', 'true');</script>'''
                else:
                    body = b'''<input id="link-area" aria-label="link input area">
                    <button id="setting-button-save-downloadMode-auto" aria-pressed="true" style="pointer-events:none">auto</button>
                    <button id="download-button">download</button><script>
                    setTimeout(() => {
                      const token = document.createElement('input');
                      token.name = 'cf-turnstile-response';
                      token.value = 'fixture-token';
                      document.body.append(token);
                    }, 600);
                    document.querySelector('#download-button').onclick = async () => {
                      await fetch('/', {method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({url: document.querySelector('#link-area').value,
                          token: document.querySelector('input[name=cf-turnstile-response]')?.value || ''})});
                    };
                    </script>'''
                if not widget[0]:
                    body = body.replace(b"token.name = 'cf-turnstile-response';", b"token.name = 'unused';")
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'status': 'redirect', 'url': CDN, 'filename': 'Example title.webm'}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Website)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        root = f'http://127.0.0.1:{server.server_port}/'
        args = SimpleNamespace(video_id='QkCWK_YJ7uY', quality='max', timeout=10, proxy='',
                               browser_executable=executable, site_url=root, api_url=root, headless=True)
        owner = NativeBrowser()
        self.addCleanup(owner.close)
        stages = []
        with patch('builtins.print'):
            info = asyncio.run(probe(args, owner, stages.append))
        self.assertEqual(info['url'], CDN)
        self.assertEqual(requests, [{'url': VIDEO, 'token': 'fixture-token'}])
        self.assertIn('waiting_verification', stages)
        self.assertIn('extracting', stages)
        widget[0] = False
        args.verification = 'auto'
        with patch('builtins.print'):
            self.assertEqual(asyncio.run(probe(args, owner))['url'], CDN)
        self.assertEqual(requests[-1]['token'], '')
        widget[0] = True
        # Exercise the real browser through the local broker twice: these are
        # separate client requests and must reuse the same Chromium process.
        from webapp.parser_broker import ParserBroker, resolve_remote
        with tempfile.TemporaryDirectory() as directory:
            broker = ParserBroker(Path(directory), lambda: {'worker_count':4,'yt5s_browser_count':1,
                                  'disk_min_free_gb':0,'disk_resume_free_gb':0})
            broker.start()
            pids = []
            original_start = NativeBrowser.start
            async def headless_start(browser, options):
                options.headless = True
                address = await original_start(browser, options)
                pids.append(browser.process.pid)
                return address
            try:
                with patch('webapp.bergung.require_environment',return_value=executable), \
                     patch.object(NativeBrowser,'start',headless_start), patch('builtins.print'):
                    config={'proxy_mode':'direct','_bergung_site_url':root,'_bergung_api_url':root}
                    for _ in range(2):
                        resolved=resolve_remote(VIDEO,config,Path(directory),Path(directory))
                        self.assertEqual(resolved['url'],CDN)
            finally:
                broker.close()
            self.assertEqual(len(pids),2)
            self.assertEqual(pids[0],pids[1])


class WorkflowTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def save(self, methods, **values):
        response = self.client.put('/api/config', json={'download_methods': methods,
                                   'bergung_quality': '720', 'bergung_timeout': 120,
                                   'disk_min_free_gb': 0, 'disk_resume_free_gb': 0,
                                   'min_free_gb': 0, **values},
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)

    def test_page_policy_and_fallback(self):
        import yt_dlp

        self.assertIn('name="bergung_quality"', self.client.get('/').text)
        self.save(['bergung', 'downmp3'])
        task = self.store.create('download', {'urls': [VIDEO]})
        claimed = self.store.claim()
        self.assertEqual(claimed['payload']['config']['bergung_quality'], '720')
        with patch('webapp.bergung.require_environment'), \
                patch('webapp.environment.require_downmp3_environment'), \
                patch('webapp.bergung.resolve', side_effect=BergungError('site failed')) as bergung, \
                patch('webapp.downmp3.resolve', return_value={'id': 'QkCWK_YJ7uY'}) as downmp3, \
                patch.object(yt_dlp.YoutubeDL, 'process_ie_result'), patch('builtins.print'):
            result = execute('download', claimed['payload'], store=self.store, task_id=task['id'])
        self.assertEqual(result['download_method'], 'downmp3')
        self.assertTrue(result['fallback_used'])
        bergung.assert_called_once()
        downmp3.assert_called_once()

    def test_verified_file_is_counted_for_bergung(self):
        self.verified_fixture()

    def test_cobalt_merge_downloads_two_tracks_and_counts_only_verified_output(self):
        self.verified_fixture(merge=True)

    def verified_fixture(self, merge=False):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('FFmpeg required for media fixture')
        source = self.root / 'source'
        source.mkdir()
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'color=c=black:s=64x64:d=0.3', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono',
                        '-shortest', '-c:v', 'libx264', '-c:a', 'aac', str(source / 'fixture.mp4')], check=True)
        if merge:
            for name, selection in (('video.mp4','0:v:0'),('audio.m4a','0:a:0')):
                subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-i',str(source/'fixture.mp4'),
                                '-map',selection,'-c','copy',str(source/name)],check=True)

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(source)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.save(['bergung'])
        task = self.store.create('download', {'urls': [VIDEO]})
        claimed = self.store.claim()
        info = {'id': 'QkCWK_YJ7uY', 'title': 'Fixture', 'url':
                f'http://127.0.0.1:{server.server_port}/fixture.mp4', 'ext': 'mp4',
                'extractor': 'youtube', 'extractor_key': 'Youtube'}
        if merge:
            with patch('webapp.yt1d.media_url',side_effect=lambda value:value):
                info = result_info({'status':'local-processing','type':'merge','service':'youtube',
                    'tunnel':[f'http://127.0.0.1:{server.server_port}/video.mp4',
                              f'http://127.0.0.1:{server.server_port}/audio.m4a'],
                    'output':{'filename':'Fixture.mp4'}}, 'QkCWK_YJ7uY','720')
        with patch('webapp.bergung.require_environment'), \
                patch('webapp.bergung.resolve', return_value=info), patch('builtins.print'):
            result = execute('download', claimed['payload'], store=self.store, task_id=task['id'])
        self.assertEqual(result['download_method'], 'bergung')
        self.assertEqual(result['file_count'], 1)
        if not merge:
            self.assertEqual(result['file_bytes'], (source / 'fixture.mp4').stat().st_size)
        else:
            self.assertGreater(result['file_bytes'], 0)
        self.store.finish(task['id'], result=result, worker_id=self.store.owner)
        rows = {item['method']: item for item in self.client.get('/api/statistics').json['methods']}
        self.assertEqual((rows['bergung']['succeeded'], rows['bergung']['file_bytes']),
                         (1, result['file_bytes']))

    def test_auto_attempt_budget_does_not_walk_all_failed_sites(self):
        self.save(['bergung'], scheduler_mode='auto', auto_channel_attempts=2,
            download_channels=[dict(id='alias'+str(index), api_url=f'https://api{index}.example.com/') for index in range(5)])
        task = self.store.create('download', {'urls':[VIDEO]})
        claimed = self.store.claim()
        with patch('webapp.bergung.require_environment'), patch('builtins.print'), \
             patch('webapp.bergung.resolve',side_effect=BergungError('upstream timed out',retryable=True)) as resolver:
            with self.assertRaises(BergungError):
                execute('download',claimed['payload'],store=self.store,task_id=task['id'])
        self.assertEqual(resolver.call_count, 2)


if __name__ == '__main__':
    unittest.main()
