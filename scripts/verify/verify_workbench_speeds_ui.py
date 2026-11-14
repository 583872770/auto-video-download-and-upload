"""Workbench speed totals, session output, instance isolation and unavailable samples."""
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
from scripts.verify.verify_jobs import QuietHandler
from webapp.browser import browser_options


def main():
    output = Path('.runtime/verify/workbench-speeds')
    output.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid',
                         instance_id='alpha', database_url='sqlite:///' + (root / 'test.db').as_posix())
        store = app.extensions['task_store']
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        state = {'mode': 'normal'}
        errors, writes = [], []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                page = browser.new_page(viewport={'width': 1440, 'height': 1050}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: writes.append(request.url) if request.method in ('POST', 'PUT', 'DELETE') else None)
                page.route('**/api/instances?compact=1', lambda route: route.fulfill(json={'instance_id': 'alpha', 'items': [
                    {'id': name, 'name': name, 'role': 'hybrid', 'online': True, 'running': 2, 'paused': 0, 'stop_requested': 0, 'error': ''}
                    for name in ('alpha', 'beta')]}))
                capacity = app.test_client().get('/api/status').json
                page.route('**/api/status', lambda route: route.fulfill(json=deepcopy(capacity)))

                def status(route):
                    if state['mode'] == 'error':
                        route.abort()
                        return
                    identity = route.request.headers.get('x-instance-id')
                    stamp = time.time() - (30 if state['mode'] == 'stale' else 0)
                    speeds = [8e6, 12e6] if identity == 'alpha' else [3e6]
                    active = [dict(id=f'{identity}-{index}', title=f'视频 {index + 1}', status='running',
                        instance_id=identity, progress=dict(stage='downloading', speed=speed, _reported_at=stamp, last_bytes_at=stamp))
                        for index, speed in enumerate(speeds)]
                    active.append(dict(id='waiting', title='等待请求', status='running', progress=dict(stage='waiting_request', speed=99e6, _reported_at=stamp)))
                    if state['mode'] == 'empty':
                        active = []
                    session = None if state['mode'] == 'empty' else dict(
                        id=identity + '-session', started_at='2026-11-14T00:00:00+00:00', elapsed_seconds=1000,
                        file_bytes_exact='1000000000', test_file_bytes_exact='1000000000', downloaded_file_bytes_exact='2000000000',
                        file_count=10, output_mbps=16 if identity == 'alpha' else 48,
                        output_recent_mbps=8, output_history={'points': []})
                    route.fulfill(json=dict(selected_job_id=None, job=None, online=state['mode'] != 'offline',
                        paused=False, running=len(active), queued=0, active_tasks=active, error='', session=session))

                page.route('**/api/jobs/workbench*', status)
                page.goto(base + '/workbench?instance_id=alpha')
                download = page.locator('#workbench-download-speed')
                produced = page.locator('#workbench-output-speed')
                panel = page.get_by_role('region', name='下载与产出速度', exact=True)
                expect(download).to_have_text('20.00')
                expect(produced).to_have_text('2.00')
                expect(panel).to_contain_text('160.00 Mbps')
                expect(panel).to_contain_text('16.00 Mbps')
                assert panel.bounding_box()['y'] < page.locator('.download-console').bounding_box()['y']
                page.screenshot(path=str(output / 'speeds-1440.png'), full_page=True)
                page.get_by_label('当前实例', exact=True).select_option('beta')
                expect(download).to_have_text('3.00')
                expect(produced).to_have_text('6.00')
                state['mode'] = 'stale'
                expect(download).to_have_text('—', timeout=12000)
                expect(produced).to_have_text('6.00')
                expect(panel).to_contain_text('0 / 1 个任务有新鲜速度采样')
                state['mode'] = 'offline'
                expect(produced).to_have_text('—', timeout=12000)
                expect(panel).to_contain_text('实例离线')
                state['mode'] = 'empty'
                expect(download).to_have_text('0.00', timeout=12000)
                expect(produced).to_have_text('—')
                expect(panel).to_contain_text('暂无当前会话')
                state['mode'] = 'normal'
                expect(produced).to_have_text('6.00', timeout=12000)
                state['mode'] = 'error'
                expect(produced).to_have_text('—', timeout=12000)
                expect(panel).to_contain_text('读取失败')
                state['mode'] = 'normal'
                expect(download).to_have_text('3.00', timeout=12000)
                for width in (768, 390, 360):
                    page.set_viewport_size({'width': width, 'height': 844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    page.screenshot(path=str(output / f'speeds-{width}.png'), full_page=True)
                assert not errors, errors
                assert not writes, writes
                browser.close()
            print(json.dumps({'ok': True, 'browser_errors': errors, 'screenshots': str(output)}))
        finally:
            server.shutdown()
            server.server_close()
            store.db.engine.dispose()


if __name__ == '__main__':
    main()
