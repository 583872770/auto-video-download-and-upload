"""Login controls, cookie replacement and queued downloads on SQLite/MySQL."""
import asyncio
import io
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from playwright.async_api import Error as BrowserError
import yaml

from tests import test_web, test_scale
from webapp.youtube_login import YouTubeLogin, LoginBusy

COOKIE = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-only-secret\n'


class LoginFixture:
    def __init__(self):
        self.ready = threading.Event()
        self.complete = threading.Event()
        self.config = None
        self.failure = None

    async def run(self, config, output, *, on_status, cancelled):
        from scripts.login_youtube import LoginCancelled
        self.config = config
        on_status('waiting', 'Fixture login waiting')
        self.ready.set()
        while not self.complete.is_set():
            if cancelled.is_set():
                raise LoginCancelled('登录已取消，已有 Cookie 保持不变。')
            await asyncio.sleep(0.01)
        if self.failure:
            raise self.failure
        output.write_bytes(COOKIE)


class LoginContract:
    def setUp(self):
        super().setUp()
        self.manager = self.app.extensions['youtube_login']
        self.fixture = LoginFixture()
        patcher = patch('webapp.youtube_login.login', self.fixture.run)
        patcher.start()
        self.addCleanup(patcher.stop)

        def cleanup():
            self.manager.cancel()
            if self.manager.thread:
                self.manager.thread.join(timeout=8)
        self.addCleanup(cleanup)

    def post(self, path='/api/youtube/login', **kwargs):
        return self.client.post(path, headers=self.headers, **kwargs)

    def start(self):
        response = self.post()
        self.assertEqual(response.status_code, 202, response.json)
        self.assertTrue(self.fixture.ready.wait(timeout=5))
        return response.json

    def finish(self):
        self.fixture.complete.set()
        self.manager.thread.join(timeout=8)
        self.assertFalse(self.manager.thread.is_alive())
        return self.client.get('/api/youtube/login').json

    def test_paused_queue_login_success_updates_configuration_and_runtime(self):
        queued = self.store.create('download', {'config': {'youtube_cookie': ''}, 'base': str(self.root)})
        self.assertEqual(self.post().status_code, 409)
        self.store.set_downloads_paused(True)
        self.start()
        self.assertEqual(self.finish()['state'], 'succeeded')
        target = self.root / 'Cookies/youtube.txt'
        self.assertTrue(target.is_file())
        self.assertEqual(yaml.safe_load(self.config.read_text())['youtube_cookie'], 'Cookies/youtube.txt')
        self.assertEqual(self.store.runtime()['youtube_cookie'], str(target))
        self.assertEqual(self.store.get(queued['id'])['status'], 'queued')
        self.assertTrue(self.store.downloads_paused())
        self.assertNotIn('fixture-only-secret', self.client.get('/api/status').text)
        self.assertEqual(list(self.manager.directory.glob('cookies-*')), [])
        # A job created before login must use the refreshed cookie at execution time.
        import yt_dlp
        from web_worker import execute
        captured = []

        def download(downloader, urls):
            captured.append(Path(downloader.params['cookiefile']).read_bytes())
            return 0

        payload = self.store.get(queued['id'], private=True)['payload']
        payload['urls'] = ['https://example.test/fixture.mp4']
        with patch.object(yt_dlp.YoutubeDL, 'download', download):
            execute('download', payload, store=self.store)
        self.assertEqual(captured, [target.read_bytes()])

    def test_duplicate_login_and_conflicting_mutations_are_blocked(self):
        self.start()
        other = YouTubeLogin(self.store.directory, lambda data: None)
        self.assertTrue(other.status()['active'])
        with self.assertRaises(LoginBusy):
            other.start({}, '直连')
        self.assertEqual(self.post().status_code, 409)
        for endpoint, data in (('/api/downloads/resume', None),
                               ('/api/downloads', {'urls': 'npNnbj46_ik'}),
                               ('/api/clash/stop', None)):
            self.assertEqual(self.post(endpoint, json=data).status_code, 409)
        self.assertEqual(self.client.put('/api/config', json={'proxy_mode': 'direct'}, headers=self.headers).status_code, 409)
        self.assertEqual(self.post('/api/youtube/cookie', data={'cookie': (io.BytesIO(COOKIE), 'cookie.txt')}).status_code, 409)
        self.assertEqual(self.post('/api/downloads/import', data={'file': (io.BytesIO(b'npNnbj46_ik'), 'ids.txt')}).status_code, 409)
        self.assertEqual(self.post('/api/youtube/login/cancel').status_code, 200)
        self.manager.thread.join(timeout=8)
        self.assertEqual(self.manager.status()['state'], 'cancelled')
        self.assertFalse((self.root / 'Cookies/youtube.txt').exists())

    def test_cancellation_and_browser_failures_preserve_previous_cookie(self):
        self.assertEqual(self.post('/api/youtube/cookie', data={'cookie': (io.BytesIO(COOKIE), 'cookie.txt')}).status_code, 201)
        target = self.root / 'Cookies/youtube.txt'
        original, config = target.read_bytes(), self.config.read_bytes()
        self.start()
        self.post('/api/youtube/login/cancel')
        self.manager.thread.join(timeout=8)
        self.assertEqual(self.manager.status()['state'], 'cancelled')
        self.fixture = LoginFixture()
        with patch('webapp.youtube_login.login', self.fixture.run):
            self.fixture.failure = BrowserError('http://user:DO_NOT_EXPOSE@127.0.0.1')
            self.start()
            status = self.finish()
        self.assertEqual(status['state'], 'failed')
        self.assertNotIn('DO_NOT_EXPOSE', str(status))
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.config.read_bytes(), config)

    def test_configuration_failure_rolls_back_and_stale_status_can_restart(self):
        self.assertEqual(self.post('/api/youtube/cookie', data={'cookie': (io.BytesIO(COOKIE), 'cookie.txt')}).status_code, 201)
        target = self.root / 'Cookies/youtube.txt'
        original, config, runtime = target.read_bytes(), self.config.read_bytes(), self.store.runtime()
        self.start()
        with patch('app.save_settings', side_effect=PermissionError('fixture')):
            status = self.finish()
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual(self.store.runtime(), runtime)
        self.manager._write({'state': 'waiting', 'message': 'stale'})
        self.assertEqual(self.manager.status()['state'], 'interrupted')

    def test_csrf_and_selected_proxy_are_used_without_exposing_credentials(self):
        self.assertEqual(self.client.post('/api/youtube/login').status_code, 403)
        self.assertEqual(self.client.post('/api/youtube/login/cancel').status_code, 403)
        config = yaml.safe_load(self.config.read_text())
        config.update(proxy_mode='clash', proxy='http://wrong:9999')
        self.config.write_text(yaml.safe_dump(config), encoding='utf-8')
        with patch.object(self.app.extensions['clash'], 'snapshot', return_value={'proxy': 'http://127.0.0.1:18888', 'node': 'Fixture Node'}):
            response = self.start()
        self.assertEqual(self.fixture.config['proxy'], 'http://127.0.0.1:18888')
        self.assertEqual(response['connection'], 'Clash · Fixture Node')
        self.assertEqual(self.finish()['state'], 'succeeded')


class SQLiteFixture(unittest.TestCase):
    setUp = test_web.WebTests.setUp


class SQLiteLoginTests(LoginContract, SQLiteFixture):
    pass


