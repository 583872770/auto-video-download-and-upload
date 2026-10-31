"""Exercise Cookie management in real Chromium using isolated, dummy credentials."""
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app import create_app


def main():
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    scratch = Path(__file__).resolve().parents[1] / '.cache'
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
                expect(page.get_by_role('tab')).to_have_count(5)
                preview = Path('.cache/workspace-layout-preview')
                preview.mkdir(parents=True, exist_ok=True)
                for key in ('download', 'accounts', 'performance', 'storage', 'diagnostics'):
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
                for key in ('download', 'accounts', 'performance', 'storage', 'diagnostics'):
                    page.locator(f'#settings-tab-{key}').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), key
                    page.screenshot(path=str(preview / f'{key}-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.get_by_role('tab', name='Cookie 与登录').click()
                page.locator('[data-open="youtube-cookie-dialog"]').first.click()
                dialog = page.locator('#youtube-cookie-dialog')
                expect(dialog).to_be_visible()
                expect(dialog).to_contain_text('当前未选用已保存的 Cookie')
                form = dialog.locator('form')
                for name in ('Account A', '<b>Account B</b>'):
                    form.locator('[name="name"]').fill(name)
                    form.locator('[name="cookie"]').set_input_files({'name': 'cookies.txt', 'mimeType': 'text/plain',
                        'buffer': b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tDUMMY_SECRET\n'})
                    form.locator('[type="submit"]').click()
                    expect(dialog.locator('.cookie-profile-row').filter(has_text=name)).to_be_visible()
                rows = dialog.locator('.cookie-profile-row')
                expect(rows).to_have_count(2)
                rows.filter(has_text='Account A').get_by_role('button', name='检测登录：Account A', exact=True).click()
                expect(rows.filter(has_text='Account A')).to_contain_text('登录有效')
                expect(rows.filter(has_text='Account A')).to_contain_text('检测时间：')
                expect(rows.filter(has_text='Account A')).to_contain_text('检测出口：直连')
                expect(dialog).to_contain_text('当前未选用已保存的 Cookie')
                assert probe.call_count == 1
                rows.filter(has_text='Account A').get_by_role('button', name='使用此 Cookie').click()
                expect(rows.filter(has_text='Account A')).to_contain_text('当前使用')
                expect(rows.filter(has_text='Account A').get_by_role('button', name='删除')).to_be_disabled()
                rows.filter(has_text='Account B').get_by_role('button', name='使用此 Cookie').click()
                expect(rows.filter(has_text='Account B')).to_contain_text('当前使用')
                page.once('dialog', lambda prompt: prompt.accept())
                rows.filter(has_text='Account A').get_by_role('button', name='删除').click()
                expect(rows).to_have_count(1)
                expect(dialog.locator('b')).to_have_count(0)
                form.locator('[name="name"]').fill('')
                form.locator('[name="cookie"]').set_input_files({'name': 'filename-default.txt', 'mimeType': 'text/plain',
                    'buffer': b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tDUMMY_SECRET\n'})
                form.locator('[type="submit"]').click()
                expect(rows.filter(has_text='filename-default.txt')).to_be_visible()
                expect(rows).to_have_count(2)
                expect(dialog).to_contain_text('保存在数据库')
                assert 'DUMMY_SECRET' not in dialog.inner_text()
                output = Path('.cache/cookie-profiles-preview')
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output / 'desktop.png'))
                page.set_viewport_size({'width': 390, 'height': 844})
                assert dialog.evaluate('(e) => e.scrollWidth <= e.clientWidth'), 'Mobile dialog overflows'
                page.screenshot(path=str(output / 'mobile.png'))
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
    print('Cookie UI: upload, filename default, select, login check, active guard, delete, escaping, mobile layout: OK')


if __name__ == '__main__':
    main()
