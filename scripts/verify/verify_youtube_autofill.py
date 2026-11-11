"""Exercise real sign-in field selection in Chromium with synthetic pages only."""
import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.async_api import async_playwright

from scripts.login_youtube import GoogleSignIn
from webapp.browser import browser_session


EMAIL = 'fixture@example.test'
PASSWORD = 'fixture-!&#-password'


def identifier_page(field):
    return '''<html><body>
    <input type="email" hidden><input type="password" name="hiddenPassword" hidden>
    ''' + field + '''
    <div id="identifierNext"><button onclick="nextStep()">Next</button></div>
    <script>
    window.emailSubmissions = window.passwordSubmissions = 0;
    function nextStep() {
      window.submittedEmail = document.querySelector('input:not([hidden])').value;
      window.emailSubmissions++;
      document.body.innerHTML = `<input type="password" name="hiddenPassword" hidden>
        <input type="password" name="Passwd"><div id="passwordNext"><button
        onclick="passwordStep()">Next</button></div>`;
    }
    function passwordStep() {
      window.submittedPassword = document.querySelector('input[name="Passwd"]').value;
      window.passwordSubmissions++;
      document.body.innerHTML = `<input name="totpPin" hidden><input name="totpPin">`;
    }
    </script></body></html>'''


async def main():
    cases = []
    async with async_playwright() as driver:
        async with browser_session(driver, headless=True, proxy_server='') as context:
            # No request leaves the fixture, including those with Google URLs.
            async def route(request):
                await request.fulfill(body=fixture['html'], content_type='text/html')

            fixture = {'html': ''}
            await context.route('**/*', route)
            page = await context.new_page()
            for label, field in (
                ('google_text_identifier', '<input id="identifierId" name="identifier" type="text">'),
                ('identifier_name_fallback', '<input name="identifier" type="text">'),
                ('email_type_fallback', '<input type="email">'),
            ):
                fixture['html'] = identifier_page(field)
                await page.goto('https://accounts.google.com/v3/signin/identifier')
                reports = []
                assistant = GoogleSignIn({'email': EMAIL, 'password': PASSWORD},
                                         lambda state, message: reports.append((state, message)))
                await assistant.advance(page)
                assert await page.evaluate('window.submittedEmail') == EMAIL
                await assistant.advance(page)
                assert await page.evaluate('window.submittedPassword') == PASSWORD
                await assistant.advance(page)
                await assistant.advance(page)
                assert assistant.stopped and assistant.password is None
                assert await page.evaluate('[window.emailSubmissions, window.passwordSubmissions]') == [1, 1]
                assert '二次验证' in reports[-1][1]
                assert PASSWORD not in repr(reports)
                cases.append(label + '_hidden_fields_once_then_challenge')

            for address in ('https://accounts.google.com.example/signin',
                            'http://accounts.google.com/signin', 'https://accounts.google.com:8443/signin'):
                fixture['html'] = identifier_page('<input id="identifierId" type="text">')
                await page.goto(address)
                assistant = GoogleSignIn({'email': EMAIL, 'password': PASSWORD}, lambda *_: None)
                await assistant.advance(page)
                assert await page.locator('#identifierId').input_value() == ''
                assert await page.evaluate('window.emailSubmissions') == 0
            cases.append('untrusted_origin_and_port_do_not_receive_credentials')

            fixture['html'] = '<input name="totpPin" hidden><input name="totpPin"><input name="Passwd" type="password">'
            await page.goto('https://accounts.google.com/v3/signin/challenge/totp')
            assistant = GoogleSignIn({'email': EMAIL, 'password': PASSWORD}, lambda *_: None)
            await assistant.advance(page)
            assert assistant.stopped and assistant.password is None
            assert await page.locator('input[name="Passwd"]').input_value() == ''
            cases.append('visible_challenge_hands_off_before_password')

            for label, field in (
                ('live_email_pin_id', '<input id="idvPinId" type="tel">'),
                ('live_email_pin_name', '<input name="Pin" type="tel">'),
                ('legacy_email_pin', '<input id="idvPin" name="idvPin" type="tel">'),
            ):
                fixture['html'] = ('<input name="idvPin" hidden>' + field
                    + '<div id="idvPreregisteredEmailNext"><button onclick="window.codeSubmissions=(window.codeSubmissions||0)+1">Next</button></div>')
                await page.goto('https://accounts.google.com/v3/signin/challenge/ipe/verify')
                inbox = SimpleNamespace(error=None, poll=AsyncMock(return_value='123456'))
                assistant = GoogleSignIn({'email': EMAIL, 'password': PASSWORD}, lambda *_: None, inbox=inbox)
                await assistant.advance(page)
                await assistant.advance(page)
                assert await page.locator('input:visible').input_value() == '123456', label
                assert await page.evaluate('window.codeSubmissions') == 1, label
                assert inbox.poll.await_count == 1, label
                cases.append(label + '_submits_once')

            await page.goto('https://accounts.google.com/v3/signin/challenge/ipp/verify')
            inbox = SimpleNamespace(error=None, poll=AsyncMock(return_value='123456'))
            assistant = GoogleSignIn({'email': EMAIL, 'password': PASSWORD}, lambda *_: None, inbox=inbox)
            await assistant.advance(page)
            assert await page.locator('input:visible').input_value() == ''
            assert inbox.poll.await_count == 0
            cases.append('same_pin_on_sms_page_never_receives_email_code')

            for mode in ('visible', 'hidden', 'disabled', 'unverified'):
                fixture['html'] = ('<button type="button" '
                    + ('hidden ' if mode == 'hidden' else 'disabled ' if mode == 'disabled' else '')
                    + 'onclick="window.sendCount=(window.sendCount||0)+1">Send</button>')
                await page.goto('https://accounts.google.com/v3/signin/challenge/ipe/consent')
                inbox = SimpleNamespace(error=None, poll=AsyncMock(return_value='123456'))
                reports = []
                assistant = GoogleSignIn({'email': EMAIL, 'password': PASSWORD},
                    lambda stage, message: reports.append(message), inbox=inbox)
                if mode != 'unverified':
                    assistant.choice_sent_at = time.monotonic()
                await assistant.advance(page)
                await assistant.advance(page)
                assert await page.evaluate('window.sendCount||0') == (1 if mode == 'visible' else 0), mode
                inbox.poll.assert_not_awaited()
                if mode in ('hidden', 'disabled'):
                    assistant.consent_started = time.monotonic() - 31
                    await assistant.advance(page)
                    assert assistant.stopped and '持续加载' in reports[-1], mode
                elif mode == 'unverified':
                    assert assistant.stopped
                else:
                    assistant.consent_sent_at = time.monotonic() - 31
                    await assistant.advance(page)
                    assert assistant.stopped and '尚未进入验证码输入页' in reports[-1]
                cases.append('email_consent_' + mode)
    print(json.dumps({'kind': 'youtube_autofill_browser_verified', 'synthetic_pages_only': True,
                      'real_google_credentials_submitted': False, 'cases': cases}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
