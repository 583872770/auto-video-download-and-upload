"""Web boundaries, durable jobs, and a real local HTTP background download."""
import functools
import http.server
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.cookiejar import MozillaCookieJar
from unittest.mock import patch

from PIL import Image
import yaml

from app import create_app
from webapp.tasks import ROOT, TaskStore
from web_worker import worker_lock


def picture():
    output = io.BytesIO()
    Image.new('RGB', (20, 12), '#8d9c7a').save(output, format='PNG')
    output.seek(0)
    return output


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\nworker_count: 1\n', encoding='utf-8')
        self.app = create_app(self.config, self.root / 'state', start_worker=False)
        self.app.testing = True
        self.client = self.app.test_client()
        response = self.client.get('/')
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', response.text)[1]
        self.headers = {'X-CSRF-Token': self.token}
        self.store = self.app.extensions['task_store']

    def post_task(self, action, **extra):
        return self.client.post('/api/tasks', json={'action': action, **extra}, headers=self.headers)

    def import_video(self, cover=None):
        return self.client.post('/api/media', data={
            'video': (io.BytesIO(b'local-video-fixture'), '我的世界.v2.mp4'),
            'cover': (cover or picture(), 'other-name.png'),
        }, headers=self.headers)

    def test_page_assets_and_read_endpoints(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        for url in ('/static/app.js', '/static/app.css', '/api/config', '/api/status', '/api/media', '/api/tasks'):
            with self.client.get(url) as response:
                self.assertEqual(response.status_code, 200, url)
        self.assertIn("script-src 'self'", self.client.get('/').headers['Content-Security-Policy'])
        self.assertEqual(self.client.get('/api/media').json['items'], [])

    def test_csrf_and_host_boundaries(self):
        self.assertEqual(self.client.post('/api/tasks', json={'action': 'check'}).status_code, 403)
        self.assertEqual(self.client.post('/api/tasks', json={'action': 'check'}, headers={'X-CSRF-Token': 'wrong'}).status_code, 403)
        self.assertEqual(self.client.get('/', base_url='http://untrusted.example').status_code, 400)
        self.assertEqual(self.client.get('/media/covers/..%5cconfig.yaml').status_code, 404)

    def test_settings_roundtrip_and_invalid_write_is_atomic(self):
        response = self.client.put('/api/config', json={'worker_count': 2}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        saved = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        self.assertEqual(saved['worker_count'], 2)
        before = self.config.read_bytes()
        for invalid in ({'worker_count': 0}, {'download_method': 'invalid'}, {'path': ''}, {'unknown': 1}):
            response = self.client.put('/api/config', json=invalid, headers=self.headers)
            self.assertEqual(response.status_code, 400, invalid)
            self.assertEqual(self.config.read_bytes(), before)

    def test_task_validation_and_publish_confirmation(self):
        self.assertEqual(self.post_task('shell').status_code, 400)
        self.assertEqual(self.post_task('publish').status_code, 400)
        self.assertEqual(self.post_task('login', platforms=['xigua']).status_code, 400)
        self.assertEqual(self.post_task('login', platforms=['douyin', 'kuaishou']).status_code, 400)
        self.assertEqual(self.post_task('download', urls='file:///private').status_code, 400)
        self.assertEqual(len(self.store.list()), 0)

    def test_pending_task_binds_settings_when_execution_starts(self):
        task = self.post_task('download', urls='https://example.com/video.mp4').json
        self.client.put('/api/config', json={'path': 'another-folder'}, headers=self.headers)
        stored = TaskStore(self.root / 'state').get(task['id'], private=True)
        self.assertNotIn('config', stored['payload'])
        started = self.store.claim(self.store.owner)
        self.assertEqual(started['payload']['config']['path'], 'another-folder')
        self.assertEqual(stored['payload']['base'], str(self.root))
        self.assertNotIn('payload', self.client.get(f'/api/tasks/{task["id"]}').json)
        self.assertEqual(self.client.get('/api/tasks/no-such-task/logs').status_code, 404)

    def test_download_scope_hides_unrelated_history_and_counts(self):
        payload = {'config': {}, 'base': str(self.root)}
        self.store.create('legacy', payload)
        download = self.post_task('download', urls='https://example.com/video.mp4').json
        self.post_task('check', scope='download')
        response = self.client.get('/api/tasks?scope=download').json
        self.assertEqual({task['action'] for task in response['items']}, {'download', 'check'})
        self.assertEqual(response['counts']['total'], 2)
        self.assertEqual(self.client.get('/api/tasks').json['counts']['total'], 3)
        self.assertIn(download['id'], {task['id'] for task in response['items']})

    def test_settings_save_keeps_only_download_fields(self):
        original = yaml.safe_load(self.config.read_text())
        original['legacy_publish_field'] = 'removed'
        self.config.write_text(yaml.safe_dump(original), encoding='utf-8')
        response = self.client.put('/api/config', json={'dir': 'youtube'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        saved = yaml.safe_load(self.config.read_text())
        self.assertNotIn('legacy_publish_field', saved)
        self.assertEqual(self.post_task('download', urls='https://example.com/video.mp4').status_code, 202)
        self.assertEqual(self.post_task('check', scope='download').status_code, 202)

    def test_import_unicode_pair_collision_and_bad_cover_rollback(self):
        response = self.import_video()
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json['name'], '我的世界.v2.mp4')
        self.assertEqual(self.client.get('/api/media').json['items'][0]['cover'], '我的世界.v2.png')
        with self.client.get('/media/covers/我的世界.v2.png') as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.import_video().status_code, 409)
        (self.root / 'videos' / '我的世界.v2.mp4').unlink()
        (self.root / 'videos' / '我的世界.v2.png').unlink()
        self.assertEqual(self.import_video(io.BytesIO(b'not-an-image')).status_code, 400)
        self.assertEqual(list((self.root / 'videos').iterdir()), [])

    def test_queued_work_blocks_media_mutation(self):
        self.post_task('check')
        self.assertEqual(self.import_video().status_code, 409)

    def test_format_tracks_are_one_item_with_shared_cover_and_finished_video_wins(self):
        folder = self.root / 'videos'
        folder.mkdir()
        stem = '测试视频 [abcdefghijk]'
        video = folder / f'{stem}.f136.mp4'
        audio = folder / f'{stem}.f251.webm'
        video.write_bytes(b'video-track')
        audio.write_bytes(b'audio-track')
        (folder / f'{stem}.info.json').write_text(json.dumps({'title': '测试视频'}), encoding='utf-8')
        (folder / f'{stem}.webp').write_bytes(picture().getvalue())
        self.app.extensions['media_catalog'].reindex(folder)
        items = self.client.get('/api/media').json['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['cover'], f'{stem}.webp')
        self.assertFalse(items[0]['playable'])
        self.assertEqual(items[0]['status'], 'incomplete')
        finished = folder / f'{stem}.mp4'
        finished.write_bytes(b'finished-video')
        self.app.extensions['media_catalog'].reindex(folder)
        items = self.client.get('/api/media').json['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], finished.name)
        self.assertEqual(items[0]['title'], '测试视频')
        self.assertTrue(items[0]['playable'])
        self.assertEqual(video.read_bytes(), b'video-track')
        self.assertEqual(audio.read_bytes(), b'audio-track')

    def test_video_stream_supports_ranges_unicode_and_blocks_non_video_paths(self):
        self.import_video()
        url = '/media/videos/我的世界.v2.mp4'
        with self.client.get(url, headers={'Range': 'bytes=6-10'}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, b'video')
            self.assertEqual(response.mimetype, 'video/mp4')
            self.assertEqual(response.headers['Content-Range'], 'bytes 6-10/19')
        with self.client.head(url) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Content-Length'], '19')
        for target in ('..%5cconfig.yaml', '我的世界.v2.png', 'missing.mp4', '..%5coutside.mp4'):
            self.assertEqual(self.client.get('/media/videos/' + target).status_code, 404)
        for kind, suffix in (('videos', '.mp4'), ('covers', '.png')):
            (self.root / 'videos' / ('part..final' + suffix)).write_bytes(b'valid-file')
            with self.client.get('/media/' + kind + '/part..final' + suffix) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, b'valid-file')

    def test_youtube_merge_requires_ffmpeg_but_direct_file_download_does_not(self):
        from webapp.downloader import build_ydl_options, require_download_tools
        options = build_ydl_options({}, self.root)
        with patch('webapp.downloader.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'FFmpeg'):
                require_download_tools(['https://youtu.be/abcdefghijk'], options)
            require_download_tools(['https://example.com/video.mp4'], options)
            require_download_tools(['https://www.youtube.com/watch?v=abcdefghijk'], {'format': 'best'})

    def test_saved_account_state_does_not_expose_credentials(self):
        response = self.client.get('/api/status')
        self.assertEqual(response.json['accounts'], {})

    def upload_cookie(self, content, filename='cookies.txt', headers=None):
        response = self.client.post('/api/youtube/cookie', data={'cookie': (io.BytesIO(content), filename)},
                                    headers=self.headers if headers is None else headers)
        response.request.environ['wsgi.input'].close()
        return response

    def test_youtube_cookie_upload_configures_downloader_without_exposing_values(self):
        content = (b'# Netscape HTTP Cookie File\n'
                   b'#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tDO_NOT_EXPOSE\n'
                   b'.google.com\tTRUE\t/\tTRUE\t0\tSID\tOTHER_SECRET\n'
                   b'.youtube.com\tTRUE\t/\tTRUE\t1\told\tEXPIRED_SECRET\n')
        response = self.upload_cookie(content, '../../custom-name.txt')
        self.assertEqual(response.status_code, 201, response.json)
        target = self.root / 'Cookies' / 'youtube.txt'
        jar = MozillaCookieJar(str(target))
        jar.load(ignore_discard=True, ignore_expires=True)
        self.assertEqual([(entry.domain, entry.name, entry.value) for entry in jar],
                         [('.youtube.com', 'SID', 'DO_NOT_EXPOSE')])
        self.assertIsNone(next(iter(jar)).expires)
        self.assertEqual(yaml.safe_load(self.config.read_text())['youtube_cookie'], 'Cookies/youtube.txt')
        with patch.object(self.app.extensions['youtube_auth'], 'require', return_value={'state': 'valid'}):
            task = self.post_task('download', urls='https://www.youtube.com/watch?v=example').json
        from webapp.downloader import build_ydl_options
        payload = self.store.get(task['id'], private=True)['payload']
        payload = self.store.effective_download_payload(task['id'], payload)
        options = build_ydl_options(payload['config'], Path(payload['base']))
        self.assertEqual(options['cookiefile'], str(target))
        for response in (response, self.client.get('/api/status'), self.client.get('/api/config')):
            self.assertNotIn('DO_NOT_EXPOSE', response.text)
        self.assertTrue(self.client.get('/api/status').json['youtube']['saved'])

    def test_invalid_youtube_cookie_keeps_existing_file_and_config(self):
        valid = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprevious\n'
        self.assertEqual(self.upload_cookie(valid).status_code, 201)
        target = self.root / 'Cookies' / 'youtube.txt'
        previous, config_before = target.read_bytes(), self.config.read_bytes()
        invalids = [b'{"cookies": []}', b'# Netscape HTTP Cookie File\nmalformed SECRET_VALUE\n',
                    b'# HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tSID\tonly-google\n',
                    b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tSID\texpired\n',
                    b'# HTTP Cookie File\nnotyoutube.com\tFALSE\t/\tTRUE\t0\tSID\tlookalike\n',
                    b'X' * (1024 * 1024 + 1), b'']
        for content in invalids:
            with self.subTest(length=len(content)):
                response = self.upload_cookie(content)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn('SECRET_VALUE', response.text)
                self.assertEqual(target.read_bytes(), previous)
                self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.upload_cookie(valid, filename='cookies.json').status_code, 400)
        self.assertEqual(self.upload_cookie(valid, headers={}).status_code, 403)

    def test_youtube_cookie_update_blocks_while_task_queued(self):
        self.post_task('check')
        valid = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n'
        self.assertEqual(self.upload_cookie(valid).status_code, 409)
        self.assertFalse((self.root / 'Cookies' / 'youtube.txt').exists())

    def test_youtube_cookie_rolls_back_when_config_save_fails(self):
        valid = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprevious\n'
        self.assertEqual(self.upload_cookie(valid).status_code, 201)
        target = self.root / 'Cookies' / 'youtube.txt'
        before = target.read_bytes()
        with patch('app.save_settings', side_effect=OSError('read-only config')):
            with self.assertLogs(self.app.logger, level='ERROR'):
                response = self.upload_cookie(valid.replace(b'previous', b'new-secret'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(target.parent.glob('.youtube-import-*')), [])

    def test_persistence_recovery_and_utf8_log_cursor(self):
        task = self.post_task('check').json
        self.store.claim()
        self.assertEqual(TaskStore(self.root / 'state').get(task['id'])['status'], 'running')
        self.store.recover(expired_only=False)
        self.assertEqual(self.store.get(task['id'])['status'], 'interrupted')
        self.store.log_path(task['id']).write_bytes(('中文日志\n' * 20000).encode('utf-8'))
        cursor, output = 0, ''
        while True:
            chunk = self.store.logs(task['id'], cursor, size=101)
            if not chunk['text']:
                break
            self.assertGreater(chunk['offset'], cursor)
            cursor = chunk['offset']
            output += chunk['text']
        self.assertEqual(output, '中文日志\n' * 20000)

    def test_worker_lock_excludes_second_process(self):
        lock = self.root / 'exclusive.lock'
        with worker_lock(lock) as held:
            self.assertTrue(held)
            code = 'from pathlib import Path\nfrom web_worker import worker_lock\nimport sys\nwith worker_lock(Path(sys.argv[1])) as acquired:\n print(acquired)'
            result = subprocess.run([sys.executable, '-c', code, str(lock)], cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), 'False')

    def test_real_worker_download_and_failure_keep_queue_moving(self):
        fixture = self.root / 'http'
        fixture.mkdir()
        content = b'local transport fixture, not an encoded video\n' * 100
        (fixture / 'video.mp4').write_bytes(content)

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = functools.partial(QuietHandler, directory=str(fixture))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # First job fails without networking. The next job must still run.
        failure = self.store.create('not-supported', {'config': {}, 'base': str(self.root), 'platforms': []})
        response = self.post_task('download', urls=f'http://127.0.0.1:{server.server_port}/video.mp4')
        self.assertEqual(response.status_code, 202)
        task = response.json
        result = subprocess.run([sys.executable, '-u', str(ROOT / 'web_worker.py'), '--state', str(self.store.directory)],
                                cwd=ROOT, env=dict(os.environ, PYTHONUTF8='1'), capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.get(failure['id'])['status'], 'failed')
        finished = self.store.get(task['id'])
        self.assertEqual(finished['status'], 'succeeded', self.store.logs(task['id'])['text'])
        downloaded = list((self.root / 'videos').rglob('*.mp4'))
        self.assertEqual(len(downloaded), 1)
        self.assertEqual(downloaded[0].read_bytes(), content)
        self.assertIn('任务执行完成', self.store.logs(task['id'])['text'])
        self.assertTrue(self.store.archive_has('generic video'))


if __name__ == '__main__':
    unittest.main()

