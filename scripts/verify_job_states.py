"""Verify instance-scoped task badges and automatic refresh without downloads."""
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server

from tests.test_workbench import WorkbenchFleetTests
from webapp.browser import browser_options


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


def main():
    output = Path(__file__).resolve().parents[1] / '.cache' / 'validation' / 'job-states'
    output.mkdir(parents=True, exist_ok=True)
    fixture = WorkbenchFleetTests()
    fixture.setUp()
    server = make_server('127.0.0.1', 0, fixture.apps['manager'], threaded=True,
                         request_handler=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    errors = []
    try:
        job = fixture.create(urls='npNnbj46_ik\n2Gu7q-KUXCk')
        fixture.control('alpha', 'start', job)
        task = fixture.a.claim('alpha-owner')
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, **browser_options(p.chromium))
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/jobs')
            expect(page.locator('#task-table .badge')).to_have_text('执行中')
            page.locator(f'.open-task[data-open="{job}"]').click()
            badge = page.locator('#detail-heading .badge')
            expect(badge).to_have_text('执行中')
            page.screenshot(path=str(output / 'running.png'), full_page=True)
            fixture.control('alpha', 'pause')
            expect(badge).to_have_text('暂停中', timeout=15000)
            page.screenshot(path=str(output / 'draining.png'), full_page=True)
            assert fixture.a.finish(task['id'], result={}, worker_id='alpha-owner')
            expect(badge).to_have_text('待开始 / 已暂停', timeout=15000)
            fixture.control('alpha', 'start', job)
            task = fixture.a.claim('alpha-next')
            fixture.control('beta', 'start', job)
            fixture.control('alpha', 'pause')
            expect(badge).to_have_text('执行中', timeout=15000)
            assert fixture.status('alpha')['job']['state'] == 'draining'
            assert fixture.a.finish(task['id'], result={}, worker_id='alpha-next')
            expect(badge).to_have_text('已完成', timeout=15000)
            page.screenshot(path=str(output / 'completed.png'), full_page=True)
            page.locator('#back').click()
            expect(page.locator('#task-table .badge')).to_have_text('已完成')
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        fixture.doCleanups()
    print('PASS: scoped running, draining, paused, shared-job execution, completion, list/detail badges and automatic refresh.')
    print(output)


if __name__ == '__main__':
    main()
