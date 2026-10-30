"""Verify persisted job scope and live workbench/instance synchronization."""
from pathlib import Path
import json
import re
import sys
import tempfile
import threading
from unittest.mock import patch

from flask import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_jobs import QuietHandler
from webapp.browser import browser_options
from webapp.fleet_routes import rpc as instance_rpc
from webapp.database import instances


def main():
    output = Path('.cache/validation/selected-execution')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        url = 'sqlite:///' + (root / 'shared.db').as_posix()
        stores = []
        apps = []
        for identity, role in [('alpha', 'hybrid'), ('beta', 'worker')]:
            config = root / f'{identity}.yaml'
            config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\n'
                              'download_methods: [downmp3]\n', encoding='utf-8')
            app = create_app(config, root / identity, start_worker=False, role=role,
                             instance_id=identity, instance_url='http://127.0.0.1:1', database_url=url)
            if identity == 'alpha':
                manager = app
            store = app.extensions['task_store']
            store.fleet.control(identity, {'action': 'resume'})
            stores.append(store)
            apps.append(app)
        status_gate, status_entered = threading.Event(), threading.Event()
        status_gate.set()
        @manager.before_request
        def slow_status():
            if request.path == '/api/status' and not status_gate.is_set():
                status_entered.set()
                status_gate.wait(15)

        client = manager.test_client()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text)[1]
        headers = {'X-CSRF-Token': csrf}
        jobs = []
        for name, videos in [('Order A', 'npNnbj46_ik\n2Gu7q-KUXCk'),
                             ('Order B', 'WMu6ANTg8mk\naaaaaaaaaaa')]:
            response = client.post('/api/jobs', json={'customer': 'Customer', 'name': name, 'urls': videos}, headers=headers)
            assert response.status_code == 201, response.json
            jobs.append(response.json['id'])
        a, b = jobs
        server = make_server('127.0.0.1', 0, manager, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        peer = make_server('127.0.0.1', 0, apps[1], threaded=True, request_handler=QuietHandler)
        peer_thread = threading.Thread(target=peer.serve_forever, daemon=True)
        peer_thread.start()
        for store, address in zip(stores, [base, f'http://127.0.0.1:{peer.server_port}']):
            with store.connect() as db:
                db.execute(instances.update().where(instances.c.id == store.fleet.id).values(url=address))
        def checked_rpc(fleet, target, path, **kwargs):
            return {'ready':True} if path == '/api/fleet/preflight' else instance_rpc(fleet, target, path, **kwargs)
        errors = []
        try:
            with patch('webapp.fleet_routes.rpc', side_effect=checked_rpc), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                context = browser.new_context(viewport={'width':1440, 'height':1050})
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base)
                expect(page.locator('#download-job')).to_have_value('')
                page.locator('#download-job').select_option(a)
                page.locator('#download-queue-start').click()
                expect(page.locator('#download-queue-start')).to_have_text('已启动')
                response = client.post('/api/jobs/workbench', json={'action':'start', 'job_id':b},
                    headers={**headers, 'X-Instance-ID':'beta'})
                assert response.status_code == 200, response.json
                page.reload()
                expect(page.locator('#download-job')).to_have_value(a)
                page.goto(base)  # Restore from the database even without URL or browser storage.
                expect(page.locator('#download-job')).to_have_value(a)

                # A slow instance-status RPC must not keep the workbench on
                # "queued" after a worker has already claimed its video.
                status_gate.clear()
                page.evaluate('void refresh()')
                assert status_entered.wait(5), 'slow status request was not exercised'
                first = []
                for index, store in enumerate(stores):
                    task = store.claim(store.owner)
                    assert task['batch_id'] == [a, b][index]
                    store.update_progress(task['id'], {**task['progress'], 'stage':'downloading', 'percent':25,
                                                       'speed':(index + 1) * 1024}, store.owner)
                    first.append(task)
                expect(page.locator('#batch-progress')).to_contain_text('npNnbj46_ik')
                expect(page.locator('#batch-progress')).not_to_contain_text('WMu6ANTg8mk')
                expect(page.locator('#download-selection-status')).to_contain_text('当前实例正在处理所选任务的视频', timeout=4000)
                expect(page.locator('#batch-progress')).to_contain_text('本次下载方式：downmp3')
                expect(page.locator('#batch-progress a[href*="instance_id=alpha"]')).to_contain_text('实例设置：')
                expect(page.locator('#batch-progress a[href*="instance_id=beta"]')).to_have_count(0)
                assert not status_gate.is_set(), 'progress waited for the slow status response'
                status_gate.set()
                page.locator('[data-view=instances].nav-item').click()
                expect(page.locator('#fleet-job-context')).to_contain_text('Order A')
                expect(page.locator('#fleet-job-context')).to_contain_text('处理中视频 1 个')
                expect(page.locator('[data-instance-live=alpha]')).to_contain_text('npNnbj46_ik')
                expect(page.locator('[data-instance-live=beta]')).to_contain_text('WMu6ANTg8mk')
                expect(page.locator('[data-instance-live=beta]')).to_contain_text('Order B')
                # Live status must continue while an instance's settings are being edited.
                page.locator('[data-instance-options=alpha] summary').click()
                page.locator('[data-instance-form=alpha] [name=name]').fill('Unsaved name')
                stores[0].update_progress(first[0]['id'], {'stage':'downloading', 'percent':55, 'speed':4096}, stores[0].owner)
                expect(page.locator('[data-instance-live=alpha]')).to_contain_text('55%')
                expect(page.locator('[data-instance-form=alpha] [name=name]')).to_have_value('Unsaved name')
                stores[0].fleet.control('alpha', {'action':'drain'})
                expect(page.locator('[data-instance-state=alpha]')).to_contain_text('等待当前视频完成')
                stores[0].fleet.control('alpha', {'action':'resume'})
                expect(page.locator('[data-instance-state=alpha]')).to_contain_text('已启动 · 处理中')
                page.screenshot(path=str(output / 'instances-desktop.png'), full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                expect(page.locator('#sidebar')).not_to_be_in_viewport()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'instances mobile overflow'
                page.screenshot(path=str(output / 'instances-mobile.png'), full_page=True)
                page.set_viewport_size({'width':1440,'height':1050})

                # A slow response from the previous instance cannot overwrite
                # the new instance's persisted task selection.
                page.locator('[data-view=overview].nav-item').click()
                delayed = []
                def hold_alpha(route):
                    if route.request.method == 'GET' and route.request.headers.get('x-instance-id') == 'alpha' and not delayed:
                        delayed.append(route)
                    else:
                        route.continue_()
                old = client.get('/api/jobs/workbench', headers={'X-Instance-ID':'alpha'}).json
                page.route('**/api/jobs/workbench*', hold_alpha)
                page.evaluate('void refreshSelectedJob()')
                for _ in range(50):
                    if delayed:
                        break
                    page.wait_for_timeout(100)
                assert delayed, 'old instance response was not delayed'
                page.locator('#fleet-selection').select_option('beta')
                expect(page.locator('#download-job')).to_have_value(b)
                delayed[0].fulfill(status=200, content_type='application/json', body=json.dumps(old))
                page.wait_for_timeout(200)
                expect(page.locator('#download-job')).to_have_value(b)
                page.unroute('**/api/jobs/workbench*', hold_alpha)
                page.locator('#fleet-selection').select_option('alpha')
                expect(page.locator('#download-job')).to_have_value(a)
                page.locator('[data-view=instances].nav-item').click()

                other = browser.new_page(viewport={'width':1440,'height':1050})
                other.on('pageerror', lambda error: errors.append(str(error)))
                other.goto(base)
                expect(other.locator('#download-job')).to_have_value(a)
                other.locator('#download-job').select_option(b)
                other.reload()
                expect(other.locator('#download-job')).to_have_value(a)
                other.locator('#download-job').select_option(b)
                expect(other.locator('#download-selection-status')).to_contain_text('当前实例切换到所选任务')
                other.locator('#download-queue-start').click()
                expect(other.locator('#download-queue-start')).to_have_text('已启动')
                assert not stores[0].heartbeat(first[0]['id'], stores[0].owner)
                assert stores[1].heartbeat(first[1]['id'], stores[1].owner), 'switch affected peer lease'
                switched = stores[0].claim(stores[0].owner)
                assert switched['batch_id'] == b
                assert switched['video_id'] == 'aaaaaaaaaaa'
                assert stores[0].claim('extra-owner') is None
                expect(page.locator('#fleet-job-context')).to_contain_text('Order B')
                expect(page.locator('#fleet-job-context')).to_contain_text('处理中视频 1 个')
                expect(page.locator('[data-instance-live=alpha]')).not_to_contain_text('npNnbj46_ik')
                expect(page.locator('[data-instance-live=beta]')).to_contain_text('WMu6ANTg8mk')
                page.locator('[data-view=overview].nav-item').click()
                expect(page.locator('#download-job')).to_have_value(b)
                expect(page.locator('#batch-progress')).to_contain_text('aaaaaaaaaaa')
                expect(page.locator('#batch-progress')).not_to_contain_text('WMu6ANTg8mk')
                expect(page.locator('#batch-progress')).not_to_contain_text('npNnbj46_ik')
                page.screenshot(path=str(output / 'workbench-switched.png'), full_page=True)
                page.locator('#download-job-stop').click()
                expect(page.locator('#toasts')).to_contain_text('已发送立即停止指令')
                assert stores[1].heartbeat(first[1]['id'], stores[1].owner), 'stop affected peer lease'
                page.goto(base)
                expect(page.locator('#download-job')).to_have_value(b)
                expect(page.locator('#download-queue-start')).to_be_enabled()
                expect(page.locator('#fleet-selection-bar')).to_be_visible()
                page.locator('#fleet-selection').select_option('beta')
                expect(page.locator('#download-job')).to_have_value(b)
                expect(page.locator('#download-queue-start')).to_have_text('已启动')
                expect(page.locator('#batch-progress')).to_contain_text('WMu6ANTg8mk')
                page.locator('#download-job-pause').click()
                expect(page.locator('#toasts')).to_contain_text('当前实例将在正在处理的视频全部完成后停止')
                assert stores[1].heartbeat(first[1]['id'], stores[1].owner), 'pause revoked current lease'
                assert stores[1].claim('extra-owner') is None
                page.reload()
                expect(page.locator('#fleet-selection')).to_have_value('beta')
                expect(page.locator('#download-job')).to_have_value(b)
                page.locator('#fleet-selection').select_option('alpha')
                expect(page.locator('#download-job')).to_have_value(b)
                expect(page.locator('#download-queue-start')).to_be_enabled()
                expect(page.locator('#batch-progress')).not_to_contain_text('WMu6ANTg8mk')
                assert not errors, errors
                browser.close()
        finally:
            status_gate.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            peer.shutdown()
            peer.server_close()
            peer_thread.join(timeout=3)
    print('PASS: per-instance start/pause/stop, persisted selections, isolated claims, live progress despite slow status, two clients and mobile layout.')
    print(output.resolve())


if __name__ == '__main__':
    main()
