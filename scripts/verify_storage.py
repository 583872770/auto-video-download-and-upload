"""Verify current storage settings UI, then isolated backend download/cleanup contracts."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_jobs import QuietHandler
from webapp.browser import browser_options


def main():
    output = Path(__file__).resolve().parents[1] / '.cache/validation/storage'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False)
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{server.server_port}/#settings')
                    expect(page.locator('#storage-mode option')).to_have_count(2)
                    expect(page.locator('#storage-mode option[value=cloud]')).to_have_count(0)
                    expect(page.locator('#storage-mode')).to_have_value('local')
                    expect(page.locator('[data-local-storage]').first).to_be_visible()
                    page.locator('#storage-mode').select_option('test')
                    expect(page.locator('[data-local-storage]').first).not_to_be_visible()
                    expect(page.locator('#storage-mode-note')).to_contain_text('真实下载后自动删除')
                    page.locator('#settings-form button[type=submit]').click()
                    expect(page.locator('#settings-save-state')).to_contain_text('已保存')
                    page.reload()
                    expect(page.locator('#storage-mode')).to_have_value('test')
                    page.screenshot(path=str(output / 'test-desktop.png'), full_page=True)
                    page.locator('#storage-mode').select_option('local')
                    page.locator('#settings-form input[name=path]').fill('saved-videos')
                    page.locator('#settings-form button[type=submit]').click()
                    expect(page.locator('#settings-save-state')).to_contain_text('已保存')
                    page.reload()
                    expect(page.locator('#storage-mode')).to_have_value('local')
                    expect(page.locator('#settings-form input[name=path]')).to_have_value('saved-videos')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(output / 'local-mobile.png'), full_page=True)
                    assert not errors, errors
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            app.extensions['task_store'].db.engine.dispose()
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        'tests.test_storage.SQLiteStorageTests', 'tests.test_storage.WorkspaceTests'])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1
    print('PASS: storage UI save/reload, supported modes and mobile layout; backend download, accounting and cleanup contracts.')
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
