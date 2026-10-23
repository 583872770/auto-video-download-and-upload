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
