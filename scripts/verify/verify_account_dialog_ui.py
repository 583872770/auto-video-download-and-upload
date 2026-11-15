"""Verify account editing, fixed inbox, dialog flow and responsive presentation."""
from pathlib import Path
import threading

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from scripts.verify.support import QuietHandler
from tests.test_account_library import AccountLibraryTests
from webapp.account_routes import PREFIX
from webapp.browser import browser_options


def main():
    output = Path('.runtime/verify/account-dialog')
    output.mkdir(parents=True, exist_ok=True)
    fixture = AccountLibraryTests('test_encrypted_roundtrip_public_queries_and_blank_password')
    fixture.setUp()
    server = None
    try:
        identity = fixture.add('studio.demo@example.test', password='DemoPass!2026',
            extra='工作室 A · 用于字幕任务', note='日常下载主账号', instance_id=fixture.scope)
        fixture.cookie(identity=identity)
        fixture.add('backup.demo@example.test', password='DemoPass!2026', note='备用账号')
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie_name = fixture.app.config['SESSION_COOKIE_NAME']
        cookie = fixture.client.get_cookie(cookie_name)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
            context.add_cookies([{'name': cookie_name, 'value': cookie.value, 'url': base}])
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/accounts')
            row = page.get_by_role('row').filter(has_text='studio.demo@example.test')
            row.get_by_role('button', name='管理', exact=True).click()
            dialog = page.locator('.account-dialog')
            expect(dialog.get_by_label('密码', exact=True)).to_have_value('DemoPass!2026')
            expect(dialog.get_by_label('附加信息', exact=True)).to_have_value('工作室 A · 用于字幕任务')
            expect(dialog.get_by_label('密码', exact=True)).to_have_attribute('type', 'text')
            assert dialog.locator('input[type=url]').count() == 0
            expect(dialog.get_by_role('link', name='fbvjdjcf.asia')).to_have_attribute('href', 'http://fbvjdjcf.asia/')
            dialog.screenshot(path=str(output / 'account-profile-desktop.png'))
            dialog.get_by_label('附加信息', exact=True).fill('工作室 A · 视频与字幕')
            dialog.get_by_label('备注', exact=True).fill('已核对资料')
            dialog.get_by_role('button', name='保存修改').click()
            expect(dialog.get_by_label('附加信息', exact=True)).to_be_enabled()
            expect(dialog.get_by_label('附加信息', exact=True)).to_have_value('工作室 A · 视频与字幕')
            for field, value in [('password', 'DemoPass!2026'), ('extra', '工作室 A · 视频与字幕')]:
                assert fixture.client.post(PREFIX+'/'+identity+'/secrets', headers=fixture.headers,
                    json={'field': field}).json['value'] == value
            dialog.get_by_role('button', name='登录与凭据', exact=True).click()
            expect(dialog.get_by_text('当前 Cookie', exact=True)).to_be_visible()
            dialog.screenshot(path=str(output / 'account-cookies-desktop.png'))
            dialog.get_by_role('button', name='重新登录', exact=True).click()
            login = page.locator('.account-login-dialog')
            expect(login).to_be_visible()
            assert page.locator('dialog:modal').count() == 1, 'Login dialog stacked on the account dialog'
            login.screenshot(path=str(output / 'account-login-desktop.png'))
            login.get_by_role('button', name='返回账号').click()
            expect(dialog).to_be_visible()
            dialog.get_by_role('button', name='运行记录', exact=True).click()
            expect(dialog.get_by_role('heading', name='运行事件')).to_be_visible()
            dialog.get_by_role('button', name='账号资料', exact=True).click()
            page.set_viewport_size({'width': 390, 'height': 844})
            expect(dialog.get_by_label('密码', exact=True)).to_have_value('DemoPass!2026')
            assert dialog.evaluate('(e) => e.scrollWidth <= e.clientWidth + 1')
            assert dialog.locator('.account-body').evaluate('(e) => e.scrollWidth <= e.clientWidth + 1')
            expect(dialog.get_by_role('button', name='保存修改')).to_be_in_viewport()
            page.screenshot(path=str(output / 'account-profile-mobile.png'))
            page.set_viewport_size({'width': 1440, 'height': 1000})
            dialog.get_by_role('button', name='完成', exact=True).click()
            page.get_by_role('button', name='导入账号', exact=True).click()
            importing = page.locator('.account-import-dialog')
            importing.locator('textarea').fill('new.demo@example.test----DemoPass!2026----工作室 B')
            importing.get_by_role('button', name='预览账号', exact=True).click()
            expect(importing.get_by_role('button', name='确认导入 1 个账号')).to_be_enabled()
            importing.screenshot(path=str(output / 'account-import-desktop.png'))
            importing.get_by_role('button', name='确认导入 1 个账号').click()
            expect(importing).to_have_count(0)
            assert fixture.client.get(PREFIX).json['total'] == 3
            assert not errors, errors
            browser.close()
        print('Verified account edit/save, visible credentials, fixed inbox, login flow, import and mobile layout.')
        print(output.resolve())
    finally:
        if server:
            server.shutdown()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
