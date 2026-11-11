"""Check workbench layout, polling and stable account nodes in an isolated app."""
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify.verify_concurrency_filters_ui import fixture
from scripts.verify.verify_cookie_runtime_ui import QuietHandler


def main():
    channel = os.environ.get('VIDEO_UI_BROWSER_CHANNEL')
    output = Path('.runtime/cache-archive-20261111/workbench-performance-preview').resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='workbench-performance-') as temporary, patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid', instance_id='preview',
                         database_url='sqlite:///' + (root / 'fixture.sqlite3').as_posix())
        store = app.extensions['task_store']
        store.fleet.beat()
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = fixture()
        for index in range(15, 18):
            account = deepcopy(runtime['cookie_allocation']['accounts'][5])
            account.update(id=f'cookie-{index}', name=f'account{index + 1:02d}@example.test')
            runtime['cookie_allocation']['accounts'].append(account)
        job_id = 'a' * 32
        job = dict(id=job_id, customer='示例客户', name='YouTube 下载任务', queued=420, total=500,
                   running=4, succeeded=72, failed=4, file_count=72, importing=0, archived=False,
                   released=True, desired_running=True, state='running', waiting_reason='')
        selected = dict(paused=False, stop_requested=False)
        requests, errors, actions = [], [], []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, channel=channel)
                page = browser.new_page(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(request.url.split('/api/', 1)[-1].split('?', 1)[0]) if '/api/' in request.url else None)

                def diagnostic(route):
                    response = route.fetch()
                    body = response.json()
                    for item in body.get('items', []):
                        if item['id'] == 'preview':
                            item['concurrency'] = {**deepcopy(runtime), 'time': time.time()}
                    headers = {key: value for key, value in response.headers.items() if key not in ('content-encoding', 'content-length')}
                    route.fulfill(status=response.status, headers=headers, json=body)

                def workbench(route):
                    assert 'compact=1' in route.request.url
                    if route.request.method == 'POST':
                        action = route.request.post_data_json['action']
                        actions.append(action)
                        selected.update(paused=action != 'start', stop_requested=action == 'stop')
                        job['released'] = action == 'start'
                    route.fulfill(json=dict(instance_id='preview', selected_job_id=job_id, job=job,
                        online=True, running=4, queued=420, error='', session=None, **selected,
                        transfer_summary=dict(time=time.time(), transferring=4, measured=4, speed=32 * 1024**2)))

                page.route('**/api/instances*', diagnostic)
                page.route('**/api/jobs/workbench*', workbench)
                page.route('**/api/tasks/task-0-0', lambda route: route.fulfill(json=dict(
                    id='task-0-0', action='download', status='running', created_at=None,
                    progress=dict(stage='downloading', title='Inspector video', speed=1024**2),
                    error='', result=None)))
                page.route('**/api/tasks/task-0-0/logs*', lambda route: route.fulfill(
                    json=dict(text='Inspector log entry', offset=19)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#overview')
                panel = page.locator('[data-concurrency-panel=overview]')
                expect(panel.locator('.cookie-runtime-card')).to_have_count(18)
                expect(page.locator('#download-job-summary')).to_contain_text('任务处理成功72')
                expect(page.locator('#stat-speed')).to_have_text('32.0')
                expect(page.locator('#batch-progress-panel, #batch-progress, #download-queue-toggle')).to_have_count(0)
                assert page.locator('#download-console-title').bounding_box()['y'] < panel.bounding_box()['y']
                expect(page.locator('#download-job')).to_have_value(job_id)

                # Refreshing one account must preserve the other cards and controls.
                page.evaluate('''() => {
                    window.savedAccount = document.querySelector('[data-cookie-id="cookie-0"]');
                    window.savedTask = savedAccount.querySelector('[data-task]');
                    window.savedSelect = document.querySelector('[data-cookie-account-filter]');
                    window.savedOption = document.querySelector('#download-job').options[1];
                    window.removedAccounts = 0;
                    new MutationObserver(changes => changes.forEach(change => change.removedNodes.forEach(node => {
                        if (node.nodeType === 1 && (node.matches('.cookie-runtime-card') || node.querySelector('.cookie-runtime-card'))) removedAccounts++;
                    }))).observe(document.querySelector('[data-concurrency-panel=overview]'), {subtree:true, childList:true});
                }''')
                selector = panel.locator('[data-cookie-account-filter]')
                selector.focus()
                runtime['cookie_allocation']['accounts'][0]['tasks'][0]['speed'] = 7 * 1024**2
                page.evaluate('loadFleet()')
                expect(panel.locator('[data-cookie-id="cookie-0"]')).to_contain_text('7.0 MB/s')
                expect(selector).to_be_focused()
                assert page.evaluate('''() => savedAccount === document.querySelector('[data-cookie-id="cookie-0"]')
                    && savedTask === savedAccount.querySelector('[data-task]') && savedAccount.isConnected
                    && savedSelect === document.querySelector('[data-cookie-account-filter]') && removedAccounts === 0''')

                panel.locator('[data-cookie-task-filter=transferring]').click()
                expect(panel.locator('.cookie-runtime-card')).to_have_count(3)
                panel.locator('[data-cookie-filters-clear]').click()
                expect(panel.locator('.cookie-runtime-card')).to_have_count(18)
                panel.locator('[data-task="task-0-0"]').click()
                expect(page.locator('#task-dialog')).to_be_visible()
                expect(page.locator('#task-download-progress progress')).to_be_visible()
                page.locator('.task-technical summary').click()
                expect(page.locator('#task-log')).to_contain_text('Inspector log entry')
                page.locator('#task-dialog [data-close]').click()
                expect(page.locator('#task-dialog')).not_to_be_visible()
                page.locator('#download-job-pause').click()
                expect(page.locator('#download-queue-start')).to_have_text('启动')
                page.locator('#download-queue-start').click()
                expect(page.locator('#download-queue-start')).to_have_text('已启动')
                page.locator('#download-job-stop').click()
                expect(page.locator('#download-queue-start')).to_have_text('启动')
                assert actions == ['pause', 'start', 'stop'], actions

                # Let regular polls run after initial load and explicit controls.
                page.wait_for_timeout(1500)
                requests.clear()
                page.wait_for_timeout(10500)
                counts = Counter(requests)
                assert counts['instances'] >= 3, counts
                assert 1 <= counts['jobs/workbench'] <= 3, counts
                assert counts['status'] == counts['overview'] == counts['tasks'] == counts['media'] == counts['clash'] == 0, counts
                assert page.evaluate("savedOption === document.querySelector('#download-job').options[1]")
                page.screenshot(path=str(output / 'workbench-desktop.png'), full_page=True)
                for width in (390, 360):
                    page.set_viewport_size({'width': width, 'height': 844})
                    page.locator('#sidebar').evaluate('node=>Promise.all(node.getAnimations().map(a=>a.finished))')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'overflow at {width}px'
                    page.screenshot(path=str(output / f'workbench-mobile-{width}.png'), full_page=True)
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()
        print(json.dumps(dict(result='PASS', accounts=18, page_errors=errors, poll_requests=dict(counts),
            actions=actions, checks=['layout', 'stable-account-and-task-nodes', 'filter-focus', 'filters',
                'task-inspector-and-logs', 'controls', 'no-hidden-view-polls',
                'stable-job-options', 'desktop-mobile'], output=str(output)), ensure_ascii=False))


if __name__ == '__main__':
    main()
