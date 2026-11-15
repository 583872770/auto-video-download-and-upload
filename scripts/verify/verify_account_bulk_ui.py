"""Exercise bulk binding, background login progress and cross-page selection."""
import asyncio
from pathlib import Path
import threading
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from scripts.verify.support import QuietHandler
from tests.test_account_library import AccountLibraryTests, COOKIE
from webapp.account_routes import PREFIX
from webapp.browser import browser_options


def main():
    output = Path('.runtime/verify/account-bulk')
    output.mkdir(parents=True, exist_ok=True)
    fixture = AccountLibraryTests('test_encrypted_roundtrip_public_queries_and_blank_password')
    fixture.setUp()
    batch = fixture.app.extensions['account_login_batch']
    batch.interval = .02
    release, entered = threading.Event(), threading.Event()
    server = None

    async def browser(config, destination, *, cancelled, expected_email, **options):
        from scripts.login_youtube import AutomaticLoginNeedsUser, LoginCancelled
        entered.set()
        while not release.is_set():
            if cancelled.is_set():
                raise LoginCancelled('已取消测试登录。')
            await asyncio.sleep(.02)
        if expected_email.startswith('manual'):
            raise AutomaticLoginNeedsUser('需要在浏览器中完成短信验证，请单独登录此账号。')
        destination.write_bytes(COOKIE.replace(b'account-library-fixture', expected_email.encode()))
        return {'account_name': expected_email}

    try:
        for email in ('studio.demo@example.test', 'backup.demo@example.test', 'manual.demo@example.test'):
            fixture.add(email, password='DemoPass!2026')
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie_name = fixture.app.config['SESSION_COOKIE_NAME']
        cookie = fixture.client.get_cookie(cookie_name)
        with patch('webapp.youtube_login.login', browser), sync_playwright() as pw:
            browser_ui = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            context = browser_ui.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
            context.add_cookies([{'name': cookie_name, 'value': cookie.value, 'url': base}])
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/accounts')
            page.get_by_label('全选本页账号').check()
            expect(page.locator('.account-bulk-toolbar')).to_contain_text('已选 3 个账号')
            page.get_by_role('button', name='批量修改实例', exact=True).click()
            popup = page.locator('.account-batch-dialog')
            popup.get_by_label('目标实例').select_option(fixture.scope)
            popup.screenshot(path=str(output / 'batch-bind.png'))
            popup.get_by_role('button', name='确认修改实例').click()
            expect(popup.locator('.batch-success')).to_have_text('成功 3')
            popup.get_by_role('button', name='关闭', exact=True).click()
            expect(page.get_by_label('全选本页账号')).not_to_be_checked()
            page.get_by_label('全选本页账号').check()
            page.get_by_role('button', name='批量自动登录', exact=True).click()
            expect(popup.get_by_role('button', name='开始自动登录')).to_be_enabled()
            popup.screenshot(path=str(output / 'batch-login-ready.png'))
            popup.get_by_role('button', name='开始自动登录').click()
            expect(popup.get_by_role('button', name='后台继续')).to_be_visible()
            assert entered.wait(3)
            popup.get_by_role('button', name='后台继续').click()
            page.reload()
            expect(page.locator('.account-batch-banner')).to_contain_text('批量自动登录进行中')
            page.get_by_role('button', name='查看进度', exact=True).click()
            expect(popup.get_by_role('button', name='停止批量登录')).to_be_visible()
            release.set()
            expect(popup.locator('.batch-success')).to_have_text('成功 2', timeout=12000)
            expect(popup.locator('.batch-summary')).to_contain_text('需人工处理 1')
            expect(popup).to_contain_text('短信验证')
            popup.screenshot(path=str(output / 'batch-login-results.png'))
            popup.get_by_role('button', name='关闭', exact=True).click()
            raw = '\n'.join(f'extra{i}@example.test----DemoPass!2026' for i in range(51))
            preview = fixture.preview(raw)
            result = fixture.client.post(PREFIX+'/import', headers=fixture.headers, json={'text': raw, 'token': preview['token']})
            assert result.status_code == 200
            page.reload()
            page.get_by_label('全选本页账号').check()
            expect(page.locator('.account-bulk-toolbar')).to_contain_text('已选 50 个账号')
            page.get_by_role('button', name='下一页', exact=True).click()
            expect(page.get_by_label('全选本页账号')).not_to_be_checked()
            page.get_by_role('checkbox', name='选择账号 ', exact=False).first.check()
            expect(page.locator('.account-bulk-toolbar')).to_contain_text('已选 51 个账号')
            page.get_by_role('button', name='上一页', exact=True).click()
            expect(page.get_by_label('全选本页账号')).to_be_checked()
            page.get_by_role('button', name='清空选择', exact=True).click()
            expect(page.get_by_label('全选本页账号')).not_to_be_checked()
            assert not errors, errors
            browser_ui.close()
        print('Verified cross-page selection, bulk binding, server-owned login after reload, and per-account results.')
        print(output.resolve())
    finally:
        release.set()
        state = batch.status()
        if state['active']:
            batch.cancel(state['id'])
        if batch.thread:
            batch.thread.join(5)
        if server:
            server.shutdown()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
