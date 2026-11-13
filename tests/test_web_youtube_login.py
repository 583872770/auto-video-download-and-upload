"""Login controls, cookie replacement and queued downloads on SQLite/MySQL."""
import unittest
from unittest.mock import patch

from playwright.async_api import Error as BrowserError

from tests import test_web
from webapp.youtube_login import browser_cookie_name, browser_failure_message

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


class LegacyLoginEndpointTests(unittest.TestCase):
    setUp = test_web.WebTests.setUp

    def test_anonymous_browser_login_is_rejected_before_starting_a_window(self):
        for route in ('/api/youtube/login','/api/youtube/account-login'):
            response=self.client.post(route,headers=self.headers,json={})
            self.assertEqual(response.status_code,400,response.json)
        self.assertFalse(self.app.extensions['youtube_login'].status()['active'])

    def test_cancel_requires_account_context(self):
        for route in ('/api/youtube/login/cancel','/api/youtube/account-login/cancel'):
            response=self.client.post(route,headers=self.headers,json={})
            self.assertEqual(response.status_code,400,response.json)

    def test_legacy_status_remains_readable_and_does_not_expose_credentials(self):
        response=self.client.get('/api/youtube/login')
        self.assertEqual(response.status_code,200,response.json)
        self.assertFalse(response.json['active'])
        self.assertNotIn('fixture-only-secret',response.text)


if __name__=='__main__': unittest.main()
