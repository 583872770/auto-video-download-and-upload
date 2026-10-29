"""Batch normalization, durable per-video progress and independent failures."""
import functools
import http.server
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import create_app
from webapp.downloads import DownloadFailure, DownloadProgress
from webapp.settings import urls_from, YOUTUBE_PREFIX


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config = self.root / 'config.yaml'
        config.write_text('path: videos\n', encoding='utf-8')
        self.app = create_app(config, self.root / 'state', start_worker=False)
        self.client = self.app.test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]
        self.headers = {'X-CSRF-Token': token}
        self.store = self.app.extensions['task_store']

    def test_ids_are_normalized_deduplicated_and_invalid_batch_is_atomic(self):
        ids = 'npNnbj46_ik\n2Gu7q-KUXCk\nWMu6ANTg8mk\nhI0ofOjFloE\nNJvj1iQdZwE\n8XiDhkvwgEo\nXmyef5q9wSo\np2MeSftrXtM'
        values = ids + '\nnpNnbj46_ik\n' + YOUTUBE_PREFIX + 'npNnbj46_ik'
        with patch.object(self.app.extensions['youtube_auth'], 'require', return_value={'state': 'valid'}):
            response = self.client.post('/api/downloads', json={'urls': values}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(response.json['items']), 8)
        batch_id = response.json['batch_id']
        for task, video_id in zip(response.json['items'], ids.splitlines()):
            self.assertEqual(task['video_id'], video_id)
            self.assertEqual(task['batch_id'], batch_id)
            self.assertEqual(task['url'], YOUTUBE_PREFIX + video_id)
            self.assertEqual(self.store.get(task['id'], private=True)['payload']['urls'], [YOUTUBE_PREFIX + video_id])
        invalid = self.client.post('/api/downloads', json={'urls': 'npNnbj46_ik\nnot-a-valid-id'}, headers=self.headers)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.store.counts()['total'], 8)
        self.assertEqual(self.client.post('/api/downloads', json={'urls': ids}).status_code, 403)
        with self.assertRaises(ValueError):
            urls_from('\n'.join(['npNnbj46_ik'] * 101))

    def test_structured_progress_tracks_audio_video_and_clears_stale_speed(self):
        task = self.store.create('download', {'urls': ['https://example.test/video.mp4']})
        tracker = DownloadProgress(lambda value: self.store.update_progress(task['id'], value))
        info = {'title': 'Video Title', 'format_id': '137', 'vcodec': 'avc1', 'ext': 'mp4',
                'requested_formats': [{'filesize': 1000}, {'filesize': 500}]}
        tracker.begin_video(info)
        info = {key: value for key, value in info.items() if key != 'requested_formats'}
        tracker.hook({'status': 'downloading', 'filename': 'video.mp4', 'downloaded_bytes': 500,
                      'total_bytes': 1000, 'speed': 200, 'eta': 3, 'info_dict': info})
        tracker.save(force=True)
        data = self.client.get(f'/api/tasks/{task["id"]}').json['progress']
        self.assertEqual(data['percent'], 25)
        self.assertEqual(data['downloaded_bytes'], 500)
        self.assertEqual(data['total_bytes'], 1500)
        self.assertEqual(data['speed'], 200)
        tracker.hook({'status': 'finished', 'filename': 'video.mp4', 'downloaded_bytes': 1000,
                      'total_bytes': 1000, 'info_dict': info})
        tracker.hook({'status': 'finished', 'filename': 'audio.m4a', 'downloaded_bytes': 500,
                      'total_bytes': 500, 'info_dict': {**info, 'vcodec': 'none'}})
        tracker.postprocess({'status': 'started', 'info_dict': info})
        data = self.store.get(task['id'])['progress']
        self.assertEqual(data['percent'], 100)
        self.assertEqual(data['stage'], 'processing')
        self.assertEqual(data['downloaded_bytes'], 1500)
        self.assertIsNone(data['speed'])
        tracker.hook({'status': 'finished', 'filename': 'captions.vtt', 'info_dict': {'ext': 'vtt'}})
        self.assertEqual(self.store.get(task['id'])['progress'], data)
        self.store.finish(task['id'], error='download failed')
        failed = self.store.get(task['id'])['progress']
        self.assertEqual(failed['stage'], 'failed')
        self.assertIsNone(failed['speed'])
        self.assertIsNone(failed['eta'])

    def test_unknown_total_does_not_invent_percent_or_speed(self):
        values = []
        tracker = DownloadProgress(values.append)
        tracker.hook({'status': 'downloading', 'downloaded_bytes': 42, 'speed': float('nan'), 'eta': float('inf')})
        tracker.save(force=True)
        self.assertIsNone(values[-1]['percent'])
        self.assertIsNone(values[-1]['speed'])
        self.assertIsNone(values[-1]['eta'])
        json.dumps(values[-1], allow_nan=False)

    def test_cookie_rejection_and_bot_checks_have_distinct_actionable_errors(self):
        failure = DownloadFailure()
        failure.error('[youtube] id: Sign in to confirm you’re not a bot.')
        self.assertIn('代理节点', failure.message())
        self.assertNotIn('已失效', failure.message())
        failure.warning('[youtube] The provided YouTube account cookies are no longer valid.')
        self.assertIn('已失效或被轮换', failure.message())
        self.assertIn('上传新的 Cookie', failure.message())

    def test_worker_preserves_cause_and_uploaded_cookie_on_both_failure_paths(self):
        import yt_dlp
        from web_worker import execute
        cookie = self.root / 'cookies.txt'
        original = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tPRIVATE_VALUE\n'
        cookie.write_bytes(original)
        for raises in (False, True):
            sessions = []

            def download(downloader, urls):
                session = Path(downloader.params['cookiefile'])
                sessions.append(session)
                self.assertNotEqual(session, cookie)
                self.assertEqual(session.read_bytes(), original)
                session.write_text('# Netscape HTTP Cookie File\n', encoding='utf-8')
                downloader.report_warning('The provided YouTube account cookies are no longer valid.')
                if raises:
                    raise yt_dlp.utils.DownloadError('Sign in to confirm you’re not a bot.')
                downloader.report_error('Sign in to confirm you’re not a bot.')
                return 1

            payload = {'base': str(self.root), 'urls': ['https://example.test/video.mp4'],
                       'config': {'youtube_cookie': str(cookie), 'proxy_mode': 'direct'}}
            with self.subTest(raises=raises), patch.object(yt_dlp.YoutubeDL, 'download', download):
                with self.assertRaisesRegex(RuntimeError, 'Cookie 已失效或被轮换'):
                    execute('download', payload)
                self.assertEqual(cookie.read_bytes(), original)
                self.assertTrue(sessions)
                self.assertFalse(sessions[0].exists())

    def test_other_extractor_errors_retain_reason_without_credentials_or_terminal_codes(self):
        failure = DownloadFailure()
        failure.error('\x1b[31mHTTP Error 403: Forbidden https://user:SECRET@host.test/video?token=PRIVATE\x1b[0m')
        message = failure.message()
        self.assertIn('HTTP Error 403: Forbidden', message)
        for secret in ('SECRET', 'PRIVATE', '\x1b'):
            self.assertNotIn(secret, message)

    def test_real_batch_failure_does_not_block_next_video_and_archive_skip_is_visible(self):
        response = self.client.put('/api/config', json={'throughput_mode': 'auto'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        fixture = self.root / 'http'
        fixture.mkdir()
        content = b'local-download-fixture\n' * 100
        (fixture / 'video.mp4').write_bytes(content)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(fixture)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        urls = [f'http://127.0.0.1:{server.server_port}/{name}.mp4' for name in ('missing', 'video')]
        response = self.client.post('/api/downloads', json={'urls': urls}, headers=self.headers)

        def drain():
            subprocess.run([sys.executable, 'web_worker.py', '--state', str(self.root / 'state')],
                           capture_output=True, check=True, timeout=25)

        drain()
        tasks = [self.store.get(item['id']) for item in response.json['items']]
        self.assertEqual([task['status'] for task in tasks], ['failed', 'succeeded'])
        self.assertIn('404', tasks[0]['error'])
        self.assertNotIn('部分视频下载失败', tasks[0]['error'])
        self.assertEqual(tasks[0]['progress']['stage'], 'failed')
        self.assertEqual(tasks[1]['progress']['stage'], 'completed')
        self.assertEqual(tasks[1]['progress']['downloaded_bytes'], len(content))
        self.assertEqual(tasks[1]['progress']['percent'], 100)
        repeated = self.client.post('/api/downloads', json={'urls': urls[1]}, headers=self.headers)
        drain()
        skipped = self.store.get(repeated.json['items'][0]['id'])
        self.assertEqual(skipped['status'], 'succeeded')
        self.assertTrue(skipped['result']['skipped'])
        self.assertEqual(skipped['progress']['stage'], 'skipped')

