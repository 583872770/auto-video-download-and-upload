"""Verify the job center against a temporary database, without real downloads."""
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

from app import create_app
from webapp.browser import browser_options


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


def main():
    output = Path(__file__).resolve().parents[1] / '.cache' / 'validation' / 'jobs'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\ndownload_methods: [downmp3]\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False)
        store = app.extensions['task_store']
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        errors = []
        try:
            with patch('app.require_downmp3_environment'), sync_playwright() as p:
                browser = p.chromium.launch(headless=True, **browser_options(p.chromium))
                page = browser.new_page(viewport={'width': 1440, 'height': 1050}, reduced_motion='reduce')
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('console', lambda e: errors.append(e.text) if e.type == 'error' and 'Content Security Policy' in e.text else None)
                page.goto(base + '/jobs')
                page.locator('#new-task').click()
                page.locator('#editor input[name=customer]').fill('Customer A')
                page.locator('#editor input[name=name]').fill('Order A')
                page.locator('#editor input[name=price]').fill('1000000')
                page.locator('#editor textarea').fill('npNnbj46_ik')
                page.locator('#editor button[type=submit]').click()
                expect(page.locator('#detail-heading h1')).to_have_text('Order A')
                a = page.url.split('#')[1]
                page.locator('[data-tab=videos]').click()
                expect(page.locator('#detail-content tbody tr')).to_have_count(1)
                page.locator('[data-action=append]').click()
                page.locator('#editor textarea').fill('npNnbj46_ik\n2Gu7q-KUXCk')
                page.locator('#editor button[type=submit]').click()
                expect(page.locator('#detail-content tbody tr')).to_have_count(2)
                page.locator('[data-action=start]').click()
                expect(page.locator('#detail-heading .badge')).to_have_text('排队中')
                task = store.claim(store.owner)
                fixture = root / 'fixture.mp4'
                fixture.write_bytes(b'x' * 1000)
                assert store.statistics.record_file(task['id'], fixture)
                store.finish(task['id'], result={}, worker_id=store.owner)
                page.locator('[data-tab=billing]').click()
                expect(page.locator('#detail-content')).to_contain_text('¥ 1.00')
                page.locator('[data-tab=overview]').click()
                expect(page.locator('#detail-content')).to_contain_text('视频处理进度')
                page.screenshot(path=str(output / 'overview-desktop.png'), full_page=True)
                page.locator('#back').click()
                page.locator('#new-task').click()
                page.locator('#editor input[name=customer]').fill('Customer B')
                page.locator('#editor input[name=name]').fill('Order B')
                page.locator('#editor textarea').fill('npNnbj46_ik')
                page.locator('#editor button[type=submit]').click()
                expect(page.locator('#detail-heading h1')).to_have_text('Order B')
                b = page.url.split('#')[1]
                page.locator('[data-tab=videos]').click()
                expect(page.locator('#detail-content tbody tr')).to_have_count(1)
                page.locator('[data-action=clear]').click()
                page.locator('#clear-form input').fill('Order B')
                page.locator('#clear-form button[type=submit]').click()
                expect(page.locator('#detail-content tbody')).to_contain_text('没有符合条件的视频')
                page.locator('[data-action=archive]').click()
                expect(page.locator('#detail-heading .badge')).to_have_text('已归档')
                page.locator('[data-action=unarchive]').click()
                expect(page.locator('#detail-heading .badge')).to_have_text('空任务')
                page.locator('[data-action=delete]').click()
                page.locator('#clear-form input').fill('Order B')
                page.locator('#clear-form button[type=submit]').click()
                expect(page.locator('#center-view')).to_be_visible()
                expect(page.locator('#task-table')).not_to_contain_text('Order B')
                page.goto(base + '/jobs#' + a)
                expect(page.locator('#detail-heading h1')).to_have_text('Order A')
                page.locator('[data-tab=billing]').click()
                expect(page.locator('#detail-content')).to_contain_text('¥ 1.00')
                page.locator('[data-tab=settings]').click()
                page.locator('#settings-form input[name=price]').fill('500000')
                page.locator('#settings-form button[type=submit]').click()
                expect(page.locator('#toast')).to_contain_text('已更新')
                page.locator('[data-tab=billing]').click()
                expect(page.locator('#detail-content')).to_contain_text('¥ 0.50')
                # Two editors must not silently replace each other's prices.
                page.locator('[data-tab=settings]').click()
                stale = browser.new_page(viewport={'width':1440,'height':1050})
                stale.goto(base + '/jobs#' + a)
                stale.locator('[data-tab=settings]').click()
                expect(stale.locator('#settings-form input[name=price]')).to_have_value('500000')
                page.locator('#settings-form input[name=price]').fill('700000')
                with page.expect_response(lambda response: response.request.method == 'PUT' and response.url == base + '/api/jobs/' + a) as saved:
                    page.locator('#settings-form button[type=submit]').click()
                assert saved.value.status == 200
                expect(page.locator('#toast')).to_contain_text('已更新')
                stale.locator('#settings-form input[name=name]').fill('Merged order')
                stale.locator('#settings-form button[type=submit]').click()
                expect(stale.locator('#settings-error')).to_contain_text('其他页面修改')
                expect(stale.locator('#settings-form input[name=price]')).to_have_value('500000')
                expect(stale.locator('[data-conflict]')).to_contain_text('700000')
                stale.screenshot(path=str(output / 'edit-conflict.png'),full_page=True)
                stale.locator('#settings-form input[name=price]').fill('700000')
                stale.locator('[data-conflict] button').click()
                stale.locator('#settings-form button[type=submit]').click()
                expect(stale.locator('#detail-heading h1')).to_have_text('Merged order')
                stale.close()
                page.goto(base + '/#statistics')
                expect(page.locator('#statistics-totals')).to_contain_text('¥ 0.70')
                expect(page.locator('#statistics-sessions')).to_contain_text('按客户任务计费')
                expect(page.locator('#customer-lists [data-list-form]')).to_have_count(0)
                page.screenshot(path=str(output / 'unified-statistics.png'),full_page=True)
                page.goto(base + '/jobs#' + a)
                page.locator('[data-tab=overview]').click()
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path=str(output / 'overview-mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.set_viewport_size({'width':1440,'height':1050})
                page.locator('.console-identity').click()
                page.locator('[name=bootstrap_key]').fill(app.extensions['console_auth'].bootstrap_key)
                page.locator('#login-form [name=username]').fill('admin')
                page.locator('#login-form [name=password]').fill('secure-browser-test')
                page.locator('#login-form button').click()
                expect(page.locator('#center-view')).to_be_visible()
                page.locator('.console-identity').click()
                page.locator('#create-account [name=username]').fill('reader')
                page.locator('#create-account [name=password]').fill('secure-browser-test')
                page.locator('#create-account button').click()
                expect(page.locator('#accounts')).to_contain_text('reader')
                anonymous = browser.new_context(viewport={'width':390,'height':844})
                login = anonymous.new_page()
                login.goto(base + '/jobs')
                expect(login.locator('h1')).to_have_text('登录管理账户')
                login.locator('[name=username]').fill('reader')
                login.locator('[name=password]').fill('wrong-password')
                login.locator('#login-form button').click()
                expect(login.locator('#access-message')).to_contain_text('用户名或密码')
                login.locator('[name=password]').fill('secure-browser-test')
                login.locator('#login-form button').click()
                expect(login.locator('#new-task')).to_be_disabled()
                expect(login.locator('#task-table')).to_contain_text('Merged order')
                login.screenshot(path=str(output / 'viewer-mobile.png'),full_page=True)
                login.locator('.console-identity').click()
                login.locator('#logout').click()
                expect(login.locator('#login-form')).to_be_visible()
                anonymous.close()
                assert a != b
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()
    print('PASS: task lifecycle, unified bills, two-editor conflict recovery, administrator setup, viewer login/permissions/logout, desktop/mobile, CSP and JavaScript.')
    print(output)


if __name__ == '__main__':
    main()
