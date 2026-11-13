"""Check settings navigation and persistence in Chromium with an isolated database."""
import argparse
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler
import yaml

from tests.test_console_auth import ConsoleAuthTests


class QuietHandler(WSGIRequestHandler):
    def log(self, kind, message, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-path', help='Optional Chromium executable')
    args = parser.parse_args()
    fixture = ConsoleAuthTests()
    fixture.setUp()
    server = None
    try:
        fixture.setup_admin()
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True,
                             request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        preview = Path('.cache/settings-ui')
        preview.mkdir(parents=True, exist_ok=True)
        errors = []
        with sync_playwright() as pw:
            options = dict(headless=True)
            if args.browser_path:
                options['executable_path'] = args.browser_path
            browser = pw.chromium.launch(**options)
            context = browser.new_context(viewport=dict(width=1440, height=1000))
            cookie_name = fixture.app.config['SESSION_COOKIE_NAME']
            context.add_cookies([dict(name=cookie_name,
                value=fixture.client.get_cookie(cookie_name).value, url=url)])
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url + '/#settings')
            page.wait_for_function('state.config && !state.polling')
            form = page.locator('#settings-form')
            save = form.locator('button[type=submit]')
            field = lambda name: form.locator(f'[name="{name}"]')
            tab = lambda key: page.locator(f'#settings-tab-{key}')

            def persist():
                with page.expect_response(lambda response: response.request.method == 'PUT'
                                          and response.url.endswith('/api/config')) as result:
                    save.click()
                assert result.value.status == 200, result.value.status
                expect(page.locator('#settings-save-state')).to_contain_text('系统设置已保存')
                expect(save).to_be_enabled()
                return yaml.safe_load(fixture.config.read_text(encoding='utf-8'))

            expect(page.locator('.settings-tabs [role=tab]')).to_have_count(6)
            expect(page.locator('#settings-pane-storage')).to_be_visible()
            expect(field('oss_endpoint')).to_be_visible()
            expect(field('oss_bucket')).to_be_visible()
            expect(field('storage_mode')).to_have_value('local')
            assert not field('oss_endpoint').evaluate('(el) => el.required')
            assert page.locator('#settings-form #delivery-defaults-form').count() == 0
            names = form.locator('[name]').evaluate_all('(nodes) => nodes.map(el => el.name)')
            assert len(names) == len(set(names)), 'duplicate setting fields'
            assert page.evaluate('''() => {
                const ids = [...document.querySelectorAll('[id]')].map(el => el.id);
                return ids.length === new Set(ids).size;
            }'''), 'duplicate DOM ids'

            # Configuring OSS in advance must work without switching modes or uploading.
            field('oss_endpoint').fill('https://oss-cn-hangzhou.aliyuncs.com')
            field('oss_bucket').fill('ui-fixture-bucket')
            field('oss_prefix').fill('fixture')
            field('oss_access_key_id').fill('fixture-id')
            field('oss_access_key_secret').fill('fixture-secret')
            config = persist()
            assert config['storage_mode'] == 'local'
            assert config['oss_bucket'] == 'ui-fixture-bucket'
            assert config['oss_access_key_secret'] == 'fixture-secret'
            expect(field('oss_access_key_secret')).to_have_value('')

            # Missing OSS fields reveal the right tab before browser validation focuses them.
            field('storage_mode').select_option('oss')
            assert page.locator('.oss-settings-panel').bounding_box()['y'] < \
                page.locator('.local-storage-panel').bounding_box()['y']
            field('oss_endpoint').fill('')
            tab('download').click()
            save.click()
            expect(page.locator('#settings-pane-storage')).to_be_visible()
            expect(field('oss_endpoint')).to_be_focused()
            field('oss_endpoint').fill('https://oss-cn-hangzhou.aliyuncs.com')
            advanced = page.locator('.oss-settings-panel details')
            advanced.locator('summary').click()
            field('oss_upload_limit').fill('9')
            advanced.locator('summary').click()
            tab('download').click()
            save.click()
            expect(field('oss_upload_limit')).to_be_visible()
            expect(field('oss_upload_limit')).to_be_focused()
            field('oss_upload_limit').fill('3')

            # A separate global-default save cannot submit or discard instance edits.
            field('oss_prefix').fill('draft-kept')
            tab('delivery').click()
            expect(form).not_to_be_visible()
            defaults = page.locator('#delivery-defaults-form')
            defaults.locator('[data-delivery-preset=all]').click()
            defaults.locator('button[type=submit]').click()
            expect(defaults.locator('[data-delivery-message]')).to_contain_text('默认要求已保存')
            tab('storage').click()
            expect(field('oss_prefix')).to_have_value('draft-kept')
            assert fixture.client.get('/api/delivery/defaults').json['spec']['outputs'] == [
                'subtitle', 'video_only', 'audio_only', 'av']

            # Save from a different tab; the secret's blank UI must keep the saved value.
            tab('maintenance').click()
            field('disk_failure_log_days').fill('9')
            tab('performance').click()
            config = persist()
            assert config['storage_mode'] == 'oss'
            assert config['oss_prefix'] == 'draft-kept'
            assert config['oss_upload_limit'] == 3
            assert config['disk_failure_log_days'] == 9
            assert config['oss_access_key_secret'] == 'fixture-secret'
            public = fixture.client.get('/api/config').json['config']
            assert public['oss_access_key_secret'] == ''
            assert public['oss_access_key_secret_configured'] is True
            tab('storage').click()
            advanced.locator('summary').click()
            page.locator('#toasts').evaluate('(el) => el.replaceChildren()')
            for width, height in ((1440, 1000), (390, 844)):
                page.set_viewport_size(dict(width=width, height=height))
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(preview / f'storage-oss-{width}.png'),
                                full_page=True, animations='disabled')
            page.set_viewport_size(dict(width=1440, height=1000))
            tab('diagnostics').click()
            expect(form.locator('.settings-save')).not_to_be_visible()
            tab('storage').focus()
            page.keyboard.press('ArrowDown')
            expect(tab('download')).to_be_focused()
            page.keyboard.press('End')
            expect(page.locator('#settings-pane-delivery')).to_be_visible()
            page.keyboard.press('Home')
            expect(page.locator('#settings-pane-storage')).to_be_visible()
            page.locator('#workspace-disk-status').click()
            expect(page.locator('#settings-pane-maintenance')).to_be_visible()

            # Switching modes must not hide or erase OSS configuration.
            tab('storage').click()
            field('storage_mode').select_option('test')
            expect(field('path')).to_be_disabled()
            expect(field('oss_endpoint')).to_be_visible()
            field('storage_mode').select_option('local')
            assert page.locator('.local-storage-panel').bounding_box()['y'] < \
                page.locator('.oss-settings-panel').bounding_box()['y']
            expect(field('path')).to_be_enabled()
            assert not field('oss_endpoint').evaluate('(el) => el.required')
            persist()
            page.reload()
            expect(field('oss_bucket')).to_have_value('ui-fixture-bucket')

            categories = ('storage', 'download', 'performance', 'maintenance', 'diagnostics', 'delivery')
            for width, height in ((1440, 1000), (1280, 900), (768, 1024), (390, 844)):
                page.set_viewport_size(dict(width=width, height=height))
                for key in categories:
                    tab(key).click()
                    expect(page.locator(f'#settings-pane-{key}')).to_be_visible()
                    expect(page.locator('.settings-category:visible')).to_have_count(1)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, key)
                    if width in (1440, 390):
                        page.evaluate('window.scrollTo(0, 0)')
                        page.screenshot(path=str(preview / f'{key}-{width}.png'),
                                        full_page=True, animations='disabled')
            assert not errors, errors
            browser.close()
        print(json.dumps(dict(oss_discoverable=True, cross_tab_save=True,
            secret_preserved=True, invalid_field_navigation=True, defaults_isolated=True,
            responsive_widths=[1440, 1280, 768, 390], browser_errors=errors)))
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
