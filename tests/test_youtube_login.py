from http.cookiejar import MozillaCookieJar
from pathlib import Path
import tempfile
import unittest

from Utils.youtube_login import save_youtube_cookies
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio


def cookie(domain, name='session', expires=-1):
    return {'domain': domain, 'name': name, 'value': 'test-value', 'path': '/',
            'secure': True, 'httpOnly': True, 'expires': expires}


class YoutubeCookieTests(unittest.TestCase):
    def test_netscape_roundtrip_preserves_session_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'youtube.txt'
            save_youtube_cookies([cookie('.youtube.com'), cookie('www.youtube.com', 'persistent', 2000000000)], target)
            jar = MozillaCookieJar(str(target))
            jar.load(ignore_discard=True, ignore_expires=True)
            values = {entry.name: entry for entry in jar}
            self.assertEqual(values['session'].value, 'test-value')
            self.assertIsNone(values['session'].expires)
            self.assertTrue(values['session'].secure)
            self.assertEqual(values['persistent'].expires, 2000000000)
            self.assertEqual(list(Path(directory).glob('.youtube-*')), [])

    def test_google_and_lookalike_domains_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'youtube.txt'
            save_youtube_cookies([cookie('.youtube.com'), cookie('.google.com'),
                                  cookie('youtube.com.example'), cookie('notyoutube.com')], target)
            jar = MozillaCookieJar(str(target))
            jar.load(ignore_discard=True, ignore_expires=True)
            self.assertEqual([entry.domain for entry in jar], ['.youtube.com'])

    def test_missing_youtube_session_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'youtube.txt'
            target.write_text('previous session', encoding='utf-8')
            with self.assertRaises(ValueError):
                save_youtube_cookies([cookie('.google.com')], target)
            self.assertEqual(target.read_text(encoding='utf-8'), 'previous session')


class YoutubeBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_label_comes_only_from_active_account_header(self):
        from scripts.login_youtube import current_account_name
        from playwright.async_api import TimeoutError as BrowserTimeoutError
        page, avatar, header = MagicMock(), MagicMock(), MagicMock()
        avatar.first, header.first = avatar, header
        avatar.click, header.wait_for = AsyncMock(), AsyncMock()
        page.locator.side_effect = lambda selector: {'#avatar-btn': avatar,
            'ytd-active-account-header-renderer': header}[selector]
        for labels, expected in [({'#email': ' alice@example.test ', '#account-name': 'Alice'}, 'alice@example.test'),
                                 ({'#email': '', '#account-name': '频道名称'}, '频道名称'),
                                 ({'#channel-handle': '@alice'}, '@alice'), ({}, None)]:
            def field(selector):
                element = MagicMock()
                element.first = element
                element.inner_text = AsyncMock(return_value=labels.get(selector, ''))
                if selector not in labels:
                    element.inner_text.side_effect = BrowserTimeoutError('Missing optional field')
                return element
            header.locator.side_effect = field
            self.assertEqual(await current_account_name(page), expected)
        header.wait_for.side_effect = BrowserTimeoutError('Account menu changed')
        self.assertIsNone(await current_account_name(page))

    async def test_login_returns_account_label_before_cookie_export_and_can_fall_back(self):
        from scripts.login_youtube import login
        for account_result in ('alice@example.test', asyncio.TimeoutError()):
            page, avatar, refusal = AsyncMock(), MagicMock(), MagicMock()
            avatar.first, refusal.first = avatar, refusal
            avatar.wait_for = AsyncMock()
            refusal.is_visible = AsyncMock(return_value=False)
            page.locator = MagicMock(return_value=avatar)
            page.get_by_text = MagicMock(return_value=refusal)
            context = AsyncMock()
            context.new_page.return_value = page
            cookies = [cookie('.youtube.com')]
            context.cookies.return_value = cookies
            session = AsyncMock()
            session.__aenter__.return_value = context
            order = []
            async def navigate(url, **kwargs):
                order.append(url)
            page.goto.side_effect = navigate
            async def read_account(current_page):
                self.assertIs(current_page, page)
                order.append('account')
                if isinstance(account_result, Exception):
                    raise account_result
                return account_result
            with patch('scripts.login_youtube.async_playwright') as driver, \
                    patch('scripts.login_youtube.browser_session', return_value=session), \
                    patch('scripts.login_youtube.current_account_name', read_account), \
                    patch('scripts.login_youtube.save_youtube_cookies') as save:
                driver.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
                driver.return_value.__aexit__ = AsyncMock(return_value=False)
                result = await login({}, Path('unused.txt'))
            self.assertEqual(result['account_name'], account_result if isinstance(account_result, str) else None)
            self.assertEqual(order, ['https://www.youtube.com/', 'account', 'https://www.youtube.com/robots.txt'])
            save.assert_called_once_with(cookies, Path('unused.txt'))
            session.__aexit__.assert_awaited_once()

    async def test_timeout_waits_for_browser_cleanup_without_export(self):
        from scripts.login_youtube import login
        closed = []

        async def pending(*args):
            try:
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        with patch('scripts.login_youtube._login', pending):
            with self.assertRaisesRegex(RuntimeError, '未确认 YouTube 登录'):
                await login({}, Path('unused.txt'), timeout_seconds=0.02)
        self.assertEqual(closed, [True])

    async def test_google_rejection_stops_without_export_or_security_changes(self):
        from scripts.login_youtube import login
        page = AsyncMock()
        refusal = MagicMock()
        refusal.first = refusal
        refusal.is_visible = AsyncMock(return_value=True)
        page.get_by_text = MagicMock(return_value=refusal)
        context = AsyncMock()
        context.new_page.return_value = page
        session = AsyncMock()
        session.__aenter__.return_value = context
        with patch('scripts.login_youtube.async_playwright') as driver:
            driver.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            driver.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch('scripts.login_youtube.browser_session', return_value=session):
                with patch('scripts.login_youtube.save_youtube_cookies') as save:
                    with self.assertRaisesRegex(RuntimeError, 'Google 拒绝'):
                        await asyncio.wait_for(login({'proxy': ''}, Path('unused.txt')), timeout=1)
                    context.cookies.assert_not_awaited()
                    save.assert_not_called()
                    session.__aexit__.assert_awaited_once()
