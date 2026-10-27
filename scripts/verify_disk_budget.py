"""Verify disk settings, cleanup and mobile layout against a temporary instance."""
import os
import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler
from app import create_app
from webapp.browser import browser_options
from webapp.yt5s_browser import NativeBrowser
from webapp.diagnostics import runtime_log


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


def main():
    output = Path(__file__).resolve().parents[1] / '.cache/validation/disk-budget'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False)
        store = app.extensions['task_store']
        runtime_log(store.directory).write(b'fixture worker startup diagnostic\n')
        task = store.create('check', {'config':{}, 'base':str(root)})
        store.finish(task['id'], result={'message':'fixture'})
        log = store.log_path(task['id'])
        log.write_bytes(b'old diagnostic fixture')
        os.utime(log, (time.time() - 48 * 3600,) * 2)
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, **browser_options(playwright.chromium))
                page = browser.new_page(viewport={'width':1440,'height':1050})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' and 'Content Security Policy' in msg.text else None)
                page.goto(f'http://127.0.0.1:{server.server_port}/#settings')
                expect(page.locator('input[name=disk_budget_gb]')).to_have_value('20')
                expect(page.locator('#disk-summary')).to_contain_text('磁盘剩余')
                expect(page.locator('input[name=disk_log_budget_mb]')).to_have_value('64')
                page.locator('#instance-logs-open').click()
                expect(page.locator('#instance-logs-dialog')).to_be_visible()
                expect(page.locator('#instance-logs-content')).to_contain_text('fixture worker startup diagnostic')
                page.locator('#instance-logs-close').click()
                page.locator('input[name=disk_budget_gb]').fill('22')
                page.get_by_role('button', name='保存设置', exact=True).click()
                expect(page.locator('#settings-save-state')).to_contain_text('设置已保存')
                assert store.runtime()['disk_budget_gb'] == 22
                page.locator('#disk-cleanup').click()
                expect(page.locator('#disk-cleanup-result')).to_contain_text('统计与素材保留')
                assert not log.exists()
                assert store.get(task['id'])['status'] == 'succeeded'
                page.locator('#disk-panel').screenshot(path=str(output / 'desktop.png'))
                page.set_viewport_size({'width':390,'height':844})
                page.locator('#instance-logs-open').click()
                expect(page.locator('#instance-logs-dialog')).to_be_visible()
                assert page.locator('#instance-logs-dialog').evaluate('(el) => el.getBoundingClientRect().right <= innerWidth')
                page.locator('#instance-logs-close').click()
                page.locator('#disk-panel').scroll_into_view_if_needed()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.locator('#disk-panel').screenshot(path=str(output / 'mobile.png'))
                assert not errors, errors
                browser.close()
                executable = browser_options(playwright.chromium).get('executable_path', playwright.chromium.executable_path)
                native = NativeBrowser(store.directory, {'disk_min_free_gb':0,'disk_resume_free_gb':0})
                try:
                    with patch.dict(os.environ, {'YT5S_PROBE_CONTAINER':'1'}):
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            pool.submit(lambda: asyncio.run(native.start(SimpleNamespace(
                                browser_executable=executable, proxy='', headless=True)))).result(timeout=30)
                    profile = Path(native.profile.name)
                    assert profile.is_relative_to(store.directory / 'runtime/browser-profiles')
                finally:
                    native.close()
                assert not profile.exists()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()
    print('PASS: disk budget settings, saved configuration, safe cleanup, retained task, desktop/mobile, CSP and JavaScript.')
    print(output)


if __name__ == '__main__':
    main()
