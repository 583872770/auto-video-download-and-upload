"""Real browser lifecycle against a local login fixture, never a Google account."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from app import create_app
from webapp.browser import browser_session, browser_options
from Utils.runtime import ROOT
from scripts.verify_web import QuietHandler


def main():
    output = ROOT / '.cache/validation' / ('youtube-login-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    config = output / 'config.yaml'
    config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
    app = create_app(config, output / 'state', start_worker=False)
    manager = app.extensions['youtube_login']
    complete = threading.Event()
    mode = {'reject': False}
    sessions = []

    @asynccontextmanager
    async def fixture_session(driver, **options):
        assert options['headless'] is False
        assert options['proxy_server'] == ''
        session = {'closed': False}
        sessions.append(session)
        context = None
        try:
            async with browser_session(driver, headless=True, proxy_server='') as context:
                async def route(request):
                    # All login traffic is synthetic; no real YouTube request is sent.
                    if request.request.url == 'https://www.youtube.com/robots.txt':
                        await request.fulfill(status=200, body='User-agent: *', content_type='text/plain')
                    elif request.request.url == 'https://www.youtube.com/':
                        text = 'This browser or app may not be secure' if mode['reject'] else 'Local login fixture'
                        await request.fulfill(status=200, body='<html><body>' + text + '</body></html>', content_type='text/html')
                    else:
                        await request.abort()
                await context.route('**/*', route)

                async def sign_in():
                    while not complete.is_set():
                        await asyncio.sleep(0.05)
                    await context.add_cookies([{'name': 'SID', 'value': 'local-fixture-only',
                                               'domain': '.youtube.com', 'path': '/', 'secure': True}])
                    await context.pages[0].evaluate("document.body.innerHTML += '<div id=avatar-btn><img alt=fixture width=24 height=24></div>'")

                automation = asyncio.create_task(sign_in())
                try:
                    yield context
                finally:
                    automation.cancel()
                    await asyncio.gather(automation, return_exceptions=True)
        finally:
            session['closed'] = context is not None and not context.browser.is_connected()

    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with patch('scripts.login_youtube.browser_session', fixture_session), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base, wait_until='networkidle')
                page.locator('.page-heading [data-open=youtube-login-dialog]').click()
                expect(page.locator('#youtube-login-dialog')).to_be_visible()
                expect(page.locator('#youtube-login-start')).to_be_enabled()
                page.locator('#youtube-login-start').click()
                expect(page.locator('#youtube-login-status')).to_contain_text('请在独立浏览器中登录', timeout=20000)
                page.reload(wait_until='networkidle')
                page.locator('.page-heading [data-open=youtube-login-dialog]').click()
                expect(page.locator('#youtube-login-start')).to_be_disabled()
                page.locator('#youtube-login-cancel').click()
                expect(page.locator('#youtube-login-status')).to_contain_text('已取消', timeout=15000)
                assert sessions[0]['closed']
                assert not (output / 'Cookies/youtube.txt').exists()

                page.locator('#youtube-login-start').click()
                expect(page.locator('#youtube-login-status')).to_contain_text('请在独立浏览器中登录', timeout=20000)
                complete.set()
                expect(page.locator('#youtube-login-status')).to_contain_text('YouTube 登录成功，Cookie 已保存', timeout=20000)
                assert sessions[1]['closed']
                cookie = output / 'Cookies/youtube.txt'
                original = cookie.read_bytes()
                assert b'local-fixture-only' in original
                assert app.extensions['task_store'].runtime()['youtube_cookie'] == str(cookie)
                page.screenshot(path=str(output / 'success-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'success-mobile.png'), full_page=True)

                complete.clear()
                mode['reject'] = True
                page.locator('#youtube-login-start').click()
                expect(page.locator('#youtube-login-status')).to_contain_text('Google 拒绝', timeout=20000)
                assert sessions[2]['closed']
                assert cookie.read_bytes() == original
                expect(page.locator('#youtube-login-start')).to_be_enabled()
                page.locator('#youtube-login-dialog [data-close]').click()
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('.nav-item[data-view=settings]').click()
                page.locator('.youtube-setting [data-open=youtube-login-dialog]').click()
                expect(page.locator('#youtube-login-dialog')).to_be_visible()
                assert not errors, errors
            finally:
                browser.close()
    finally:
        manager.cancel()
        if manager.thread:
            manager.thread.join(timeout=10)
        server.shutdown()
        server.server_close()
    report = {'local_fixture_only': True, 'real_google_login_tested': False,
              'open_cancel_reload_success_rejection': 'passed', 'browsers_closed': sessions,
              'configuration_and_cookie_saved': 'passed', 'mobile_layout': 'passed', 'browser_errors': errors}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('YOUTUBE_LOGIN_UI_VERIFIED:', output / 'report.json')


if __name__ == '__main__':
    main()

