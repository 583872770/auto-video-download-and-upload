"""Real Chromium + local HTTP inbox fixture; never sign in to a real account."""
import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.login_youtube import login, LoginCancelled
from webapp.browser import browser_session
from webapp.email_codes import ORIGIN, email_inbox


EMAIL = 'fixture@example.test'
PASSWORD = 'fixture-private-password'
OLD_CODE, NEW_CODE = '111111', '222222'


async def main():
    state = {'issued': False, 'deliver': True, 'posts': 0, 'disposed': 0, 'browsers_closed': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
            assert form == {'email': [EMAIL]}, 'Only the mailbox email may be submitted'
            state['posts'] += 1
            self.send_response(303)
            self.send_header('Location', ORIGIN + '/inbox?sid=fixture-private-session')
            self.end_headers()

        def do_GET(self):
            code = '<div class="verify-row"><span>Google 验证码</span><b class="verify-code">{}</b></div>'
            body = ('<h1>' + EMAIL + '</h1><a href="/inbox?sid=fixture-private-session&amp;refresh=1">刷新</a>'
                    + code.format(OLD_CODE))
            if state['issued'] and state['deliver']:
                body += code.format(NEW_CODE)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(body.encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class LocalRequest:
        """Only substitute transport; production redirect and HTML checks remain."""
        def __init__(self, request):
            self.request = request

        def local(self, url):
            assert url.startswith(ORIGIN + '/')
            return url.replace(ORIGIN, f'http://127.0.0.1:{server.server_port}', 1)

        async def post(self, url, **options):
            return await self.request.post(self.local(url), **options)

        async def get(self, url, **options):
            return await self.request.get(self.local(url), **options)

        async def dispose(self):
            await self.request.dispose()
            state['disposed'] += 1

    @asynccontextmanager
    async def fixture_inbox(driver, credentials, config, report):
        async def new_context(**options):
            return LocalRequest(await driver.request.new_context(**options))
        facade = SimpleNamespace(request=SimpleNamespace(new_context=new_context))
        async with email_inbox(facade, credentials, config, report) as inbox:
            yield inbox

    @asynccontextmanager
    async def fixture_browser(driver, **options):
        assert options['headless'] is False
        async with browser_session(driver, headless=True, proxy_server='') as context:
            async def route(request):
                address = urlsplit(request.request.url)
                if address.hostname == 'accounts.google.com':
                    if address.path.endswith('/challenge/ipe/verify'):
                        state['issued'] = True
                        body = ('<input name="idvPin" hidden><input id="idvPinId" name="Pin" type="tel">'
                                '<div id="idvpreregisteredemailNext"><div><button type="button" '
                                'onclick="if(document.getElementById(\'idvPinId\').value===\''
                                + NEW_CODE + '\') location.href=\'https://www.youtube.com/?signed=1\'">Next</button></div></div>')
                    elif address.path.endswith('/challenge/ipe/consent'):
                        body = ('<p>Get a verification code at ' + EMAIL + '</p>'
                                '<button type="button" onclick="location.href=\'/v3/signin/challenge/ipe/verify\'">Send</button>')
                    elif address.path.endswith('/challenge/selection'):
                        body = ('<div role="link" onclick="location.href=\'/v3/signin/challenge/ipe/consent\'">'
                                'Get a verification code at <span>' + EMAIL + '</span></div>'
                                '<div role="link" onclick="location.href=\'/v3/signin/challenge/ipe/consent\'">'
                                'Get a verification code at <span>' + EMAIL + '</span></div>'
                                '<div role="link">Use another phone or computer to finish signing in</div>')
                    elif address.path.endswith('/challenge/pwd'):
                        body = ('<input type="password" name="hiddenPassword" hidden><input name="Passwd" type="password">'
                                '<div id="passwordNext"><button onclick="if(document.querySelector(\'input[name=Passwd]\').value===\''
                                + PASSWORD + '\') location.href=\'/v3/signin/challenge/selection\'">Next</button></div>')
                    else:
                        body = ('<input id="identifierId" name="identifier" type="text">'
                                '<div id="identifierNext"><button onclick="if(document.getElementById(\'identifierId\').value===\''
                                + EMAIL + '\') location.href=\'/v3/signin/challenge/pwd\'">Next</button></div>')
                elif address.hostname == 'www.youtube.com':
                    if parse_qs(address.query).get('signed'):
                        await context.add_cookies([{'name': 'SID', 'value': 'synthetic-cookie',
                                                   'domain': '.youtube.com', 'path': '/', 'secure': True}])
                        body = ('<button id="avatar-btn"><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"></button>'
                                '<ytd-active-account-header-renderer><span id="email">' + EMAIL + '</span></ytd-active-account-header-renderer>')
                    else:
                        body = 'Synthetic YouTube page'
                else:
                    await request.abort()
                    return
                await request.fulfill(body=body, content_type='text/html')
            await context.route('**/*', route)
            try:
                yield context
            finally:
                state['browsers_closed'] += 1

    reports = []
    credentials = {'email': EMAIL, 'password': PASSWORD, 'code_url': ORIGIN}
    try:
        with tempfile.TemporaryDirectory() as directory, \
                patch('scripts.login_youtube.browser_session', fixture_browser), \
                patch('scripts.login_youtube.email_inbox', fixture_inbox), \
                patch('webapp.email_codes.POLL_SECONDS', 0):
            output = Path(directory) / 'youtube.txt'
            result = await login({}, output, credentials=credentials, expected_email=EMAIL,
                                 on_status=lambda stage, message: reports.append((stage, message)), timeout_seconds=30)
            assert result['account_name'] == EMAIL
            assert b'synthetic-cookie' in output.read_bytes()
            assert any('已提交新收到的邮件验证码' in message for _, message in reports)
            assert sum('已选择当前账号的邮件验证码方式' in message for _, message in reports) == 1
            assert sum('已确认向当前账号邮箱发送验证码' in message for _, message in reports) == 1
            assert not any(value in repr(reports) for value in (PASSWORD, NEW_CODE, 'fixture-private-session'))

            state.update(issued=False, deliver=False)
            cancelled = threading.Event()
            def cancel_at_challenge(stage, message):
                if '正在等待此账号的新邮件验证码' in message:
                    cancelled.set()
            cancelled_output = Path(directory) / 'cancelled.txt'
            try:
                await login({}, cancelled_output, credentials=credentials, expected_email=EMAIL,
                            on_status=cancel_at_challenge, cancelled=cancelled, timeout_seconds=30)
            except LoginCancelled:
                pass
            else:
                raise AssertionError('Cancellation must stop the email wait')
            assert not cancelled_output.exists()
            assert state['posts'] == state['disposed'] == state['browsers_closed'] == 2
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps({'kind': 'youtube_email_login_browser_verified', 'local_fixtures_only': True,
                      'email_password_fresh_code_identity_and_cookie_export': 'passed',
                      'selection_consent_to_ipe_verify': 'passed',
                      'old_code_excluded': True, 'mailbox_received_only_email': True,
                      'cancel_closes_browser_and_inbox': True, 'real_google_login_tested': False}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
