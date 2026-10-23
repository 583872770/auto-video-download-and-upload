"""Verify actual batch downloads and live per-video metrics through the web UI."""
import json
from pathlib import Path
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Response, abort
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_web import QuietHandler
from Uploaders.browser import browser_options
from Utils.runtime import ROOT


def main():
    output = ROOT / '.cache/validation' / ('downloads-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    config = output / 'config.yaml'
    config.write_text('path: videos\nurl: "npNnbj46_ik\\n2Gu7q-KUXCk"\n', encoding='utf-8')
    app = create_app(config, output / 'state')
    content = b'local batch download bytes\n' * 300000

    @app.route('/fixture/<name>.mp4', methods=['GET', 'HEAD'])
    def source(name):
        if name == 'missing':
            abort(404)

        def chunks():
            for offset in range(0, len(content), 65536):
                time.sleep(0.05)
                yield content[offset:offset + 65536]

        return Response(chunks(), mimetype='video/mp4', headers={'Content-Length': str(len(content))})

    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {}
    errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base, wait_until='networkidle')
                expect(page.locator('#download-urls')).to_have_value('npNnbj46_ik\n2Gu7q-KUXCk')
                expect(page.locator('.youtube-url-prefix')).to_have_text('https://www.youtube.com/watch?v=')
                expect(page.locator('#download-input-count')).to_contain_text('2')
                page.locator('#download-urls').fill('npNnbj46_ik\ninvalid')
                page.locator('#download-form button[type=submit]').click()
                expect(page.locator('.toast.error')).to_be_visible()
                assert not app.extensions['task_store'].pending()
                report['id_input_and_atomic_validation'] = 'passed'
                urls = '\n'.join(f'{base}/fixture/{name}.mp4' for name in ('missing', 'alpha', 'beta'))
                page.locator('#download-urls').fill(urls)
                page.locator('#download-form button[type=submit]').click()
                expect(page.locator('#batch-progress .batch-video')).to_have_count(1, timeout=30000)
                page.wait_for_function('() => [...document.querySelectorAll("#batch-progress .download-metrics")].some(row => row.textContent.includes("/s"))', timeout=30000)
                page.locator('#batch-progress-panel').scroll_into_view_if_needed()
                page.screenshot(path=str(output / 'progress-desktop.png'), full_page=True)
                tasks = page.request.get(base + '/api/tasks?scope=download').json()['items']
                live = next(task for task in tasks if task['status'] == 'running')
                assert live['progress']['downloaded_bytes'] > 0
                assert live['progress']['speed'] > 0
                assert 0 < live['progress']['percent'] < 100
                report['actual_live_metrics'] = {key: live['progress'][key] for key in ('percent', 'speed', 'eta', 'downloaded_bytes', 'total_bytes')}
                batch_id = live['batch_id']
                page.reload(wait_until='networkidle')
                expect(page.locator('#batch-progress .batch-video')).to_have_count(1)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#batch-progress-panel').scroll_into_view_if_needed()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'progress-mobile.png'), full_page=True)
                page.wait_for_function('() => { const summary = document.querySelector("#batch-summary").textContent; return summary.includes("正在下载 0") && summary.includes("排队 0"); }', timeout=45000)
                tasks = page.request.get(base + '/api/tasks?scope=download').json()['items']
                assert sum(task['status'] == 'succeeded' for task in tasks) == 2
                assert sum(task['status'] == 'failed' for task in tasks) == 1
                assert len(list((output / 'videos').rglob('*.mp4'))) == 2
                report['failure_continuation_and_reload_persistence'] = 'passed'
                page.locator('#download-urls').fill(f'{base}/fixture/alpha.mp4')
                page.locator('#download-form button[type=submit]').click()
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('.nav-item[data-view=tasks]').click()
                expect(page.locator('#all-tasks .task-row')).to_have_count(4)
                expect(page.locator('#batch-progress .batch-video')).to_have_count(0)
                expect(page.locator('#all-tasks')).to_contain_text('已下载过')
                failed = next(task for task in tasks if task['status'] == 'failed')
                page.locator(f'#all-tasks [data-task="{failed["id"]}"]').click()
                expect(page.locator('#task-download-progress .download-meter')).to_be_visible()
                expect(page.locator('#task-error')).to_be_visible()
                page.locator('#task-dialog [data-close]').click()
                report['archive_skip_finished_history_and_individual_logs'] = 'passed'
                assert not errors, errors
                report['browser_errors'] = errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        runner = app.extensions['task_runner']
        runner.stop.set()
        if runner.process:
            runner.process.wait(timeout=30)
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'BATCH_DOWNLOADS_VERIFIED: {output / "report.json"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
