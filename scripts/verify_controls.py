"""Exercise stop, reload and real byte-range resumption through the browser."""
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Response, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from app import create_app
from scripts.verify_web import QuietHandler
from Uploaders.browser import browser_options
from Utils.runtime import ROOT


def main():
    output = ROOT / '.cache/validation' / ('controls-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    config = output / 'config.yaml'
    config.write_text('path: videos\nproxy_mode: direct\nworker_count: 2\nmin_free_gb: 0\n', encoding='utf-8')
    app = create_app(config, output / 'state')
    content = b'local stop-resume fixture\n' * 350000
    ranges, errors = [], []

    @app.route('/fixture/<name>.mp4', methods=['GET', 'HEAD'])
    def fixture(name):
        requested = request.headers.get('Range', '')
        start = int(re.match(r'bytes=(\d+)-', requested)[1]) if requested else 0
        if start:
            ranges.append(start)

        def chunks():
            for offset in range(start, len(content), 65536):
                time.sleep(0.035)
                yield content[offset:offset + 65536]

        headers = {'Content-Length': str(len(content) - start), 'Accept-Ranges': 'bytes'}
        if start:
            headers['Content-Range'] = f'bytes {start}-{len(content)-1}/{len(content)}'
        return Response(chunks(), status=206 if start else 200, mimetype='video/mp4', headers=headers)

    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base, wait_until='networkidle')
                expect(page.locator('#download-queue-toggle')).to_be_disabled()
                page.locator('#download-urls').fill('\n'.join(f'{base}/fixture/video{i}.mp4' for i in range(4)))
                page.locator('#download-form button[type=submit]').click()
                expect(page.locator('#batch-progress .batch-video')).to_have_count(2)
                expect(page.locator('#all-tasks .task-row')).to_have_count(0)
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    tasks = page.request.get(base + '/api/tasks?scope=download').json()['items']
                    if sum(t['status'] == 'running' and (t['progress'] or {}).get('downloaded_bytes', 0) > 0 for t in tasks) == 2:
                        break
                    page.wait_for_timeout(100)
                else:
                    raise AssertionError('Both download processes did not start')
                expect(page.locator('#download-queue-toggle')).to_be_enabled()
                page.locator('#download-queue-toggle').click()
                expect(page.locator('#download-queue-toggle')).to_have_text('继续下载', timeout=20000)
                expect(page.locator('#download-queue-toggle')).to_be_enabled()
                expect(page.locator('#download-queue-status')).to_contain_text('下载已停止')
                expect(page.locator('#batch-progress .batch-video')).to_have_count(0)
                expect(page.locator('#all-tasks .task-row')).to_have_count(0)
                queue = page.request.get(base + '/api/status').json()['download_queue']
                assert queue == {'paused': True, 'queued': 4, 'running': 0}, queue
                files = list((output / 'videos').rglob('*.part'))
                assert files and all(file.stat().st_size > 0 for file in files)
                saved = {str(file): file.stat().st_size for file in files}
                page.reload(wait_until='networkidle')
                expect(page.locator('#download-queue-toggle')).to_have_text('继续下载')
                assert {str(file): file.stat().st_size for file in files} == saved
                page.screenshot(path=str(output / 'stopped-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'stopped-mobile.png'), full_page=True)
                page.locator('#download-queue-toggle').click()
                expect(page.locator('#download-queue-toggle')).to_have_text('停止全部下载')
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    tasks = page.request.get(base + '/api/tasks?scope=download').json()['items']
                    if len(tasks) == 4 and all(task['status'] == 'succeeded' for task in tasks):
                        break
                    page.wait_for_timeout(300)
                else:
                    raise AssertionError('Resumed downloads did not complete')
                expect(page.locator('#batch-progress .batch-video')).to_have_count(0)
                expect(page.locator('#all-tasks .task-row')).to_have_count(4)
                expect(page.locator('#nav-task-count')).to_have_text('4')
                actual = list((output / 'videos').rglob('*.mp4'))
                assert len(actual) == 4 and all(file.read_bytes() == content for file in actual)
                assert ranges, 'No saved byte-range was resumed'
                assert all(task['attempts'] == 1 for task in tasks)
                assert not errors, errors
                report = {'stop_running_and_queued': 'passed', 'reload_preserves_stop': 'passed',
                          'two_download_processes': 'passed', 'byte_range_resume': ranges,
                          'complete_files_verified': len(actual), 'manual_stop_uses_no_retry': 'passed', 'browser_errors': errors, 'live_only_progress_and_finished_only_history': 'passed'}
            finally:
                browser.close()
    finally:
        runner = app.extensions['task_runner']
        runner.stop.set()
        from webapp.control import stop_downloads
        store = app.extensions['task_store']
        store.set_downloads_paused(True)
        stop_downloads(store)
        for child in runner.processes.values():
            child.wait(timeout=15)
        server.shutdown()
        server.server_close()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('DOWNLOAD_CONTROLS_VERIFIED:', output / 'report.json')


if __name__ == '__main__':
    main()
