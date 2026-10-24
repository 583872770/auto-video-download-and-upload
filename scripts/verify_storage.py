"""Exercise storage selection and an actual disposable local download in the UI."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import send_file
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_web import QuietHandler
from webapp.browser import browser_options
from Utils.runtime import ROOT


def main():
    output = ROOT / '.cache/validation' / ('storage-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    config = output / 'config.yaml'
    config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\n', encoding='utf-8')
    fixture = output / 'source.mp4'
    fixture.write_bytes(b'local disposable video fixture\n' * 1000)
    app = create_app(config, output / 'state', start_worker=False)
    app.add_url_rule('/fixture.mp4', view_func=lambda: send_file(fixture, mimetype='video/mp4'))
    store = app.extensions['task_store']
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
                page.goto(base + '/#settings', wait_until='networkidle')
                expect(page.locator('#storage-mode option')).to_have_count(3)
                expect(page.locator('#storage-mode option[value=cloud]')).to_have_attribute('disabled', '')
                expect(page.locator('#storage-mode')).to_have_value('local')
                expect(page.locator('[data-local-storage]').first).to_be_visible()
                page.locator('#storage-mode').select_option('test')
                expect(page.locator('[data-local-storage]').first).not_to_be_visible()
                expect(page.locator('#storage-mode-note')).to_contain_text('真实下载后自动删除')
                page.locator('#settings-form button[type=submit]').click()
                expect(page.locator('#download-storage-label')).to_have_text('保存位置 · 测试模式')
                page.reload(wait_until='networkidle')
                expect(page.locator('#storage-mode')).to_have_value('test')
                page.screenshot(path=str(output / 'settings-desktop.png'), full_page=True)
                page.locator('.nav-item[data-view=overview]').click()
                expect(page.locator('#download-directory')).to_contain_text('下载完成后自动删除')
                expect(page.locator('#download-final-step')).to_have_text('完成后删除')
                page.locator('#download-urls').fill(base + '/fixture.mp4')
                page.locator('#download-form button[type=submit]').click()
                expect(page.locator('#stat-active')).to_have_text('1')
                task = store.page()['items'][0]
                result = subprocess.run([sys.executable, 'web_worker.py', '--state', str(store.directory)],
                                        cwd=ROOT, capture_output=True, timeout=45)
                assert result.returncode == 0
                expect(page.locator('#recent-tasks .task-row').first).to_contain_text('测试完成 · 文件已删除', timeout=15000)
                page.locator('#recent-tasks .task-row').first.click()
                expect(page.locator('#task-result')).to_contain_text('本次视频、封面、字幕及元数据已删除')
                page.locator('#task-dialog [data-close]').click()
                assert store.get(task['id'])['result']['files_deleted']
                assert not (store.directory / 'test-downloads' / task['id']).exists()
                assert not (output / 'videos').exists()
                page.screenshot(path=str(output / 'completed-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'completed-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('.nav-item[data-view=settings]').click()
                page.locator('#storage-mode').select_option('local')
                expect(page.locator('[data-local-storage]').first).to_be_visible()
                page.locator('#settings-form input[name=path]').fill('saved-videos')
                page.locator('#settings-form button[type=submit]').click()
                expect(page.locator('#download-storage-label')).to_have_text('保存位置 · 指定目录')
                expect(page.locator('#download-directory')).to_contain_text('saved-videos')
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
    report = {'three_storage_options': 'passed', 'cloud_unavailable': 'passed', 'test_mode_persisted': 'passed',
              'real_local_download_deleted': 'passed', 'task_history_retained': 'passed',
              'local_directory_roundtrip': 'passed', 'mobile_layout': 'passed', 'browser_errors': errors}
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('STORAGE_UI_VERIFIED:', output / 'report.json')


if __name__ == '__main__':
    main()

