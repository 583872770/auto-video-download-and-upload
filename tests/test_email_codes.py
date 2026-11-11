"""Provider isolation, fresh-code selection, pacing and sign-in handoff contracts."""
import asyncio
from datetime import datetime, timezone
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import Error as BrowserError

from webapp.email_codes import (EmailInbox, InboxUnavailable, MAX_PAGE_BYTES, ORIGIN,
                               email_inbox, read_inbox, supported_source)
from scripts.login_youtube import GoogleSignIn


EMAIL = 'fixture@example.test'


def mail(code='123456', *, sender='Google', stamp=None, explicit=True):
    date = '<time datetime="' + stamp + '"></time>' if stamp else ''
    content = '<span class="verify-code">' + code + '</span>' if explicit else code
    return '<div class="mail-card"><div class="mail-meta">' + sender + '</div>' + date + content + '</div>'


def inbox(*messages, email=EMAIL):
    return ('<h1>' + email + '</h1><a href="/inbox?sid=fixture-private-session&amp;refresh=1">刷新</a>'
            + (''.join(messages) or '<div class="verify-empty">暂无验证码</div>'))


class InboxParsingTests(unittest.TestCase):
    def test_supported_provider_upgrades_http_and_rejects_other_hosts(self):
        for value in ('http://fbvjdjcf.asia/', 'https://fbvjdjcf.asia/', 'https://fbvjdjcf.asia/inbox?sid=ignored'):
            self.assertTrue(supported_source(value))
        for value in ('http://127.0.0.1/', 'https://fbvjdjcf.asia.evil/', 'https://evil@fbvjdjcf.asia/',
                      'https://fbvjdjcf.asia:8443/', 'https://fbvjdjcf.asia/admin', None):
            self.assertFalse(supported_source(value))

    def test_cloudflare_protected_heading_and_google_sender_are_decoded(self):
        raw = bytes([41]) + bytes(c ^ 41 for c in EMAIL.encode())
        body = inbox(mail()).replace(EMAIL, '<a data-cfemail="' + raw.hex() + '">[email protected]</a>')
        known, fresh, refresh = read_inbox(body, EMAIL, time.time())
        self.assertEqual(known, {'123456'})
        self.assertEqual(fresh, {'123456'})
        self.assertTrue(refresh.startswith(ORIGIN + '/inbox?'))

    def test_only_google_labelled_or_explicit_codes_are_candidates(self):
        body = inbox(mail('123456', sender='Google'), mail('654321', sender='Unrelated service'),
                     mail('Order 999999', explicit=False),
                     mail('765432 is your Google verification code', explicit=False),
                     mail('验证码：876543', sender='谷歌', explicit=False))
        known, fresh, _ = read_inbox(body, EMAIL, time.time())
        self.assertEqual(fresh, {'123456', '765432', '876543'})
        self.assertIn('654321', known)
        self.assertNotIn('999999', known)

    def test_wrong_mailbox_unknown_layout_and_unsafe_refresh_fail_closed(self):
        for body in (inbox(mail(), email='other@example.test'),
                     inbox(mail()).replace('mail-card', 'unknown-layout'),
                     inbox(mail()).replace('/inbox?sid=', 'https://evil.example/inbox?sid=')):
            with self.assertRaises(InboxUnavailable):
                read_inbox(body, EMAIL, time.time())

    def test_expired_and_future_messages_are_not_candidates(self):
        now = time.time()
        old = datetime.fromtimestamp(now - 600, timezone.utc).isoformat()
        future = datetime.fromtimestamp(now + 600, timezone.utc).isoformat()
        known, fresh, _ = read_inbox(inbox(mail(stamp=old), mail('654321', stamp=future)), EMAIL, now)
        self.assertEqual(known, {'123456', '654321'})
        self.assertEqual(fresh, set())

    def test_large_or_deep_page_is_bounded(self):
        for body in ('x' * (MAX_PAGE_BYTES + 1), '<div>' * 100 + inbox()):
            with self.assertRaises(InboxUnavailable):
                read_inbox(body, EMAIL, time.time())


def response(body='', *, status=200, headers=None):
    result = MagicMock()
    result.status, result.headers = status, headers or {}
    result.body = AsyncMock(return_value=body.encode())
    result.dispose = AsyncMock()
    return result


class InboxNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_sends_only_email_and_redirect_response_is_disposed(self):
        request = MagicMock()
        redirect = response(status=303, headers={'location': '/inbox?sid=fixture-private-session'})
        request.post, request.get = AsyncMock(return_value=redirect), AsyncMock(return_value=response(inbox()))
        client = EmailInbox(request, EMAIL)
        await client.fetch(ORIGIN + '/login?lang=zh', form={'email': EMAIL})
        request.post.assert_awaited_once_with(ORIGIN + '/login?lang=zh', form={'email': EMAIL}, max_redirects=0)
        redirect.dispose.assert_awaited_once()

    async def test_foreign_redirect_never_makes_a_second_request(self):
        request = MagicMock()
        redirect = response(status=302, headers={'location': 'http://127.0.0.1/private'})
        request.get = AsyncMock(return_value=redirect)
        with self.assertRaises(InboxUnavailable):
            await EmailInbox(request, EMAIL).fetch(ORIGIN + '/inbox')
        self.assertEqual(request.get.await_count, 1)
        redirect.dispose.assert_awaited_once()

    async def test_old_code_baseline_cadence_and_new_code_once(self):
        client = EmailInbox(None, EMAIL)
        client.fetch = AsyncMock(side_effect=[inbox(mail()), inbox(mail()), inbox(mail(), mail('654321')),
                                              inbox(mail(), mail('654321'))])
        with patch('webapp.email_codes.time.monotonic', return_value=100):
            await client.open()
            self.assertIsNone(await client.poll())
        self.assertEqual(client.fetch.await_count, 2)
        with patch('webapp.email_codes.time.monotonic', return_value=130):
            self.assertEqual(await client.poll(), '654321')
            self.assertIsNone(await client.poll())
        with patch('webapp.email_codes.time.monotonic', return_value=160):
            self.assertIsNone(await client.poll())
        self.assertEqual(client.fetch.await_count, 4)

    async def test_multiple_new_codes_fail_instead_of_guessing(self):
        client = EmailInbox(None, EMAIL)
        client.refresh, client.last_poll = ORIGIN + '/inbox', 0
        client.fetch = AsyncMock(return_value=inbox(mail(), mail('654321')))
        with self.assertRaises(InboxUnavailable):
            await client.poll()

    async def test_manual_or_unsupported_source_opens_no_mailbox(self):
        driver = MagicMock()
        for credentials in (None, {'email': EMAIL, 'code_url': 'https://example.test/'}):
            async with email_inbox(driver, credentials, {}, MagicMock()) as client:
                self.assertIsNone(client)
        driver.request.new_context.assert_not_called()

    async def test_cancellation_disposes_request_context(self):
        driver, request = MagicMock(), MagicMock()
        request.dispose = AsyncMock()
        driver.request.new_context = AsyncMock(return_value=request)
        with patch('webapp.email_codes.EmailInbox.open', AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                async with email_inbox(driver, {'email': EMAIL, 'code_url': ORIGIN}, {}, MagicMock()):
                    raise asyncio.CancelledError()
        request.dispose.assert_awaited_once()


class EmailChallengeTests(unittest.IsolatedAsyncioTestCase):
    def page(self, path='/v3/signin/challenge/ipe'):
        page, elements = MagicMock(), {}
        page.url = 'https://accounts.google.com' + path
        def locator(selector):
            if selector not in elements:
                item = MagicMock()
                item.first = item
                item.is_visible = AsyncMock(return_value=False)
                item.input_value = AsyncMock(return_value='')
                item.get_attribute = AsyncMock(return_value=None)
                item.fill, item.click, item.press = AsyncMock(), AsyncMock(), AsyncMock()
                elements[selector] = item
            return elements[selector]
        page.locator.side_effect = locator
        locator(GoogleSignIn.EMAIL_PIN).is_visible.return_value = True
        locator(GoogleSignIn.EMAIL_NEXT).is_visible.return_value = True
        return page, locator

    def assistant(self, code='123456'):
        provider = MagicMock()
        provider.error = None
        provider.poll = AsyncMock(return_value=code)
        report = MagicMock()
        return GoogleSignIn({'email': EMAIL, 'password': 'fixture-password'}, report, inbox=provider), report

    async def test_email_challenge_submits_once_without_logging_code(self):
        page, locator = self.page()
        assistant, report = self.assistant()
        await assistant.advance(page)
        await assistant.advance(page)
        locator(GoogleSignIn.EMAIL_PIN).fill.assert_awaited_once_with('123456', timeout=3000)
        locator(GoogleSignIn.EMAIL_NEXT).click.assert_awaited_once()
        self.assertEqual(assistant.inbox.poll.await_count, 1)
        self.assertNotIn('123456', str(report.call_args_list))
        self.assertIsNone(assistant.password)

    async def test_real_ipe_verify_path_receives_mail_but_other_challenges_do_not(self):
        for path in ('/v3/signin/challenge/ipe/verify', '/signin/challenge/ipe/verify/'):
            page, locator = self.page(path)
            assistant, _ = self.assistant()
            await assistant.advance(page)
            locator(GoogleSignIn.EMAIL_PIN).fill.assert_awaited_once_with('123456', timeout=3000)
        for path in ('/v3/signin/challenge/ipp/verify', '/v3/signin/challenge/ipe/other'):
            page, locator = self.page(path)
            assistant, _ = self.assistant()
            locator(GoogleSignIn.CHALLENGE).is_visible.return_value = True
            await assistant.advance(page)
            assistant.inbox.poll.assert_not_awaited()

    async def test_selection_clicks_matching_mail_once_even_with_duplicate_options(self):
        page, locator = self.page('/v3/signin/challenge/selection')
        assistant, report = self.assistant()
        choices = page.get_by_text.return_value
        choices.count = AsyncMock(return_value=2)
        option = choices.nth.return_value
        option.is_visible = AsyncMock(return_value=True)
        option.click = AsyncMock()
        await assistant.advance(page)
        await assistant.advance(page)
        option.click.assert_awaited_once_with(timeout=3000)
        expression = page.get_by_text.call_args.args[0]
        self.assertIsNotNone(expression.fullmatch('Get a verification code at ' + EMAIL))
        for text in ('Get a verification code at other@example.test',
                     'Get a verification code at +123456789',
                     'Get a verification code at fix•••@example.test',
                     'Use another phone or computer to finish signing in'):
            self.assertIsNone(expression.fullmatch(text))
        assistant.inbox.poll.assert_not_awaited()
        self.assertNotIn('123456', str(report.call_args_list))

    async def test_selection_failure_hands_off_without_clicking_other_methods(self):
        page, _ = self.page('/v3/signin/challenge/selection')
        assistant, _ = self.assistant()
        assistant.choice_started = time.monotonic() - 11
        page.get_by_text.return_value.count = AsyncMock(return_value=0)
        await assistant.advance(page)
        self.assertTrue(assistant.stopped)
        assistant.inbox.poll.assert_not_awaited()
        page.get_by_text.return_value.nth.assert_not_called()

    async def test_selection_never_requests_code_without_a_working_inbox(self):
        page, _ = self.page('/v3/signin/challenge/selection')
        assistant, _ = self.assistant()
        assistant.inbox.error = True
        await assistant.advance(page)
        self.assertTrue(assistant.stopped)
        page.get_by_text.assert_not_called()

    async def test_sms_totp_and_captcha_never_poll_mail(self):
        for path, selector in (('/v3/signin/challenge/ipp', GoogleSignIn.CHALLENGE),
                               ('/v3/signin/challenge/totp', GoogleSignIn.CHALLENGE),
                               ('/v3/signin/challenge/ipe', GoogleSignIn.HUMAN_CHECK)):
            page, locator = self.page(path)
            assistant, _ = self.assistant()
            locator(selector).is_visible.return_value = True
            locator(GoogleSignIn.CHALLENGE).is_visible.return_value = True
            await assistant.advance(page)
            assistant.inbox.poll.assert_not_awaited()
            self.assertTrue(assistant.stopped)

    async def test_navigation_during_poll_never_receives_code_on_other_origin(self):
        page, locator = self.page()
        assistant, _ = self.assistant()
        async def moved():
            page.url = 'https://other.example/'
            return '123456'
        assistant.inbox.poll.side_effect = moved
        await assistant.advance(page)
        locator(GoogleSignIn.EMAIL_PIN).fill.assert_not_awaited()

    async def test_user_entered_code_is_not_overwritten_or_submitted(self):
        page, locator = self.page()
        assistant, _ = self.assistant()
        locator(GoogleSignIn.EMAIL_PIN).input_value.return_value = '777777'
        await assistant.advance(page)
        assistant.inbox.poll.assert_not_awaited()
        locator(GoogleSignIn.EMAIL_PIN).fill.assert_not_awaited()
        self.assertTrue(assistant.stopped)

    async def test_timeout_network_failure_and_invalid_code_hand_off(self):
        for mode in ('timeout', 'network', 'invalid'):
            page, locator = self.page()
            assistant, report = self.assistant()
            if mode == 'timeout':
                assistant.code_started = time.monotonic() - 181
            elif mode == 'network':
                assistant.inbox.poll.side_effect = BrowserError('private-url-and-session')
            else:
                assistant.code_sent = True
                locator(GoogleSignIn.EMAIL_PIN).get_attribute.return_value = 'true'
            await assistant.advance(page)
            self.assertTrue(assistant.stopped)
            self.assertIsNone(assistant.password)
            self.assertNotIn('private-url-and-session', str(report.call_args_list))
            locator(GoogleSignIn.EMAIL_PIN).fill.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
