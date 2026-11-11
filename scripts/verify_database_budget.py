"""Verify budget controls, persisted drafts and live explanations in Chromium."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from app import create_app
from scripts.settings_roundtrip_checks import reveal_setting
from scripts.verify_jobs import QuietHandler
from webapp.browser import browser_options
from webapp.concurrency_diagnostics import diagnose
from webapp.throughput import MIB, ThroughputController


def main():
    output = Path(__file__).resolve().parents[1] / '.cache/validation/database-budget'
    output.mkdir(parents=True, exist_ok=True)
    environment = {key:value for key,value in os.environ.items()
                   if key not in ('VIDEO_DB_CONNECTION_BUDGET', 'VIDEO_WORKER_DB_CONNECTION_BUDGET')}
    with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, environment, clear=True), ExitStack() as stack:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nworker_count: 21\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid',
                         instance_id='budget-test', database_url='sqlite:///' + (root / 'test.db').as_posix())
        stack.callback(app.extensions['task_store'].db.engine.dispose)
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def shutdown():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        stack.callback(shutdown)
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            page = browser.new_page(viewport={'width': 1440, 'height': 1050})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/#settings')
            form = page.locator('#settings-form')
            mode = reveal_setting(page, form.locator('[name=db_budget_mode]'))
            manual = form.locator('[name=db_connection_budget]')
            preview = page.locator('#database-budget-preview')
            expect(mode).to_have_value('auto')
            expect(manual).to_have_js_property('readOnly', True)
            expect(preview).to_contain_text('需要 53 个')
            expect(preview).to_contain_text('最多支持 21 个下载进程')
            mode.select_option('manual')
            expect(manual).to_have_js_property('readOnly', False)
            manual.fill('48')
            expect(preview).to_contain_text('最多支持 18 个下载进程')
            expect(preview).to_have_class('settings-category-note capacity-draft-warning')
            def save():
                with page.expect_response(lambda r: r.request.method == 'PUT' and r.url.endswith('/api/config')) as response:
                    form.locator('button[type=submit]').click()
                expect(form.locator('button[type=submit]')).to_be_enabled()
                return response.value
            assert save().status == 200
            expect(page.locator('#settings-save-state')).to_contain_text('系统设置已保存')
            page.reload()
            expect(mode).to_have_value('manual')
            expect(manual).to_have_value('48')
            manual = reveal_setting(page, manual)
            expect(preview).to_contain_text('最多支持 18 个下载进程')
            page.locator('#database-budget-settings').screenshot(path=str(output / 'manual.png'))
            manual.fill('8')
            assert save().status == 400
            expect(page.locator('#settings-save-state')).to_contain_text('至少需要')
            expect(manual).to_have_value('8')
            assert app.test_client().get('/api/config').json['config']['db_connection_budget'] == 48
            mode.select_option('auto')
            assert save().status == 200
            expect(page.locator('#settings-save-state')).to_contain_text('系统设置已保存')
            page.reload()
            expect(mode).to_have_value('auto')
            reveal_setting(page, mode)
            expect(preview).to_contain_text('最多支持 21 个下载进程')
            page.locator('#database-budget-settings').screenshot(path=str(output / 'auto.png'))
            # Exercise the live explanation using the actual controller and diagnostics.
            pool_config = dict(download_methods=['yt_dlp'], youtube_cookie_pool=['a'] * 21,
                               youtube_cookie_pool_worker_limit=21, builtin_transfer_limit=21,
                               db_budget_mode='manual', db_connection_budget=48)
            controller = ThroughputController(root / 'metrics')
            sample = dict(cpu_cores=4, memory_total=16384*MIB, memory_available=10000*MIB,
                          cpu_percent=30, io_pressure=0, rx=0)
            with patch.object(controller, 'sample', return_value=sample):
                rate = controller.update(pool_config, cookie_capacity=dict(known=True, capacity=21, reason='ready'))
            diagnostic = diagnose(pool_config, 'budget-test', [], rate, ({}, {}, {}), time.time())
            rendered = page.evaluate('(data) => workerControlHTML(data)', diagnostic)
            assert '数据库连接预算' in rendered and '最多支持 18 个下载进程' in rendered
            assert '需要 53 个' in rendered and 'NaN' not in rendered and 'undefined' not in rendered
            mode.select_option('manual')
            manual.fill('48')
            page.locator('#database-budget-settings').evaluate('(el, html) => el.insertAdjacentHTML("beforeend", html)', rendered)
            page.locator('#database-budget-settings').screenshot(path=str(output / 'live.png'))
            page.set_viewport_size({'width':390, 'height':844})
            page.locator('#database-budget-settings').screenshot(path=str(output / 'mobile.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'horizontal overflow'
            assert not errors, errors
            browser.close()
    print('PASS: automatic/manual budgets, validation, persistence, live causes and narrow layout.')


if __name__ == '__main__':
    main()
