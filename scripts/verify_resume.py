"""Verify selected-task downloads, ownership and paused-instance recovery."""
from pathlib import Path
import re
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from flask import request
from webapp.bulk import run_import
from webapp.jobs import Jobs
from scripts.verify_jobs import QuietHandler
from webapp.browser import browser_options


def main():
    output = Path('.cache/validation/resume')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\ndownload_methods: [downmp3]\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid',
                         instance_id='fixture', instance_url='http://127.0.0.1:1',
                         database_url='sqlite:///' + (root / 'shared.db').as_posix())
        store = app.extensions['task_store']
        release, entered = threading.Event(), threading.Event()

        def slow_resume():
            entered.set()
            if not release.wait(15):
                raise ValueError('Fixture resume timed out')

        original_control = app.view_functions['download_workbench']
        delay_control = [False]
        def delayed_control():
            if delay_control[0] and request.method == 'POST' and (request.get_json(silent=True) or {}).get('action') == 'start':
                slow_resume()
            return original_control()
        app.view_functions['download_workbench'] = delayed_control
        store.fleet.check_local = lambda policy: ({'ready':True}, 200)
        client = app.test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text)[1]
        headers = {'X-CSRF-Token': token}
        def create(name, urls=''):
            response = client.post('/api/jobs', json={'customer': 'Customer', 'name': name, 'urls': urls}, headers=headers)
            assert response.status_code == 201, response.json
            return response.json['id']
        a, b = create('Order A','npNnbj46_ik'), create('Order B', '2Gu7q-KUXCk')
        for index in range(22):
            create(f'Empty {index:02d}')
        jobs = Jobs(store)
        store.set_downloads_paused(True)
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, **browser_options(p.chromium))
                page = browser.new_page(viewport={'width':1440, 'height':1050})
                page.on('pageerror', lambda error: errors.append(str(error)))
                # No implicit selection, including when there are more than 20 tasks.
                page.goto(base)
                expect(page.locator('#download-queue-start')).to_be_disabled()
                expect(page.locator('#download-job')).to_have_value('')
                expect(page.locator('#download-job option')).to_have_count(21)
                expect(page.locator('#download-form')).to_have_count(0)
                expect(page.locator('#download-urls')).to_have_count(0)
                expect(page.locator('#bulk-import-dialog')).to_have_count(0)
                page.locator('#download-jobs-more').click()
                expect(page.locator('#download-job option')).to_have_count(25)
                page.locator('#download-job').select_option(a)
                expect(page.locator('#download-job-summary')).to_contain_text('当前实例 · 下载顺序：downmp3')
                for path, selector, note in [(f'/?job={a}', '#download-queue-start', '#download-execution-status'),
                                              (f'/?job={b}', '#download-queue-start', '#download-execution-status')]:
                    store.set_downloads_paused(True)
                    release.clear(); entered.clear(); delay_control[0] = True
                    page.goto(base + path)
                    button = page.locator(selector)
                    expect(button).to_be_enabled()
                    button.click()
                    expect(button).to_contain_text('正在启动')
                    expect(button).to_be_disabled()
                    assert entered.wait(3)
                    if path.startswith('/?'):
                        expect(page.locator('#download-job')).to_be_disabled()
                    page.screenshot(path=str(output / ('home-pending.png' if path.startswith('/?') else 'jobs-pending.png')))
                    release.set()
                    expect(button).not_to_contain_text('正在启动')
                    assert jobs.detail(a)['total'] == 1
                    assert not jobs.detail(a)['released']
                    assert not jobs.detail(b)['released']
                    assert store.downloads_paused(), 'instance start changed the shared switch'
                    assert not store.fleet.row()['paused']
                    assert client.get('/api/jobs/workbench').json['selected_job_id'] == (a if path == f'/?job={a}' else b)
                    delay_control[0] = False
                    page.screenshot(path=str(output / ('home-result.png' if path.startswith('/?') else 'jobs-result.png')))
                    store.fleet.control('fixture', {'action':'drain'})
                    page.locator('[data-view="instances"].nav-item').click()
                    expect(page.locator('#instances-view')).to_be_visible()
                    expect(page.locator('#overview-view')).to_be_hidden()
                    expect(page.locator('#fleet-nav')).to_have_class('nav-item active')
                    # An environment failure must stay paused and show the reason; retry remains available.
                    store.fleet.check_local = lambda policy: ({'error':'Fixture environment needs repair'}, 409)
                    page.locator('[data-instance-action="resume"]').click()
                    expect(page.locator('#toasts')).to_contain_text('Fixture environment needs repair')
                    expect(page.locator('[data-instance-action="resume"]')).to_be_enabled()
                    assert store.fleet.row()['paused']
                    # Exercise the real instance-control endpoint with a delayed local preflight.
                    release.clear()
                    entered.clear()
                    def slow_preflight(policy):
                        slow_resume()
                        return {'ready': True}, 200
                    store.fleet.check_local = slow_preflight
                    resume = page.locator('[data-instance-action="resume"]')
                    resume.click()
                    expect(resume).to_have_text('正在检查并恢复…')
                    expect(resume).to_be_disabled()
                    assert entered.wait(3)
                    page.evaluate('loadFleet()')  # Polling must not replace the in-flight button.
                    expect(resume).to_have_text('正在检查并恢复…')
                    expect(resume).to_be_disabled()
                    release.set()
                    expect(page.locator('#fleet-instances .count-pill')).to_have_text('在线')
                    assert not store.fleet.row()['paused']
                    page.locator('[data-view="overview"]').first.click()
                    expect(page.locator('#download-execution-status')).not_to_contain_text('在线下载实例均已暂停')
                    store.fleet.control('fixture', {'action':'drain'})
                # A timeout means the result is unknown; it must not claim cancellation or success.
                store.set_downloads_paused(True)
                release.clear()
                entered.clear()
                page.add_init_script('AbortSignal.timeout = () => { const controller = new AbortController(); setTimeout(() => controller.abort(new DOMException("test timeout", "TimeoutError")), 300); return controller.signal; };')
                delay_control[0] = True
                page.goto(base + f'/?job={a}')
                page.locator('#download-queue-start').click()
                expect(page.locator('#download-execution-status')).to_contain_text('结果尚未确认')
                expect(page.locator('#download-queue-start')).to_be_enabled()
                assert entered.wait(3)
                release.set()
                page.screenshot(path=str(output / 'timeout-feedback.png'))
                delay_control[0] = False
                expect(page.locator('#download-queue-start')).to_have_text('任务已开始')
                # Search results retain an explicitly chosen task, and loading via
                # task-center link keeps that choice through homepage navigation.
                page.locator('#download-job-search').fill('Order B')
                expect(page.locator('#download-job option')).to_have_count(3)
                page.locator('#download-job').select_option(b)
                expect(page.locator('#download-job-note')).to_contain_text('Order B')
                page.locator('#download-job-detail').click()
                expect(page.locator('#detail-heading h1')).to_have_text('Order B')
                store.set_downloads_paused(True)
                page.reload()
                page.locator('#system-note a').first.click()
                expect(page.locator('#download-job')).to_have_value(b)
                page.set_viewport_size({'width':390,'height':844})
                expect(page.locator('#download-queue-start')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile overflow'
                page.screenshot(path=str(output / 'selected-job-mobile.png'), full_page=True)
                assert not errors, errors
                browser.close()
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
    print('Selected-job browser verification passed: required selection, no video-entry controls, pagination/search, start/stop isolation, node recovery, task-center navigation, mobile layout and timeout feedback.')


if __name__ == '__main__':
    main()
