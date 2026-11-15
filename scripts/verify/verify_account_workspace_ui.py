"""Verify the single account inventory, runtime filters, checks and participation."""
from pathlib import Path
import threading
import time
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from scripts.verify.support import QuietHandler
from tests.test_account_library import AccountLibraryTests, COOKIE
from webapp.account_library import AccountLibrary
from webapp.account_routes import PREFIX
from webapp.browser import browser_options


def main():
    output = Path('.runtime/verify/account-workspace')
    output.mkdir(parents=True, exist_ok=True)
    fixture = AccountLibraryTests('test_encrypted_roundtrip_public_queries_and_blank_password')
    fixture.setUp()
    server = None
    batch = fixture.app.extensions['account_check_batch']
    try:
        ids, profiles = {}, {}
        for name, note in [('studio', '日常下载主账号'), ('cooling', '自动等待冷却后恢复'),
                           ('attention', '需要完成网页验证'), ('backup', '备用账号')]:
            ids[name] = fixture.add(name + '@example.test', instance_id=fixture.scope, note=note)
            profiles[name] = fixture.cookie(COOKIE.replace(b'account-library-fixture', name.encode()), identity=ids[name])
        ids['unbound'] = fixture.add('new.account@example.test', note='尚未分配，等待登录')
        until = time.time() + 3675
        inventory = AccountLibrary.inventory
        def snapshot(lib, db, profiles=None):
            nodes, rows = inventory(lib, db, profiles)
            for row in rows:
                name = row['name'].split('@')[0]
                if name == 'studio':
                    row['auth'] = {'state': 'valid', 'checked_at': time.time() - 120}
                    row['download_state'] = 'ready'
                elif name == 'cooling':
                    row['auth'] = {'state': 'valid', 'checked_at': time.time() - 180}
                    row['download_state'] = 'cooldown'
                    row['session'] = {'cooldown_until': until}
                elif name == 'attention':
                    row['auth'] = {'state': 'verification_required', 'message': '请先完成网页验证，再检测登录。'}
                    row['download_state'] = 'manual'
            return nodes, rows
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie_name = fixture.app.config['SESSION_COOKIE_NAME']
        cookie = fixture.client.get_cookie(cookie_name)
        with patch.object(AccountLibrary, 'inventory', snapshot), \
                patch('webapp.youtube_auth.probe', return_value=('valid', '登录检测通过。')), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            context = browser.new_context(viewport={'width': 1540, 'height': 1050}, reduced_motion='reduce')
            context.add_cookies([{'name': cookie_name, 'value': cookie.value, 'url': base}])
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/accounts')
            tabs = page.locator('.account-status-tabs')
            expect(tabs.get_by_role('button', name='全部账号')).to_contain_text('5')
            expect(tabs.get_by_role('button', name='已验证登录')).to_contain_text('2')
            expect(tabs.get_by_role('button', name='冷却中')).to_contain_text('1')
            expect(page.locator('.account-table tbody tr')).to_have_count(5)
            expect(page.get_by_label('当前实例', exact=True)).to_have_count(0)
            page.screenshot(path=str(output / 'accounts-desktop.png'), full_page=True)
            tabs.get_by_role('button', name='冷却中').click()
            expect(page.locator('.account-table tbody tr')).to_have_count(1)
            expect(page.locator('.account-table')).to_contain_text('cooling@example.test')
            timer = page.locator('.cooldown-timer')
            expect(timer).to_contain_text('秒后检查恢复')
            before = timer.inner_text()
            page.wait_for_function('(before) => document.querySelector(".cooldown-timer")?.textContent !== before', arg=before)
            page.screenshot(path=str(output / 'accounts-cooldown.png'), full_page=True)
            tabs.get_by_role('button', name='全部账号').click()
            page.get_by_label('按绑定实例筛选').select_option('unbound')
            expect(page.locator('.account-table tbody tr')).to_have_count(1)
            expect(tabs.get_by_role('button', name='全部账号')).to_contain_text('1')
            page.get_by_label('按绑定实例筛选').select_option('')
            for name in ('studio@example.test', 'new.account@example.test'):
                page.get_by_label('选择账号 ' + name, exact=True).check()
            page.get_by_role('button', name='批量检测登录', exact=True).click()
            popup = page.locator('.account-batch-dialog')
            popup.get_by_role('button', name='开始检测登录', exact=True).click()
            expect(popup.locator('.batch-success')).to_have_text('成功 1', timeout=10000)
            expect(popup).to_contain_text('尚未保存登录凭据')
            popup.screenshot(path=str(output / 'batch-check-results.png'))
            popup.get_by_role('button', name='关闭', exact=True).click()
            for name in ('studio@example.test', 'cooling@example.test'):
                page.get_by_label('选择账号 ' + name, exact=True).check()
            page.get_by_role('button', name='加入下载', exact=True).click()
            joining = page.get_by_role('dialog', name='加入下载', exact=True)
            joining.get_by_role('button', name='确认加入', exact=True).click()
            expect(joining).to_contain_text('现有 2 个池内账号')
            joining.get_by_role('button', name='关闭', exact=True).click()
            config = fixture.client.get('/api/config').json['config']
            assert set(config['youtube_cookie_pool']) == {profiles['studio'], profiles['cooling']}
            for name in ('studio@example.test', 'cooling@example.test'):
                page.get_by_label('选择账号 ' + name, exact=True).check()
            page.get_by_role('button', name='移出下载', exact=True).click()
            removing = page.get_by_role('dialog', name='移出下载', exact=True)
            removing.get_by_role('button', name='确认移出', exact=True).click()
            expect(removing).to_contain_text('现有 0 个池内账号')
            removing.get_by_role('button', name='关闭', exact=True).click()
            assert not fixture.client.get('/api/config').json['config']['youtube_cookie_pool']
            page.get_by_role('button', name='请求节奏', exact=True).click()
            expect(page.get_by_role('button', name='保存节奏', exact=True)).to_be_visible()
            expect(page.locator('.account-table')).to_have_count(0)
            expect(page.locator('.account-pacing').get_by_role('table')).to_have_count(0)
            interval = page.get_by_label('两次任务启动最小间隔（秒）', exact=False)
            interval.fill('111')
            expect(page.get_by_role('button', name='账号清单', exact=True)).to_be_disabled()
            page.get_by_role('button', name='保存节奏', exact=True).click()
            expect(page.get_by_role('status')).to_contain_text('请求节奏已保存')
            assert fixture.client.get('/api/config').json['config']['youtube_start_interval'] == 111
            page.screenshot(path=str(output / 'request-pacing.png'), full_page=True)
            page.get_by_role('button', name='账号清单', exact=True).click()
            page.set_viewport_size({'width': 390, 'height': 844})
            expect(page.locator('.account-table')).to_be_visible()
            assert page.locator('main.content').evaluate('(e) => e.scrollWidth <= e.clientWidth + 1')
            page.screenshot(path=str(output / 'accounts-mobile.png'), full_page=True)
            assert not errors, errors
            browser.close()
        print('Verified status counts, instance filters, live cooldown, batch checks, download participation and pacing separation.')
        print(output.resolve())
    finally:
        if batch.thread:
            batch.thread.join(5)
        if server:
            server.shutdown()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
