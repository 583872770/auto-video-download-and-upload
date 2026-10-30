"""Verify paused queues can edit rotation without starting a download."""
import json
from pathlib import Path
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_web import QuietHandler
from webapp.browser import browser_options
from Utils.runtime import ROOT


def main():
    output = ROOT / '.cache/validation' / ('rotation-unlock-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    config = output / 'config.yaml'
    config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
    app = create_app(config, output / 'state', start_worker=False)
    store = app.extensions['task_store']
    manager = app.extensions['clash']
    (manager.directory / 'nodes.yaml').write_text(
        'proxies:\n  - {name: A, type: http, server: 127.0.0.1, port: 1234}\n'
        '  - {name: B, type: http, server: 127.0.0.1, port: 1235}\n', encoding='utf-8')
    task = store.create('download', {'config': {}, 'base': str(output),
                                     'urls': ['https://example.test/fixture.mp4']})
    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base + '/#clash', wait_until='networkidle')
                expect(page.locator('#rotation-enabled')).to_be_disabled()
                expect(page.locator('#rotation-draft-note')).to_contain_text('请先停止全部下载')
                # Seed a paused queue; this check exercises proxy controls,
                # independently of the workbench's task selection controls.
                store.set_downloads_paused(True)
                page.evaluate('refresh()')
                expect(page.locator('#rotation-enabled')).to_be_enabled()
                page.locator('#rotation-enabled').check()
                page.locator('[data-rotation-node=A]').check()
                page.locator('[data-rotation-node=B]').check()
                page.locator('#rotation-every').fill('4')
                page.locator('#rotation-save').click()
                expect(page.locator('#rotation-draft-note')).to_have_text('保存后对待下载任务生效')
                assert manager.status()['rotation']['enabled']
                assert manager.status()['rotation']['every'] == 4
                queue = store.download_queue()
                assert queue['paused'] and queue['queued'] == 1 and queue['running'] == 0
                assert store.get(task['id'])['attempts'] == 0
                page.reload(wait_until='networkidle')
                expect(page.locator('#rotation-enabled')).to_be_checked()
                expect(page.locator('#rotation-every')).to_have_value('4')
                page.screenshot(path=str(output / 'desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'mobile.png'), full_page=True)
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
    report = {'running_queue_locked': 'passed', 'paused_queue_edit_and_save': 'passed',
              'queue_preserved': 'passed', 'saved_after_reload': 'passed',
              'mobile_layout': 'passed', 'browser_errors': errors}
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('ROTATION_UNLOCK_VERIFIED:', output / 'report.json')


if __name__ == '__main__':
    main()

