"""Cookie library API isolation, selection, secret handling and busy guards."""
import io
import unittest
from unittest.mock import patch

import yaml

from tests import test_web
from webapp.console_auth import LOCAL_RPC_METHODS
from webapp.database import cookie_profiles

COOKIE = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tPRIVATE_COOKIE_VALUE\n'


class CookieProfileTests(unittest.TestCase):
    setUp = test_web.WebTests.setUp

    def add(self, name='Account A', content=COOKIE):
        # Scheduler/selection fixtures now start with an account-owned Cookie.
        # Account creation/upload HTTP contracts live in test_account_credentials.
        from tests.account_cookie_fixture import AccountCookieProfiles
        from types import SimpleNamespace
        import json
        try:
            item = AccountCookieProfiles(self.root,self.store).add(name.strip() or 'cookies.txt',content)
            return SimpleNamespace(status_code=201,json={'item':item},text=json.dumps({'item':item}))
        except ValueError as error:
            return SimpleNamespace(status_code=400,json={'error':str(error)},text=str(error))

    def action(self, action, identity):
        return self.client.post('/api/youtube/cookies/' + action,
                                json={'id': identity}, headers=self.headers)

    def replace_content(self, identity, content):
        with self.store.connect() as db:
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id == identity).values(
                content=content.decode()))

    def test_pool_accepts_one_account_and_keeps_last_member_after_deletion(self):
        first = self.add('A').json['item']['id']
        second = self.add('B', COOKIE.replace(b'PRIVATE_COOKIE_VALUE', b'SECOND_SECRET')).json['item']['id']
        response = self.client.put('/api/config', json={'youtube_cookie_pool': [first]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.client.get('/api/config').json['config']['youtube_cookie_pool'], [first])
        response = self.client.put('/api/config', json={'youtube_cookie_pool': [first, second]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        response = self.action('delete', second)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.client.get('/api/config').json['config']['youtube_cookie_pool'], [first])
        snapshot = self.client.get('/api/youtube/cookies').json
        self.assertEqual(snapshot['pool']['capacity_summary']['available_sessions'], 1)
        self.assertEqual(self.action('delete', first).status_code, 200)
        self.assertEqual(self.client.get('/api/config').json['config']['youtube_cookie_pool'], [])

    def test_cooling_account_is_named_and_login_check_sends_no_network_request(self):
        from webapp.cookie_pool import CookiePool
        from webapp.downloads import YouTubeAccountLimited
        first = self.add('Account A').json['item']['id']
        second = self.add('Account B', COOKIE.replace(b'PRIVATE_COOKIE_VALUE', b'OTHER_FIXTURE')).json['item']['id']
        config = yaml.safe_load(self.config.read_text())
        config.update(youtube_cookie_pool=[first, second])
        self.config.write_text(yaml.safe_dump(config), encoding='utf-8')
        pool = CookiePool(self.store, config, self.root, None)
        pool.outcome(pool.accounts[0][2], YouTubeAccountLimited('limited'))
        with patch('webapp.youtube_auth.probe') as probe:
            result = self.action('check', first)
            probe.assert_not_called()
        self.assertEqual(result.status_code, 409, result.json)
        self.assertIn('Account A', result.json['error'])
        self.assertGreater(result.json['retry_after'], 3500)
        snapshot = self.client.get('/api/youtube/cookies').json['pool']
        self.assertEqual(snapshot['session_states'][0]['name'], 'Account A')
        self.assertEqual(snapshot['session_states'][0]['state'], 'cooldown')
        self.assertEqual(snapshot['capacity_summary']['available_sessions'], 1)
        self.assertNotIn('halted', snapshot)

    def test_manual_login_check_http_429_is_not_an_entire_pool_authentication_halt(self):
        from webapp.youtube_auth import ProbeResult
        first = self.add('Account A').json['item']['id']
        second = self.add('Account B', COOKIE.replace(b'PRIVATE_COOKIE_VALUE', b'OTHER_FIXTURE')).json['item']['id']
        config = yaml.safe_load(self.config.read_text())
        config.update(youtube_cookie_pool=[first, second])
        self.config.write_text(yaml.safe_dump(config), encoding='utf-8')
        with patch('webapp.youtube_auth.probe', return_value=ProbeResult('rate_limited', 'HTTP 429',
                http_status=429, retry_after=720)) as probe:
            result = self.action('check', first)
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json['auth']['http_status'], 429)
            self.assertEqual(self.action('check', first).status_code, 409)
            probe.assert_called_once()
        snapshot = self.client.get('/api/youtube/cookies').json['pool']
        self.assertNotIn('halted', snapshot)
        self.assertEqual(snapshot['capacity_summary']['available_sessions'], 1)
        self.assertEqual(snapshot['session_states'][0]['cooldown_kind'], 'request_rate_limit')


    def test_blank_name_defaults_to_filename(self):
        for name in ('', '   '):
            response = self.add(name)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json['item']['name'], 'cookies.txt')
        response = self.client.post('/api/youtube/cookies', data={
            'cookie': (io.BytesIO(COOKIE), 'account-export.txt')}, headers=self.headers)
        self.assertEqual(response.status_code, 405)

    def test_status_uses_session_capacity_in_custom_mode_without_online_probes(self):
        from webapp.cookie_pool import CookiePool
        from webapp.throughput import MIB, ThroughputController
        ids = [self.add(name).json['item']['id'] for name in ('A', 'B')]
        config = yaml.safe_load(self.config.read_text())
        config.update(download_task_limit=16, youtube_cookie_pool=ids)
        self.config.write_text(yaml.safe_dump(config), encoding='utf-8')
        summary = CookiePool(self.store, config, self.root, None).status()['capacity_summary']
        controller = ThroughputController(self.store.directory)
        sample = dict(cpu_cores=4, memory_total=8192*MIB, memory_available=4096*MIB,
                      cpu_percent=20, io_pressure=0, rx=0)
        with patch.object(controller, 'sample', return_value=sample), patch('webapp.youtube_auth.probe') as probe:
            controller.update(config, cookie_capacity=summary)
            result = self.client.get('/api/status')
            probe.assert_not_called()
        self.assertEqual(result.status_code, 200)
        capacity = result.json['concurrency']
        self.assertEqual(capacity['requested'], 1)
        self.assertEqual(capacity['mode'], 'cookie_pool')
        self.assertEqual(capacity['cookie_pool']['capacity'], 1)
        self.assertEqual(capacity['effective_limit'], 1)
        self.assertEqual(capacity['cookie_pool']['configured_files'], 2)
        self.assertEqual(capacity['cookie_pool']['effective_sessions'], 1)
        self.assertNotIn('PRIVATE_COOKIE_VALUE', result.text)

    def test_session_limit_setting_saves_and_pool_list_reports_effective_limit(self):
        identity = self.add().json['item']['id']
        response = self.client.put('/api/config', json={'youtube_cookie_pool': [identity],
            'youtube_cookie_session_limit': 2, 'download_task_limit': 6}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.client.get('/api/config').json['config']['youtube_cookie_session_limit'], 2)
        pool = self.client.get('/api/youtube/cookies').json['pool']
        self.assertEqual(pool['session_limit'], 2)
        self.assertEqual(pool['execution_limit'], 64)
        self.assertEqual(pool['accounts'][0]['configured_limit'], 2)
        self.assertEqual(pool['accounts'][0]['limit'], 1)


    def test_profiles_isolated_by_registered_instance(self):
        from types import SimpleNamespace
        from tests.account_cookie_fixture import AccountCookieProfiles as CookieProfiles
        self.store.fleet = SimpleNamespace(id='instance-a')
        first = CookieProfiles(self.root, self.store)
        identity = first.add('A', COOKIE)['id']
        # No file is needed to retain a newly saved account.
        self.assertFalse(first.path(identity).exists())
        self.store.fleet = SimpleNamespace(id='instance-b')
        second = CookieProfiles(self.root, self.store)
        self.assertEqual(second.items(), [])
        with self.assertRaises(ValueError):
            second.require(identity)
        self.store.fleet = SimpleNamespace(id='instance-a')
        self.assertEqual(CookieProfiles(self.root, self.store).items()[0]['id'], identity)

    def test_old_files_are_not_imported_without_an_account(self):
        import json
        from webapp.cookie_profiles import CookieProfiles
        identity='a'*32
        directory=self.root/'Cookies/profiles'
        directory.mkdir(parents=True)
        target=directory/(identity+'.txt')
        target.write_bytes(COOKIE)
        target.with_suffix('.json').write_text(json.dumps({'name':'Historical','created_at':'2026-10-31'}))
        self.assertEqual(CookieProfiles(self.root,self.store).items(),[])
        with self.assertRaises(ValueError): CookieProfiles(self.root,self.store).require(identity)



















    def test_csrf_and_instance_rpc_allowlist(self):
        self.assertEqual(self.client.post('/api/youtube/cookies/delete', json={'id': 'dddddddddddddddddddddddddddddddd'}).status_code, 403)
        self.assertEqual(self.client.post('/api/youtube/cookies/check', json={'id': 'dddddddddddddddddddddddddddddddd'}).status_code, 403)
        self.assertNotIn('POST', LOCAL_RPC_METHODS['/api/youtube/cookies'])
        for path in ('/api/youtube/cookies/delete', '/api/youtube/cookies/check'):
            self.assertIn('POST', LOCAL_RPC_METHODS[path])



    def test_check_without_auth_credentials_makes_no_network_request(self):
        identity = self.add(content=COOKIE.replace(b'\tSID\t', b'\tPREF\t')).json['item']['id']
        with patch('webapp.youtube_auth.probe') as probe:
            response = self.action('check', identity)
        self.assertEqual(response.json['auth']['state'], 'not_logged_in')
        probe.assert_not_called()


    def test_manual_account_check_resumes_only_that_account_without_pausing_downloads(self):
        from webapp.cookie_pool import CookiePool
        from webapp.youtube_auth import YouTubeAuthRequired
        identity = self.add().json['item']['id']
        config = yaml.safe_load(self.config.read_text())
        config['youtube_cookie_pool'] = [identity]
        self.config.write_text(yaml.safe_dump(config))
        self.store.set_downloads_paused(False)
        # Establish a cached success before the download reports a new login failure.
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'old login')):
            self.assertEqual(self.action('check', identity).status_code, 200)
        pool = CookiePool(self.store, config, self.root, None)
        pool.outcome(pool.accounts[0][2], YouTubeAuthRequired({'state': 'not_logged_in', 'message': 'Login rejected'}))
        listed = self.client.get('/api/youtube/cookies').json['pool']
        self.assertEqual(listed['session_states'][0]['state'], 'manual')
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'fresh login')) as probe:
            response = self.action('check', identity)
            probe.assert_called_once()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json['auth']['account_resumed'])
        self.assertEqual(pool.status()['session_states'][0]['state'], 'ready')
        self.assertFalse(self.store.downloads_paused())


if __name__ == '__main__':
    unittest.main()
