"""Verify resource protection, live updates and stale data in an isolated page."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_concurrency_filters_ui import fixture
from scripts.verify_cookie_runtime_ui import QuietHandler


def main():
    channel = os.environ.get('VIDEO_UI_BROWSER_CHANNEL')
    output = Path(__file__).resolve().parents[1] / '.cache/worker-control-preview'
    output.mkdir(parents=True, exist_ok=True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='worker-control-') as temporary, patch.dict(os.environ, environment, clear=True):
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
        runtime['worker_control'] = dict(used=4, dynamic_limit=4, normal_limit=16, configured_limit=20,
            resource_limited=True, pressure=dict(active=False),
            stages=dict(transferring=2, preparing=1, processing=1),
            last_reduction=dict(**{'from': 6}, to=4, reasons=[dict(code='io', value=22.9, threshold=15, source='cgroup')]),
            measurements=dict(cpu_percent=33, memory_available_mb=11000, io_pressure=9.5,
                              io_pressure_source='cgroup', io_pressure_host=22.9),
            recovery=dict(eligible_at=time.time() + 60, blocked_by=[], cpu_below=80,
                          io_below=15, memory_at_least_mb=1920, interval_seconds=60, step=1))
        age = [0]
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, channel=channel)
                page = browser.new_page(viewport={'width': 1440, 'height': 1100})
                page.on('pageerror', lambda error: errors.append(str(error)))
                def diagnostic(route):
                    response = route.fetch()
                    body = response.json()
                    for item in body.get('items', []):
                        if item['id'] == 'preview':
                            item['concurrency'] = {**deepcopy(runtime), 'time': time.time() - age[0]}
                    route.fulfill(response=response, json=body)
                page.route('**/api/instances*', diagnostic)
                page.goto(f'http://127.0.0.1:{server.server_port}/#overview')
                panel = page.locator('[data-concurrency-panel=overview]')
                control = panel.locator('.worker-control')
                expect(control).to_contain_text('4 / 4')
                expect(control).to_contain_text('常规上限 16 · 配置上限 20')
                expect(control).to_contain_text('实际传输 2 · 等待 / 解析 1 · 后处理 1')
                expect(control).to_contain_text('本实例 I/O 压力 9.5%')
                expect(control).to_contain_text('整机 22.9%，仅供参考')
                expect(control).to_contain_text('最近一次缩容：6 → 4')
                expect(control).to_contain_text('CPU < 80%、I/O 压力 < 15%')
                expect(control.locator('[data-capacity-deadline]')).to_have_count(1)
                control.screenshot(path=str(output / 'protection-desktop.png'))
                runtime['worker_control']['recovery']['eligible_at'] = time.time() - 1
                page.evaluate('loadFleet()')
                expect(control).to_contain_text('调整间隔已到，等待新采样检查')
                expect(control).to_contain_text('4 / 4')

                runtime['worker_control']['pressure']['active'] = True
                runtime['worker_control']['recovery']['blocked_by'] = ['pressure', 'io']
                runtime['worker_control']['measurements']['io_pressure'] = 25
                page.evaluate('loadFleet()')
                expect(control).to_contain_text('I/O 压力尚未低于恢复阈值')
                expect(control).to_contain_text('资源保护已降低上限')
                expect(control).not_to_contain_text('当前样本满足资源恢复条件')

                # Updating the summary must preserve the existing filter controls.
                selector = panel.locator('[data-cookie-account-filter]')
                selector.focus()
                runtime['worker_control']['dynamic_limit'] = 5
                runtime['worker_control']['pressure']['active'] = False
                runtime['worker_control']['recovery']['blocked_by'] = []
                runtime['worker_control']['measurements']['io_pressure'] = 9.5
                page.evaluate('loadFleet()')
                expect(control).to_contain_text('4 / 5')
                expect(selector).to_be_focused()

                for width in (390, 360):
                    page.set_viewport_size({'width': width, 'height': 844})
                    page.locator('#sidebar').evaluate('node=>Promise.all(node.getAnimations().map(a=>a.finished))')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'overflow at {width}px'
                control.screenshot(path=str(output / 'protection-mobile.png'))

                runtime['worker_control']['measurements']['io_pressure_source'] = 'host'
                page.evaluate('loadFleet()')
                expect(control).to_contain_text('本实例 I/O 指标不可用，当前使用整机指标')
                runtime['worker_control']['measurements'].update(io_pressure_source='unavailable', io_pressure=None)
                page.evaluate('loadFleet()')
                expect(control).to_contain_text('I/O 指标未取得，当前按 CPU 和内存检查')
                expect(control).not_to_contain_text('I/O 压力 0%')
                age[0] = 30
                page.evaluate('loadFleet()')
                expect(control).to_contain_text('任务进程状态待更新')
                expect(control).not_to_contain_text('4 / 5')
                age[0] = 0
                runtime['online'] = False
                page.evaluate('loadFleet()')
                expect(control).to_contain_text('当前占用、动态上限和恢复进度待确认')
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()
        print(json.dumps({'result': 'PASS', 'scenarios': ['protected', 'recovery-eligibility', 'blocked',
            'live-update-focus', 'desktop-mobile', 'host-fallback', 'missing-io', 'stale-offline'],
            'page_errors': errors, 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
