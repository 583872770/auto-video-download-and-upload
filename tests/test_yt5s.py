"""Provider selection must route real transfers without exposing login cookies."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import create_app
from Utils.runtime import configure_runtime
from webapp.settings import validate_settings
from web_worker import execute

VIDEO = 'https://www.youtube.com/watch?v=npNnbj46_ik'


class Yt5sTests(unittest.TestCase):
    def setUp(self):
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
        self.assertEqual(task['payload']['config']['download_method'], 'yt5s')
        self.assertEqual(self.client.get('/api/status').json['download_method'], 'yt5s')
        bad = self.client.post('/api/downloads', json={'urls': 'https://www.youtube.com/playlist?list=PLtest'}, headers=self.headers)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.store.counts()['total'], 1)
        self.method('yt_dlp')
        result = self.client.post('/api/downloads', json={'urls': '2Gu7q-KUXCk'}, headers=self.headers)
        self.assertEqual(result.status_code, 409)  # Method one still requires login.

    def test_resume_uses_queued_provider_after_default_is_changed(self):
        self.method('yt5s')
        self.client.post('/api/downloads', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
        self.store.set_downloads_paused(True)
        self.method('yt_dlp')
        with patch.object(self.app.extensions['youtube_auth'], 'require', side_effect=AssertionError('queued method two')):
            result = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertFalse(self.store.downloads_paused())
        # The inverse must not let an old method-one task skip login checking.
        self.store.create('download', {'config': {'download_method': 'yt_dlp', 'proxy_mode': 'direct'},
                                       'base': str(self.root), 'urls': ['https://youtu.be/2Gu7q-KUXCk']})
        first = self.store.page(actions=('download',), status='queued', order='asc')['items'][0]
        self.store.finish(first['id'], result={'message': 'fixture complete'})
        self.method('yt5s')
        self.store.set_downloads_paused(True)
        result = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(result.status_code, 409)
        self.assertTrue(self.store.downloads_paused())

    def test_invalid_provider_and_quality_do_not_persist(self):
        before = self.config.read_bytes()
        for values in ({'download_method': 'made-up'}, {'yt5s_quality': '1080'}):
            result = self.client.put('/api/config', json=values, headers=self.headers)
            self.assertEqual(result.status_code, 400)
            self.assertEqual(self.config.read_bytes(), before)

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
            result = execute('download', payload, values.append, store=self.store)
            resolver.assert_called_once()
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
