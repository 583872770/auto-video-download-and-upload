"""Deleted jobs disappear from an open workbench and stale preview links."""
import json
from pathlib import Path
import re
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

from tests.test_workbench import StandaloneWorkbenchTests
from webapp.browser import browser_options


class QuietHandler(WSGIRequestHandler):
    def log(self, kind, message, *args):
        pass


def main():
    fixture = StandaloneWorkbenchTests()
    fixture.setUp()
    server = None
    try:
        first = fixture.create('Delete this job', urls='npNnbj46_ik\n2Gu7q-KUXCk')
        second = fixture.create('Delete the other job', urls='WMu6ANTg8mk')
        with patch('app.require_downmp3_environment'):
            response = fixture.client.post('/api/jobs/workbench', json={
                'action': 'start', 'job_id': first}, headers=fixture.headers)
        assert response.status_code == 200, response.json
        task = fixture.store.claim(fixture.store.owner)
        assert task and task['batch_id'] == first
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/workbench?job=' + first)
            expect(page.locator('#download-job')).to_have_value(first)
            expect(page.locator('#download-job-note')).to_contain_text('Delete this job')
            expect(page.locator('#workbench-tasks button')).to_have_count(1)

            # Simulate deletion from another task-center tab, without reloading this one.
            for identity, name in ((first, 'Delete this job'), (second, 'Delete the other job')):
                response = fixture.client.post(f'/api/jobs/{identity}/control', json={
                    'action': 'delete', 'confirmation': name}, headers=fixture.headers)
                assert response.status_code == 200, response.json
            expect(page.locator('#download-job')).to_have_value('', timeout=15000)
            expect(page.locator('#download-job option')).to_have_count(1)
            expect(page.locator('#download-job-note')).to_contain_text('请选择任务')
            expect(page.locator('#download-queue-start')).to_be_disabled()
            expect(page.locator('#workbench-tasks button')).to_have_count(0)
            expect(page).not_to_have_url(re.compile(r'[?&]job='))

            # An old bookmark must also recover instead of showing a deleted ID forever.
            page.goto(base + '/workbench?job=' + first)
            expect(page.locator('#download-job-note')).to_contain_text('请选择任务')
            expect(page.locator('#download-job option')).to_have_count(1)
            expect(page.locator('#download-job-detail')).to_have_count(0)
            expect(page.locator('#download-queue-start')).to_be_disabled()

            # Returning from another tab must also refresh an unselected dropdown.
            third = fixture.create('Unselected job', urls='')
            page.reload()
            expect(page.locator('#download-job option')).to_have_count(2)
            response = fixture.client.post(f'/api/jobs/{third}/control', json={
                'action': 'delete', 'confirmation': 'Unselected job'}, headers=fixture.headers)
            assert response.status_code == 200, response.json
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(page.locator('#download-job option')).to_have_count(1)
            assert not errors, errors
            browser.close()
        print(json.dumps({'ok': True, 'open_page_updated': True, 'stale_link_cleared': True,
                          'browser_errors': errors}))
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
