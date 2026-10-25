"""Ordered providers, excluded providers, preflight and retry-safe accounting."""
from contextlib import ExitStack
from itertools import permutations
from io import BytesIO
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

import yt_dlp

from tests.test_yt5s import Yt5sTests, VIDEO
from tests.test_youtube_auth import COOKIE
from web_worker import execute
from webapp.settings import download_methods


class ProviderPriorityTests(unittest.TestCase):
    setUp = Yt5sTests.setUp

    def save(self, methods):
        response = self.client.put('/api/config', json={'download_methods': methods}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)

    def job(self):
        self.store.create('download', {'base': str(self.root), 'urls': [VIDEO]})
        return self.store.claim(self.store.owner, actions=('download',))

    def rows(self):
        return {row['method']: row for row in self.client.get('/api/statistics').json['methods']}

    def run_chain(self, methods, winner=None):
        self.save(methods)
        job = self.job()
        events, values = [], []

        def resolver(provider):
            def resolve(*args, **kwargs):
                events.append(provider)
                if provider != winner:
                    raise RuntimeError(provider + ' fixture HTTP 503')
                return {'id': 'npNnbj46_ik'}
            return resolve

        def native(downloader, addresses):
            self.assertEqual(addresses, [VIDEO])
            events.append('yt_dlp')
            if winner != 'yt_dlp':
                raise yt_dlp.utils.DownloadError('native fixture HTTP 503')
            return 0

        with ExitStack() as stack:
            stack.enter_context(patch('webapp.youtube_auth.YouTubeAuth.require'))
            stack.enter_context(patch('webapp.downmp3.resolve', side_effect=resolver('downmp3')))
            stack.enter_context(patch('webapp.yt5s.resolve', side_effect=resolver('yt5s')))
            stack.enter_context(patch.object(yt_dlp.YoutubeDL, 'download', native))
            stack.enter_context(patch.object(yt_dlp.YoutubeDL, 'process_ie_result'))
            if winner:
                result = execute('download', job['payload'], values.append, store=self.store, task_id=job['id'])
                self.store.finish(job['id'], result=result, worker_id=self.store.owner)
                self.assertEqual(result['download_method'], winner)
            else:
                with self.assertRaises(RuntimeError):
                    execute('download', job['payload'], values.append, store=self.store, task_id=job['id'])
                self.store.finish(job['id'], error='all failed', worker_id=self.store.owner)
        return events, values

    def test_all_orders_stop_after_success_and_exclude_unselected_providers(self):
        for length in (1, 2, 3):
            for methods in permutations(('downmp3', 'yt5s', 'yt_dlp'), length):
                for winner in methods:
                    with self.subTest(methods=methods, winner=winner):
                        events, values = self.run_chain(list(methods), winner)
                        self.assertEqual(events, list(methods[:methods.index(winner) + 1]))
                        self.assertEqual(values[-1]['download_method'], winner)

    def test_three_failures_retry_and_provider_reordering_have_exact_counts(self):
        self.save(['downmp3', 'yt5s', 'yt_dlp'])
        job = self.job()
        identity = job['payload']['download_session_id']
        for provider in ('yt5s', 'yt_dlp'):
            self.store.statistics.methods.fallback(job['id'], provider)
        self.store.release(job['id'], self.store.owner, 'retry', delay=0)
        for attempt in range(2):
            job = self.store.claim(self.store.owner, actions=('download',))
            self.assertEqual(job['payload']['download_session_id'], identity)
            for provider in ('yt5s', 'yt_dlp'):
                self.store.statistics.methods.fallback(job['id'], provider)
            self.store.release(job['id'], self.store.owner, 'retry', delay=0)
        self.save(['yt5s', 'downmp3'])
        job = self.store.claim(self.store.owner, actions=('download',))
        self.store.statistics.methods.fallback(job['id'], 'downmp3')
        self.store.finish(job['id'], result={}, worker_id=self.store.owner)
        rows = self.rows()
        self.assertEqual((rows['yt5s']['attempted'], rows['yt5s']['failed'], rows['yt5s']['running']), (1, 1, 0))
        self.assertEqual((rows['downmp3']['attempted'], rows['downmp3']['succeeded'], rows['downmp3']['failed']), (1, 1, 0))
        self.assertEqual((rows['yt_dlp']['attempted'], rows['yt_dlp']['running'], rows['yt_dlp']['unfinished']), (1, 0, 1))
        self.assertTrue(all(row['running'] >= 0 for row in rows.values()))

    def test_all_failed_counts_each_provider_and_one_failed_video(self):
        events, _ = self.run_chain(['yt_dlp', 'downmp3', 'yt5s'])
        self.assertEqual(events, ['yt_dlp', 'downmp3', 'yt5s'])
        self.assertTrue(all((row['attempted'], row['failed'], row['running']) == (1, 1, 0) for row in self.rows().values()))
        self.assertEqual(self.client.get('/api/statistics').json['summary']['failed'], 1)

    def test_validation_is_atomic_and_legacy_configuration_loads(self):
        self.save(['downmp3', 'yt5s'])
        before = self.config.read_bytes()
        for methods in ([], ['downmp3', 'downmp3'], ['yt5s_fallback'], ['unknown'], 'yt5s', [1]):
            response = self.client.put('/api/config', json={'download_methods': methods}, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(download_methods({'download_method': 'yt5s_fallback'}), ['yt5s', 'yt_dlp'])

    def test_preflight_checks_only_selected_dependencies_and_cookie(self):
        self.save(['downmp3', 'yt5s'])
        with patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie')):
            response = self.client.post('/api/downloads', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        self.store.set_downloads_paused(True)
        self.save(['downmp3', 'yt_dlp'])
        response = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.store.downloads_paused())
        upload = self.client.post('/api/youtube/cookie', headers=self.headers,
            data={'cookie': (BytesIO(COOKIE), 'cookies.txt')})
        self.assertEqual(upload.status_code, 201, upload.json)
        with patch('webapp.yt5s.require_browser_environment', side_effect=AssertionError('no browser')), \
                patch('webapp.youtube_auth.probe', return_value=('valid', 'fixture verified')):
            response = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)

    def test_transfer_failure_switches_and_restarts_partial_file_without_cookie(self):
        self.save(['downmp3', 'yt5s'])
        job = self.job()
        options = []

        def transfer(downloader, info, **kwargs):
            options.append(downloader.params)
            if len(options) == 1:
                raise yt_dlp.utils.DownloadError('fixture HTTP 403')

        with patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie')), \
                patch('webapp.downmp3.resolve', return_value={'id': 'npNnbj46_ik'}), \
                patch('webapp.yt5s.resolve', return_value={'id': 'npNnbj46_ik'}), \
                patch.object(yt_dlp.YoutubeDL, 'process_ie_result', transfer):
            result = execute('download', job['payload'], store=self.store, task_id=job['id'])
        self.assertEqual(result['download_method'], 'yt5s')
        self.assertTrue(result['fallback_used'])
        self.assertFalse(options[1]['continuedl'])
        self.assertTrue(all('cookiefile' not in item for item in options))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
    def test_invalid_downmp3_file_is_replaced_by_real_yt5s_transfer_and_billed_once(self):
        source = self.root / 'http'
        source.mkdir()
        (source / 'bad.mp4').write_bytes(b'<html>upstream error</html>')
        subprocess.run([shutil.which('ffmpeg'), '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=black:s=64x64:d=0.3', '-f', 'lavfi', '-i',
            'anullsrc=r=44100:cl=mono', '-shortest', '-c:v', 'libx264', '-c:a', 'aac',
            str(source / 'good.mp4')], check=True)

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(source)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.save(['downmp3', 'yt5s'])
        size = (source / 'good.mp4').stat().st_size
        for mode in ('local', 'test'):
            saved = self.client.put('/api/config', json={'storage_mode': mode}, headers=self.headers)
            self.assertEqual(saved.status_code, 200, saved.json)
            job = self.job()
            host = f'http://127.0.0.1:{server.server_port}'
            info = dict(id='npNnbj46_ik', title='Same filename', ext='mp4', extractor='youtube', extractor_key='Youtube')
            with patch('webapp.downmp3.resolve', return_value={**info, 'url': host + '/bad.mp4'}), \
                    patch('webapp.yt5s.resolve', return_value={**info, 'url': host + '/good.mp4'}):
                result = execute('download', job['payload'], store=self.store, task_id=job['id'])
            self.store.finish(job['id'], result=result, worker_id=self.store.owner)
            self.assertEqual((result['file_count'], result['file_bytes'], result['download_method']), (1, size, 'yt5s'))
            self.assertEqual(result['billable_bytes'], size if mode == 'local' else 0)
            if mode == 'test':
                self.assertTrue(result['files_deleted'])
        rows = self.rows()
        self.assertEqual((rows['downmp3']['failed'], rows['downmp3']['file_bytes']), (2, 0))
        self.assertEqual((rows['yt5s']['succeeded'], rows['yt5s']['test_succeeded']), (1, 1))
        self.assertEqual((rows['yt5s']['file_bytes'], rows['yt5s']['test_file_bytes']), (size, size))
