"""Exercise Cookie management in real Chromium using isolated, dummy credentials."""
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app import create_app


def main():
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    scratch = Path(__file__).resolve().parents[2] / '.runtime' / 'verify'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as temporary, patch.dict(os.environ, environment, clear=True), \
            patch('webapp.youtube_auth.probe', return_value=('valid', 'YouTube 登录验证通过。')) as probe:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False)
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#settings')
                status=page.evaluate("""async key => {
                    const csrf=document.querySelector('meta[name="csrf-token"]').content;
                    const r=await fetch('/api/auth/setup',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({bootstrap_key:key,username:'admin',password:'ui-test-only-password'})});return r.status;
                }""", app.extensions['console_auth'].bootstrap_key)
                assert status==201, status
                page.reload()
                expect(page.locator('.settings-tabs [role=tab]')).to_have_count(4)
                expect(page.locator('[name="worker_count"]')).to_have_value('1')
                # Controls and their categories exist before application JS runs.
                # Hydration must not clone fields or change their constraints.
                plain = browser.new_page(java_script_enabled=False, storage_state=page.context.storage_state())
                plain.goto(f'http://127.0.0.1:{server.server_port}/')
                schema = """nodes => nodes.map(node => ({name: node.name, tag: node.tagName,
                    type: node.getAttribute('type'), min: node.getAttribute('min'), max: node.getAttribute('max'),
                    required: node.required, options: [...(node.options || [])].map(option => option.value),
                    category: node.closest('[data-settings-category]')?.dataset.settingsCategory}))"""
                static_fields = plain.locator('#settings-form [name]').evaluate_all(schema)
                live_fields = page.locator('#settings-form [name]').evaluate_all(schema)
                assert static_fields and static_fields == live_fields
                assert len({field['name'] for field in static_fields}) == len(static_fields)
                expect(plain.locator('#settings-form [role="tabpanel"]')).to_have_count(4)
                assert page.evaluate("""() => { const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
                    return ids.length === new Set(ids).size; }"""), 'duplicate DOM ids'
                plain.close()
                page.locator('#settings-tab-download').focus()
                page.keyboard.press('ArrowRight')
                expect(page.locator('#settings-tab-performance')).to_be_focused()
                expect(page.locator('#settings-pane-performance')).to_be_visible()
                page.keyboard.press('End')
                expect(page.locator('#settings-pane-diagnostics')).to_be_visible()
                page.keyboard.press('Home')
                expect(page.locator('#settings-pane-download')).to_be_visible()
                page.evaluate("setView('accounts'); selectAccountTab('pool')")
                interval = page.locator('[name="youtube_start_interval"]')
                interval_value = interval.input_value()
                interval.fill('')
                page.locator('#account-pool-form button[type="submit"]').click()
                expect(interval).to_be_visible()
                interval.fill(interval_value)
                page.locator('#account-pool-form button[type="submit"]').click()
                expect(page.locator('#account-pool-save-state')).to_contain_text('已保存')
                page.evaluate("setView('settings')")
                preview = Path('.runtime/cache-archive-20261111/workspace-layout-preview')
                preview.mkdir(parents=True, exist_ok=True)
                for key in ('download', 'performance', 'storage', 'diagnostics'):
                    page.locator(f'#settings-tab-{key}').click()
                    expect(page.locator(f'#settings-pane-{key}')).to_be_visible()
                    expect(page.locator('.settings-category:visible')).to_have_count(1)
                    page.screenshot(path=str(preview / f'{key}.png'), full_page=True)
                page.get_by_role('tab', name='并发与渠道').click()
                page.locator('[name="worker_count"]').fill('2')
                mode = page.locator('[name="throughput_mode"]')
                mode.select_option('manual')
                page.locator('[name="scheduler_mode"]').select_option('auto')
                expect(mode).to_be_enabled()
                expect(mode).to_have_value('manual')
                expect(page.locator('#control-mode-note')).to_contain_text('自定义')
                mode.select_option('auto')
                page.locator('[name="scheduler_mode"]').select_option('ordered_fallback')
                expect(mode).to_have_value('auto')
                expect(page.locator('#control-mode-note')).to_contain_text('自动调节')
                mode.select_option('manual')
                page.get_by_role('tab', name='存储与清理').click()
                page.locator('[name="dir"]').fill('layout-check')
                page.get_by_role('tab', name='下载策略').click()
                page.locator('#settings-form button[type="submit"]').click()
                expect(page.locator('#settings-save-state')).to_contain_text('设置已保存')
                # Saving from one category includes changes in other categories.
                saved = page.request.get(f'http://127.0.0.1:{server.server_port}/api/config').json()['config']
                assert saved['worker_count'] == 2 and saved['dir'] == 'layout-check'
                assert saved['throughput_mode'] == 'manual'
                page.set_viewport_size({'width': 390, 'height': 844})
                for key in ('download', 'performance', 'storage', 'diagnostics'):
                    page.locator(f'#settings-tab-{key}').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), key
                    page.screenshot(path=str(preview / f'{key}-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.evaluate("setView('accounts'); selectAccountTab('pool')")
                page.locator('[data-account-tab=library]').click()
                rows = page.locator('#account-rows tr')
                def upload(name, filename='cookies.txt'):
                    page.locator('#credential-upload').click()
                    page.locator('#account-upload-name').fill(name)
                    page.locator('#account-upload-file').set_input_files({'name': filename, 'mimeType': 'text/plain',
                        'buffer': b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tDUMMY_SECRET\n'})
                    page.locator('#account-upload-confirmed').check()
                    page.locator('#account-upload-form [type=submit]').click()
                    expect(page.locator('#account-upload-dialog')).not_to_be_visible()
                def detail(name):
                    rows.filter(has_text=name).locator('[data-credential-open]').click()
                    expect(page.locator('#account-detail-dialog')).to_be_visible()
                def close_detail():
                    page.locator('#account-detail-dialog [data-close]').click()
                def remove(name, cancel=False):
                    detail(name)
                    page.locator('[data-account-cookie-delete]').click()
                    page.get_by_role('button', name='取消' if cancel else '确认', exact=True).last.click()
                    if cancel: close_detail()
                    else: expect(page.locator('#account-detail-dialog')).not_to_be_visible()
                for name in ('Account A', '<b>Account B</b>'):
                    upload(name)
                    expect(rows.filter(has_text=name)).to_be_visible()
                expect(rows).to_have_count(2)
                rows.filter(has_text='Account A').locator('[data-credential-check]').click()
                expect(rows.filter(has_text='Account A')).to_contain_text('最近验证通过')
                assert probe.call_count == 1
                detail('Account A')
                expect(page.locator('#account-associated-cookies')).to_contain_text('直连')
                with page.expect_download() as downloaded:
                    page.locator('[data-account-cookie-export]').click()
                assert 'DUMMY_SECRET' in Path(downloaded.value.path()).read_text()
                close_detail()
                detail('Account B')
                page.locator('[data-account-cookie-select]').click()
                page.get_by_role('button', name='确认', exact=True).last.click()
                expect(rows.filter(has_text='Account B')).to_contain_text('当前选用')
                close_detail()
                remove('Account B', cancel=True)
                expect(rows).to_have_count(2)
                expect(rows.filter(has_text='Account B')).to_contain_text('当前选用')
                remove('Account A')
                expect(rows).to_have_count(1)
                expect(rows.filter(has_text='Account B')).to_contain_text('当前选用')
                expect(page.locator('#account-rows b')).to_have_count(0)
                upload('', 'filename-default.txt')
                expect(rows.filter(has_text='filename-default.txt')).to_be_visible()
                expect(rows).to_have_count(2)
                assert 'DUMMY_SECRET' not in page.locator('#account-rows').inner_text()
                output = Path('.runtime/cache-archive-20261111/cookie-profiles-preview')
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / 'desktop.png'))
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output / 'mobile.png'))
                remove('Account B')
                expect(rows).to_have_count(1)
                expect(rows.filter(has_text='filename-default.txt')).to_contain_text('当前选用')
                selected = page.request.get(f'http://127.0.0.1:{server.server_port}/api/youtube/cookies').json()['items'][0]
                selected_path = f"Cookies/profiles/{selected['id']}.txt"
                assert page.request.get(f'http://127.0.0.1:{server.server_port}/api/config').json()['config']['youtube_cookie'] == selected_path
                app.extensions['youtube_login']._write({'state': 'succeeded', 'message': 'Fixture past login'})
                page.reload()
                page.evaluate("setView('accounts'); selectAccountTab('library')")
                expect(rows).to_have_count(1)
                expect(rows.first).to_contain_text('当前选用')
                assert page.evaluate('state.config.youtube_cookie') == selected_path
                remove('filename-default.txt')
                expect(rows).to_have_count(0)
                assert page.request.get(f'http://127.0.0.1:{server.server_port}/api/config').json()['config']['youtube_cookie'] == ''
                assert page.evaluate('state.config.youtube_cookie') == ''
                page.reload()
                page.evaluate("setView('accounts'); selectAccountTab('library')")
                expect(rows).to_have_count(0)
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
    print('Cookie UI: upload, select, login check, cancel/delete, active replacement, empty selection, reload, escaping, mobile layout: OK')


if __name__ == '__main__':
    main()
