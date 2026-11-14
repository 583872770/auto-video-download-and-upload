"""Exercise the built Vue settings page against an isolated Flask/SQLite fixture."""
import argparse
import json
from pathlib import Path
import re
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler
from tests.test_console_auth import ConsoleAuthTests
from webapp.browser import browser_options


class QuietHandler(WSGIRequestHandler):
    def log(self, kind, message, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-path')
    args = parser.parse_args()
    fixture = ConsoleAuthTests()
    fixture.setUp()
    server = None
    try:
        fixture.setup_admin()
        assert fixture.client.get('/settings').status_code == 200, 'Run frontend build first'
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        output = Path('.runtime/verify/frontend-ui')
        output.mkdir(parents=True, exist_ok=True)
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium, args.browser_path))
            context = browser.new_context(viewport={'width':1440,'height':1050})
            cookie_name = fixture.app.config['SESSION_COOKIE_NAME']
            context.add_cookies([dict(name=cookie_name,value=fixture.client.get_cookie(cookie_name).value,url=url)])
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url + '/settings')
            page.wait_for_selector('[data-ready="true"]')
            field = lambda name: page.locator(f'[name="{name}"]')
            tab = lambda label: page.get_by_role('navigation', name='设置分类').get_by_role('button',name=label)
            save = page.locator('.savebar').get_by_role('button', name='保存设置', exact=True)

            def persist():
                with page.expect_response(lambda response: response.url.endswith('/api/config') and response.request.method == 'PUT') as response:
                    save.click()
                assert response.value.status == 200, response.value.text()
                expect(page.get_by_role('status').filter(has_text='系统设置已保存')).to_be_visible()
                expect(save).to_be_disabled()

            # Preconfiguration and secrets must work without OSS network access.
            expect(field('oss_endpoint')).to_be_visible()
            expect(field('oss_bucket')).to_be_visible()
            field('oss_endpoint').fill('https://oss-cn-hangzhou.aliyuncs.com')
            field('oss_bucket').fill('vue-fixture-bucket')
            field('oss_access_key_id').fill('fixture-id')
            field('oss_access_key_secret').fill('fixture-secret')
            tab('下载策略').click()
            expect(page.get_by_text('历史任务字幕兼容', exact=True)).to_have_count(0)
            expect(field('subtitle_mode')).to_have_count(0)
            expect(field('subtitle_languages')).to_have_count(0)
            field('download_attempts').fill('4')
            persist()
            tab('文件保存').click()
            expect(field('oss_access_key_secret')).to_have_value('')
            expect(field('oss_access_key_secret')).to_have_attribute('placeholder','已配置，留空保留')
            saved = fixture.client.get('/api/config').json['config']
            assert saved['storage_mode'] == 'local' and saved['download_attempts'] == 4
            field('oss_prefix').fill('vue-saved')
            persist()
            import yaml
            assert yaml.safe_load(fixture.config.read_text(encoding='utf-8'))['oss_access_key_secret'] == 'fixture-secret'

            page.locator('[name=storage_mode][value=oss]').check()
            assert page.locator('.oss-settings').bounding_box()['y'] < page.locator('.local-settings').bounding_box()['y']
            page.locator('[name=storage_mode][value=test]').check()
            expect(field('path')).to_be_disabled()
            page.locator('[name=storage_mode][value=local]').check()

            # Hidden validation focuses the right category, retaining every draft.
            tab('空间与日志').click()
            field('disk_resume_free_gb').fill('1')
            field('disk_min_free_gb').fill('2')
            tab('文件保存').click()
            save.click()
            expect(field('disk_resume_free_gb')).to_be_focused()
            field('disk_resume_free_gb').fill('3')
            persist()

            # Failed writes preserve draft; retry is explicit.
            tab('文件保存').click()
            field('oss_prefix').fill('retry-preserved')
            page.route('**/api/config', lambda route: route.fulfill(status=503,json={'error':'fixture unavailable'}) if route.request.method == 'PUT' else route.continue_())
            save.click()
            expect(page.get_by_role('alert')).to_contain_text('fixture unavailable')
            expect(field('oss_prefix')).to_have_value('retry-preserved')
            expect(save).to_be_enabled()
            page.unroute('**/api/config')
            persist()

            # Global defaults do not submit instance drafts or carry instance headers.
            field('oss_prefix').fill('instance-draft')
            tab('新任务交付默认').click()
            page.get_by_role('button',name='四项全选',exact=True).click()
            with page.expect_request(lambda request: '/api/delivery/defaults' in request.url and request.method == 'PUT') as submitted:
                page.get_by_role('button',name='保存新任务默认要求',exact=True).click()
            assert 'x-instance-id' not in submitted.value.headers
            expect(page.get_by_role('status').filter(has_text='新任务默认要求已保存')).to_be_visible()
            tab('文件保存').click()
            expect(field('oss_prefix')).to_have_value('instance-draft')
            expect(save).to_be_enabled()
            page.locator('.savebar').get_by_role('button',name='放弃修改').click()
            page.get_by_role('button',name='放弃并继续').click()
            expect(page.locator('.el-overlay')).to_have_count(0)
            expect(field('oss_prefix')).to_have_value('retry-preserved')

            # Independent assets, responsive layouts and background read-only diagnostics.
            assert not page.locator('script[src$="/app.js"]').count()
            for width in (1440,1280,768,390):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'overflow {width}'
                page.screenshot(path=str(output / f'settings-{width}.png'),full_page=True,animations='disabled')
                if width == 390:
                    assert page.locator('.sidebar').bounding_box()['x'] <= -220
                    buttons = page.locator('.savebar .actions button').all()
                    assert abs(buttons[0].bounding_box()['y'] - buttons[1].bounding_box()['y']) < 3
            tab('运行诊断').click()
            expect(page.get_by_role('heading',name='运行工具')).to_be_visible()
            tab('文件保存').click()
            page.set_viewport_size({'width':1440,'height':1000})

            # Instance selection confirms discard and late responses cannot contaminate state.
            original = fixture.client.get('/api/config').json
            def frontend_bootstrap(route):
                response = route.fetch()
                route.fulfill(response=response,body=re.sub(r'"fleetEnabled": false','"fleetEnabled": true',response.text()))
            page.route('**/settings',frontend_bootstrap)
            page.route('**/api/instances?compact=1',lambda route: route.fulfill(json={'instance_id':'a','items':[
                {'id':'a','name':'实例 A','role':'worker','online':True},
                {'id':'b','name':'实例 B','role':'worker','online':True}]}))
            def scoped_config(route):
                identity = route.request.headers.get('x-instance-id')
                route.fulfill(json={**original,'config':{**original['config'],'oss_prefix':'instance-' + str(identity)}})
            page.route('**/api/config',scoped_config)
            page.reload()
            page.wait_for_selector('[data-ready="true"]')
            field('oss_prefix').fill('unsaved-a')
            page.get_by_role('combobox',name='当前实例').select_option('b')
            page.get_by_role('button',name='继续编辑',exact=True).click()
            expect(page.locator('.el-overlay')).to_have_count(0)
            expect(field('oss_prefix')).to_have_value('unsaved-a')
            page.get_by_role('combobox',name='当前实例').select_option('b')
            page.get_by_role('button',name='放弃并继续').click()
            expect(field('oss_prefix')).to_have_value('instance-b')
            expect(page.locator('.savebar')).to_contain_text('实例 B')
            context.close()

            # Server-provided role makes all editable forms read-only.
            fixture.client.post('/api/auth/users', json={'username':'reader','role':'viewer','password':'secure-test-password'},headers=fixture.headers)
            reader, _ = fixture.login('reader')
            readonly = browser.new_context()
            readonly.add_cookies([dict(name=cookie_name,value=reader.get_cookie(cookie_name).value,url=url)])
            reader_page = readonly.new_page()
            reader_page.goto(url + '/settings')
            reader_page.wait_for_selector('[data-ready="true"]')
            expect(reader_page.locator('[name=oss_endpoint]')).to_be_disabled()
            expect(reader_page.locator('.savebar').get_by_role('button',name='保存设置',exact=True)).to_be_disabled()
            readonly.close()
            # Old root hashes now land in the Vue workbench.
            legacy = browser.new_context()
            legacy.add_cookies([dict(name=cookie_name,value=fixture.client.get_cookie(cookie_name).value,url=url)])
            legacy_page = legacy.new_page()
            legacy_page.on('pageerror',lambda error: errors.append(str(error)))
            legacy_page.goto(url + '/#overview')
            expect(legacy_page.get_by_role('heading',name='下载工作台',exact=True)).to_be_visible()
            expect(legacy_page.get_by_role('link',name='系统设置',exact=True)).to_have_attribute('href','/settings')
            legacy.close()
            browser.close()
        assert not errors, errors
        print(json.dumps({'ok':True,'screenshots':str(output),'browser_errors':errors},ensure_ascii=False))
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
