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
from webapp.youtube_login import YouTubeLogin, LoginBusy, browser_cookie_name, browser_failure_message

COOKIE = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-only-secret\n'


class BrowserFailureTests(unittest.TestCase):
    def test_browser_profile_name_keeps_account_and_time_within_limit(self):
        with patch('webapp.youtube_login.time.strftime', return_value='2026-11-02 18:00:00'):
            self.assertEqual(browser_cookie_name(' alice@example.test '),
                             'alice@example.test · 2026-11-02 18:00:00')
            self.assertEqual(browser_cookie_name('频道名称'), '频道名称 · 2026-11-02 18:00:00')
            for missing in (None, '', '  ', '\x00', {'unexpected': 'value'}):
                self.assertEqual(browser_cookie_name(missing), '浏览器登录 2026-11-02 18:00:00')
            name = browser_cookie_name('\x00' + '长名称' * 80 + '\n')
            self.assertEqual(len(name), 80)
            self.assertTrue(name.endswith(' · 2026-11-02 18:00:00'))
            self.assertNotIn('\x00', name)
            self.assertNotIn('\n', name)

    def test_failures_identify_environment_network_and_closed_windows_safely(self):
        cases = [
            ("BrowserType.launch: Executable doesn't exist at /private/path", 'starting', '未找到'),
            ('Missing X server or $DISPLAY', 'starting', '显示环境'),
            ('error while loading shared libraries: libexample.so', 'starting', '系统依赖'),
            ('Page.goto: net::ERR_PROXY_CONNECTION_FAILED', 'starting', 'ERR_PROXY_CONNECTION_FAILED'),
            ('Page.goto: Timeout 60000ms exceeded', 'starting', '超时'),
            ('BrowserType.launch: process exited', 'starting', '启动'),
            ('Target page, context or browser has been closed', 'waiting', '已关闭'),
        ]
        for detail, stage, expected in cases:
            with self.subTest(detail=detail):
                error = BrowserError(detail + '\nhttp://user:DO_NOT_EXPOSE@proxy.test --password=SECRET')
                message = browser_failure_message(error, stage)
                self.assertIn(expected, message)
                for private in ('DO_NOT_EXPOSE', 'SECRET', 'proxy.test', '/private/path'):
                    self.assertNotIn(private, message)


