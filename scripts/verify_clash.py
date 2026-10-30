"""Exercise subscription, selection and latency UI against an isolated real core."""
import http.server
import json
from pathlib import Path
import select
import socket
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
import yaml

from app import create_app
from webapp.browser import browser_options
import webapp.clash as clash_module
from Utils.runtime import ROOT
from scripts.verify_web import QuietHandler


def main():
    output = ROOT / '.cache/validation' / ('clash-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    config = output / 'config.yaml'
    config.write_text('path: videos\n', encoding='utf-8')

    class Fixture(http.server.BaseHTTPRequestHandler):
        def do_CONNECT(self):
            with socket.create_connection(('127.0.0.1', self.server.server_port)) as upstream:
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                while True:
                    ready, _, _ = select.select([self.connection, upstream], [], [], 5)
                    if not ready:
                        return
                    for stream in ready:
                        data = stream.recv(65536)
                        if not data:
                            return
                        (upstream if stream is self.connection else self.connection).sendall(data)

        def do_GET(self):
            if self.path.startswith('/subscribe'):
                self.send_response(200)
                self.end_headers()
                backup = self.path.startswith('/subscribe-backup')
                port = backup_fixture.server_port if backup else self.server.server_port
                names = ('备用节点 A', '备用节点 B') if backup else ('剩余流量：100 GB', '测试节点 / A', '测试节点 B')
                nodes = [{'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': port} for name in names]
                self.wfile.write(yaml.safe_dump({'proxies': nodes}).encode())
            else:
                self.send_response(204)
                self.send_header('X-Proxy-Identity', str(self.server.server_port))
                self.end_headers()

        def log_message(self, *args):
            pass

    fixture = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    backup_fixture = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=backup_fixture.serve_forever, daemon=True).start()
    previous_test_url = clash_module.TEST_URL
    clash_module.TEST_URL = f'http://127.0.0.1:{fixture.server_port}/probe'
    app = create_app(config, output / 'state', start_worker=False)
    manager = app.extensions['clash']
    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {}
    errors = []
    original_validate = clash_module.validate_subscription_url
    subscription_url = f'http://127.0.0.1:{fixture.server_port}/subscribe?token=PRIVATE_TEST_TOKEN'
    backup_url = f'http://127.0.0.1:{fixture.server_port}/subscribe-backup?token=PRIVATE_BACKUP_TOKEN'

    def validate_fixture(url):
        if url not in (subscription_url, backup_url):
            original_validate(url)

    clash_module.validate_subscription_url = validate_fixture
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#clash', wait_until='networkidle')
                expect(page.locator('#clash-view')).to_be_visible()
                page.locator('#clash-subscription-name').fill('常用订阅')
                page.locator('#clash-subscription-url').fill(subscription_url)
                page.locator('#clash-import').click()
                expect(page.locator('#clash-core-status')).to_have_text('代理运行中', timeout=30000)
                expect(page.locator('.clash-node')).to_have_count(2)
                expect(page.locator('#clash-subscription-url')).to_have_value('')
                expect(page.locator('#proxy-mode-select')).to_have_value('clash')
                expect(page.locator('#clash-selected')).to_have_text('测试节点 / A')
                assert 'PRIVATE_TEST_TOKEN' not in page.content()
                assert yaml.safe_load(config.read_text())['proxy_mode'] == 'clash'
                report['private_subscription_and_core_start'] = 'passed'
                page.locator('[data-node-select="测试节点 B"]').click()
                expect(page.locator('#clash-selected')).to_have_text('测试节点 B')
                assert manager.snapshot()['node'] == '测试节点 B'
                report['node_selection_controls_actual_core'] = 'passed'
                page.locator('#clash-test-all').click()
                expect(page.locator('#clash-test-progress')).to_contain_text('测速完成 2 / 2', timeout=20000)
                assert all(node['delay']['ms'] is not None for node in manager.status()['nodes'])
                assert manager.snapshot()['node'] == '测试节点 B'
                page.locator('#clash-node-sort').select_option('delay')
                page.locator('#clash-node-search').fill('B')
                expect(page.locator('.clash-node')).to_have_count(1)
                page.locator('#clash-node-search').fill('')
                report['latency_progress_search_sort'] = 'passed'
                page.locator('#rotation-enabled').check()
                page.locator('[data-rotation-node="测试节点 B"]').check()
                page.locator('[data-rotation-node="测试节点 / A"]').check()
                page.locator('#rotation-every').fill('3')
                page.locator('#rotation-count-failures').uncheck()
                expect(page.locator('#rotation-draft-note')).to_have_text('有未保存的修改')
                # A poll must preserve the in-progress form, including the selection order.
                page.evaluate('refresh()')
                expect(page.locator('#rotation-every')).to_have_value('3')
                expect(page.locator('#rotation-count-failures')).not_to_be_checked()
                expect(page.locator('#rotation-sequence')).to_have_text('轮换顺序：1. 测试节点 B → 2. 测试节点 / A')
                page.locator('#rotation-save').click()
                expect(page.locator('#rotation-badge')).to_have_text('已开启')
                expect(page.locator('#rotation-draft-note')).not_to_have_text('有未保存的修改')
                saved = manager.status()['rotation']
                assert saved['nodes'] == ['测试节点 B', '测试节点 / A']
                assert saved['every'] == 3 and not saved['count_failures']
                page.reload(wait_until='networkidle')
                expect(page.locator('#rotation-enabled')).to_be_checked()
                expect(page.locator('#rotation-every')).to_have_value('3')
                expect(page.locator('#rotation-count-failures')).not_to_be_checked()
                report['rotation_selection_order_save_and_reload'] = 'passed'
                first = manager.config['active_subscription_id']
                page.locator('#clash-subscription-new').click()
                page.locator('#clash-subscription-name').fill('备用订阅')
                page.locator('#clash-subscription-url').fill(backup_url)
                page.evaluate('refresh()')
                expect(page.locator('#clash-subscription-name')).to_have_value('备用订阅')
                expect(page.locator('#clash-subscription-url')).to_have_value(backup_url)
                page.locator('#clash-import').click()
                expect(page.locator('#clash-subscription-badge')).to_have_text('已保存 2 个')
                expect(page.locator('#clash-selected')).to_have_text('测试节点 B')
                assert manager.config['active_subscription_id'] == first
                second = next(item['id'] for item in manager.config['subscriptions'] if item['id'] != first)
                expect(page.locator('#clash-subscription-select')).to_have_value(second)
                page.locator('#clash-subscription-name').fill('备用订阅（已更新）')
                page.locator('#clash-import').click()  # Blank URL refreshes this saved profile.
                expect(page.locator('#clash-subscription-select option:checked')).to_have_text('备用订阅（已更新）')
                page.locator('#clash-subscription-switch').click()
                expect(page.locator('#clash-subscription-select option:checked')).to_contain_text('当前使用')
                expect(page.locator('#clash-selected')).to_have_text('备用节点 A')
                expect(page.locator('#rotation-enabled')).not_to_be_checked()
                assert manager.snapshot()['node'] == '备用节点 A'
                # Verify actual media requests follow the backup subscription's proxy.
                from urllib.request import ProxyHandler, build_opener
                with build_opener(ProxyHandler({'http': manager.proxy_url})).open('http://192.0.2.1/probe', timeout=5) as response:
                    assert response.headers['X-Proxy-Identity'] == str(backup_fixture.server_port)
                page.reload(wait_until='networkidle')
                expect(page.locator('#clash-subscription-select')).to_have_value(second)
                expect(page.locator('#clash-selected')).to_have_text('备用节点 A')
                assert 'PRIVATE_BACKUP_TOKEN' not in page.content()
                page.locator('#clash-subscription-select').select_option(first)
                page.locator('#clash-subscription-switch').click()
                expect(page.locator('#clash-selected')).to_have_text('测试节点 B')
                expect(page.locator('#rotation-enabled')).to_be_checked()
                expect(page.locator('#rotation-every')).to_have_value('3')
                with build_opener(ProxyHandler({'http': manager.proxy_url})).open('http://192.0.2.1/probe', timeout=5) as response:
                    assert response.headers['X-Proxy-Identity'] == str(fixture.server_port)
                report['multiple_subscriptions_save_update_switch_route_and_reload'] = 'passed'
                # Queue a fixture job without a worker to verify the shared lock in the UI.
                store = app.extensions['task_store']
                task = store.create('download', {'config': {}, 'base': str(output)})
                page.evaluate('refresh()')
                expect(page.locator('#clash-lock-note')).to_be_visible(timeout=10000)
                expect(page.locator('[data-node-select="测试节点 / A"]')).to_be_disabled()
                expect(page.locator('#clash-import')).to_be_disabled()
                expect(page.locator('#clash-stop')).to_be_disabled()
                expect(page.locator('#rotation-save')).to_be_disabled()
                expect(page.locator('#rotation-every')).to_be_disabled()
                store.finish(task['id'], result={'message': 'fixture complete'})
                page.evaluate('refresh()')
                expect(page.locator('#clash-lock-note')).not_to_be_visible(timeout=10000)
                report['queue_locks_node_changes'] = 'passed'
                page.locator('#clash-subscription-select').select_option(second)
                page.locator('#clash-subscription-delete').click()
                expect(page.locator('#clash-subscription-badge')).to_have_text('已保存 1 个')
                expect(page.locator('#clash-subscription-select')).to_have_value(first)
                report['delete_inactive_subscription'] = 'passed'
                page.locator('#clash-stop').click()
                expect(page.locator('#clash-core-status')).to_have_text('代理已停止')
                page.locator('#clash-start').click()
                expect(page.locator('#clash-core-status')).to_have_text('代理运行中')
                expect(page.locator('#clash-selected')).to_have_text('测试节点 B')
                report['stop_start_preserves_selection'] = 'passed'
                page.reload(wait_until='networkidle')
                expect(page.locator('#clash-node-count')).to_have_text('2')
                expect(page.locator('#clash-selected')).to_have_text('测试节点 B')
                page.screenshot(path=str(output / 'desktop.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'mobile.png'), full_page=True)
                report['reload_and_responsive_layout'] = 'passed'
                assert not errors, errors
                report['browser_errors'] = errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        manager.stop()
        fixture.shutdown()
        fixture.server_close()
        backup_fixture.shutdown()
        backup_fixture.server_close()
        clash_module.TEST_URL = previous_test_url
        clash_module.validate_subscription_url = original_validate
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'CLASH_WEB_VERIFIED: {output / "report.json"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