class LoginFixture:
    def __init__(self):
        self.ready = threading.Event()
        self.complete = threading.Event()
        self.config = None
        self.failure = None
        self.account_name = None

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
        return {'account_name': self.account_name}


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
        self.store.set_downloads_paused(True)
        self.start()
        status = self.finish()
        self.assertEqual(status['state'], 'succeeded')
        self.assertIn('Fixture login waiting', [event['message'] for event in status['events']])
        self.assertEqual(status['events'][-1]['state'], 'succeeded')
        cookie_path = yaml.safe_load(self.config.read_text())['youtube_cookie']
        target = self.root / cookie_path
        self.assertTrue(target.is_file())
        self.assertTrue(cookie_path.startswith('Cookies/profiles/'))
        items = self.client.get('/api/youtube/cookies').json['items']
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]['active'])
        self.assertTrue(items[0]['name'].startswith('浏览器登录 '))
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

    def test_login_during_download_saves_new_profile_without_replacing_running_credentials(self):
        self.fixture.account_name = 'busy-account@example.test'
        original_cookie = COOKIE.replace(b'fixture-only-secret', b'original-fixture-secret')
        self.assertEqual(self.post('/api/youtube/cookie', data={
            'cookie': (io.BytesIO(original_cookie), 'cookie.txt')}).status_code, 201)
        queued = self.store.create('download', {'base': str(self.root),
            'urls': ['https://example.test/current.mp4']})
        task = self.store.claim(self.store.owner)
        self.assertEqual(task['id'], queued['id'])
        target = self.root / 'Cookies/youtube.txt'
        original, config, runtime = target.read_bytes(), self.config.read_bytes(), self.store.runtime()
        from webapp.downloader import download_cookie_session
        with download_cookie_session({'cookiefile': str(target)}) as options:
            self.start()
            self.assertEqual(self.finish()['state'], 'succeeded')
            self.assertEqual(Path(options['cookiefile']).read_bytes(), original)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.config.read_bytes(), config)
        self.assertEqual(self.store.runtime(), runtime)
        self.assertFalse(self.store.downloads_paused())
        self.assertTrue(self.store.heartbeat(task['id'], self.store.owner))
        self.assertEqual(self.store.get(task['id'])['status'], 'running')
        profiles = self.client.get('/api/youtube/cookies').json['items']
        added = [item for item in profiles if item['id'] != 'legacy']
        self.assertEqual(len(added), 1)
        self.assertFalse(added[0]['active'])
        self.assertTrue(added[0]['name'].startswith('busy-account@example.test · '))
        from webapp.cookie_profiles import CookieProfiles
        self.assertIn(b'fixture-only-secret', CookieProfiles(self.root, self.store).require(added[0]['id']).read_bytes())
        self.assertIn('已新增 Cookie', self.manager.status()['message'])
        self.assertNotIn('fixture-only-secret', str(profiles))

    def test_downloads_can_be_created_and_resumed_while_browser_login_is_pending(self):
        self.store.set_downloads_paused(True)
        self.start()
        response = self.post('/api/downloads', json={'urls': 'https://example.test/parallel.mp4'})
        self.assertEqual(response.status_code, 202, response.json)
        response = self.post('/api/downloads/resume')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNotNone(self.store.claim(self.store.owner))
        self.assertTrue(self.manager.status()['active'])
        self.assertEqual(self.finish()['state'], 'succeeded')
        self.assertIn('已新增 Cookie', self.manager.status()['message'])

    def test_repeated_idle_logins_add_profiles_and_preserve_previous_credentials(self):
        from webapp.cookie_profiles import CookieProfiles
        self.assertEqual(self.post('/api/youtube/cookie', data={
            'cookie': (io.BytesIO(COOKIE), 'cookie.txt')}).status_code, 201)
        original = (self.root / 'Cookies/youtube.txt').read_bytes()
        identities = {'legacy'}
        for value in (b'first-new-session', b'second-new-session'):
            async def completed(config, output, *, on_status, cancelled):
                output.write_bytes(COOKIE.replace(b'fixture-only-secret', value))
            with patch('webapp.youtube_login.login', completed):
                self.assertEqual(self.post().status_code, 202)
                self.assertEqual(self.finish()['state'], 'succeeded')
            items = self.client.get('/api/youtube/cookies').json['items']
            current = {item['id'] for item in items}
            added = current - identities
            self.assertEqual(len(added), 1)
            new_id = added.pop()
            self.assertTrue(next(item for item in items if item['id'] == new_id)['active'])
            self.assertIn(value, CookieProfiles(self.root, self.store).require(new_id).read_bytes())
            self.assertEqual((self.root / 'Cookies/youtube.txt').read_bytes(), original)
            identities = current
        self.assertEqual(len(identities), 3)

    def test_same_browser_account_adds_separate_named_profiles(self):
        self.fixture.account_name = 'alice@example.test'
        self.start()
        self.assertEqual(self.finish()['state'], 'succeeded')
        first = self.client.get('/api/youtube/cookies').json['items']
        self.fixture = LoginFixture()
        self.fixture.account_name = 'alice@example.test'
        with patch('webapp.youtube_login.login', self.fixture.run):
            self.start()
            self.assertEqual(self.finish()['state'], 'succeeded')
        items = self.client.get('/api/youtube/cookies').json['items']
        self.assertEqual(len(items), 2)
        self.assertEqual(len({item['id'] for item in items}), 2)
        self.assertTrue(all(item['name'].startswith('alice@example.test · ') for item in items))
        self.assertEqual(next(item['name'] for item in items if item['id'] == first[0]['id']), first[0]['name'])

    def test_duplicate_login_and_conflicting_mutations_are_blocked(self):
        self.start()
        other = YouTubeLogin(self.store.directory, lambda data: None)
        self.assertTrue(other.status()['active'])
        with self.assertRaises(LoginBusy):
            other.start({}, '直连')
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(self.post('/api/clash/stop').status_code, 409)
        self.assertEqual(self.client.put('/api/config', json={'proxy_mode': 'direct'}, headers=self.headers).status_code, 409)
        self.assertEqual(self.post('/api/youtube/cookie', data={'cookie': (io.BytesIO(COOKIE), 'cookie.txt')}).status_code, 409)
        self.assertEqual(self.post('/api/downloads/import', data={'file': (io.BytesIO(b'npNnbj46_ik'), 'ids.txt')}).status_code, 202)
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


