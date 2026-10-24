"""Private subscription boundaries and real Mihomo routing with local fixtures."""
import http.server
import json
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

import yaml

from app import create_app
from Download import build_ydl_options
from webapp.clash import ClashError, ClashManager, GROUP, ROOT, parse_subscription


class ClashTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\n', encoding='utf-8')
        self.app = create_app(self.config, self.root / 'state', start_worker=False)
        self.app.testing = True
        self.client = self.app.test_client()
        page = self.client.get('/')
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        self.headers = {'X-CSRF-Token': token}
        self.manager = self.app.extensions['clash']

    def post(self, path, data=None):
        return self.client.post(path, json=data or {}, headers=self.headers)

    def test_subscription_credentials_are_not_in_read_apis_or_download_snapshots(self):
        self.manager.config['subscription_url'] = 'https://example.test/subscribe?token=PRIVATE_SUBSCRIPTION'
        self.manager.save()
        self.manager.config['secret'] = 'PRIVATE_CONTROLLER'
        for url in ('/api/clash', '/api/config', '/api/status', '/'):
            response = self.client.get(url)
            self.assertNotIn('PRIVATE_SUBSCRIPTION', response.text)
            self.assertNotIn('PRIVATE_CONTROLLER', response.text)
        self.assertEqual(self.post('/api/clash/select', {'name': 'missing'}).status_code, 502)
        self.config.write_text('path: videos\nproxy_mode: clash\n', encoding='utf-8')
        snapshot = {'directory': str(self.manager.directory), 'node': 'Test Node', 'proxy': self.manager.proxy_url}
        with patch.object(self.manager, 'snapshot', return_value=snapshot):
            task = self.post('/api/tasks', {'action': 'download', 'urls': 'https://example.test/video.mp4'})
        self.assertEqual(task.status_code, 202)
        saved = self.app.extensions['task_store'].get(task.json['id'], private=True)
        self.assertEqual(saved['payload']['clash']['node'], 'Test Node')
        self.assertNotIn('PRIVATE_SUBSCRIPTION', json.dumps(saved))
        self.assertNotIn('PRIVATE_CONTROLLER', json.dumps(saved))

    def test_queued_download_locks_subscription_node_and_connection_changes(self):
        self.post('/api/tasks', {'action': 'download', 'urls': 'https://example.test/video.mp4'})
        for path, data in (('/api/clash/subscription', {'url': 'https://example.test/sub'}),
                           ('/api/clash/select', {'name': 'other'}), ('/api/clash/stop', {})):
            self.assertEqual(self.post(path, data).status_code, 409)
        response = self.client.put('/api/config', json={'proxy_mode': 'direct'}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.client.get('/api/clash').json['locked'])
        self.assertEqual(self.client.post('/api/clash/start').status_code, 403)

    def test_unresolved_managed_proxy_cannot_silently_download_direct(self):
        with self.assertRaisesRegex(ValueError, 'Web'):
            build_ydl_options({'proxy_mode': 'clash'}, self.root)
        self.assertEqual(build_ydl_options({'proxy_mode': 'direct', 'proxy': 'http://unused'}, self.root)['proxy'], '')
        self.assertEqual(build_ydl_options({'proxy_mode': 'clash', '_resolved_clash_proxy': self.manager.proxy_url}, self.root)['proxy'], self.manager.proxy_url)

    def test_subscription_parser_rejects_duplicates_and_runtime_ignores_remote_settings(self):
        node = {'name': 'node', 'type': 'http', 'server': '127.0.0.1', 'port': 1234}
        data = yaml.safe_dump({'proxies': [node], 'mixed-port': 1, 'allow-lan': True,
                               'external-controller': '0.0.0.0:1', 'rules': ['MATCH,DIRECT']}).encode()
        nodes = parse_subscription(data)
        runtime = self.manager.runtime(nodes)
        self.assertFalse(runtime['allow-lan'])
        self.assertTrue(runtime['external-controller'].startswith('127.0.0.1:'))
        self.assertEqual(runtime['rules'], ['MATCH,' + GROUP])
        for content in (b'not-a-clash-subscription', b'proxies: []',
                        yaml.safe_dump({'proxies': [node, node]}).encode()):
            with self.assertRaises(ClashError):
                parse_subscription(content)

    @unittest.skipUnless((ROOT / '.tools/mihomo/mihomo.exe').is_file(), 'Project Mihomo binary not installed')
    def test_real_core_import_select_delay_route_restart_and_rollback(self):
        fixture = {'content': b'', 'proxy_requests': 0}
        video_bytes = b'local video transport through Mihomo\n' * 100

        class Handler(http.server.BaseHTTPRequestHandler):
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
                    self.wfile.write(fixture['content'])
                else:
                    fixture['proxy_requests'] += 1
                    self.send_response(200 if self.path.endswith('/download.mp4') else 204)
                    self.send_header('X-Local-Proxy-Fixture', 'yes')
                    if self.path.endswith('/download.mp4'):
                        self.send_header('Content-Type', 'video/mp4')
                        self.send_header('Content-Length', str(len(video_bytes)))
                    self.end_headers()
                    if self.path.endswith('/download.mp4'):
                        self.wfile.write(video_bytes)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(self.manager.stop)
        fixture['content'] = yaml.safe_dump({'proxies': [
            {'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': server.server_port}
            for name in ('节点 / A', '节点 B')]}).encode('utf-8')
        url = f'http://127.0.0.1:{server.server_port}/subscribe?token=PRIVATE_TOKEN'
        result = self.post('/api/clash/subscription', {'url': url})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['running'])
        self.assertNotIn('PRIVATE_TOKEN', result.text)
        self.assertEqual(yaml.safe_load(self.config.read_text())['proxy_mode'], 'clash')
        selected = self.post('/api/clash/select', {'name': '节点 B'})
        self.assertEqual(selected.json['selected'], '节点 B')
        opener = build_opener(ProxyHandler({'http': self.manager.proxy_url}))
        with opener.open('http://192.0.2.1/probe', timeout=5) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers['X-Local-Proxy-Fixture'], 'yes')
        with patch('webapp.clash.TEST_URL', f'http://127.0.0.1:{server.server_port}/probe'):
            response = self.post('/api/clash/delay')
            self.assertEqual(response.status_code, 202)
            deadline = time.monotonic() + 12
            while self.manager.testing['running'] and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertFalse(self.manager.testing['running'])
        self.assertEqual(self.manager.testing['done'], 2)
        self.assertTrue(all(delay['ms'] is not None for delay in self.manager.delays.values()))
        self.assertEqual(self.manager.snapshot()['node'], '节点 B')
        with self.assertRaises(ClashError):
            self.manager.snapshot(expected='节点 / A')
        task = self.post('/api/tasks', {'action': 'download', 'urls': 'http://192.0.2.1/download.mp4'}).json
        subprocess.run([sys.executable, 'web_worker.py', '--state', str(self.root / 'state')], cwd=ROOT,
                       capture_output=True, check=True, timeout=30)
        store = self.app.extensions['task_store']
        self.assertEqual(store.get(task['id'])['status'], 'succeeded', store.get(task['id']))
        downloaded = list((self.root / 'videos').rglob('*.mp4'))
        self.assertEqual(len(downloaded), 1)
        self.assertEqual(downloaded[0].read_bytes(), video_bytes)
        task = self.post('/api/tasks', {'action': 'download', 'urls': 'http://192.0.2.1/another.mp4'}).json
        self.manager.select('节点 / A')  # Simulate a controller change outside this web UI.
        subprocess.run([sys.executable, 'web_worker.py', '--state', str(self.root / 'state')], cwd=ROOT,
                       capture_output=True, check=True, timeout=30)
        self.assertEqual(store.get(task['id'])['status'], 'failed')
        self.assertIn('节点已改变', store.get(task['id'])['error'])
        self.manager.select('节点 B')
        before = (self.manager.directory / 'nodes.yaml').read_bytes()
        fixture['content'] = yaml.safe_dump({'proxies': [
            {'name': 'New Node', 'type': 'http', 'server': '127.0.0.1', 'port': server.server_port}]}).encode()
        real_api = self.manager.api

        def fail_select(path, method='GET', data=None, **kwargs):
            if path == '/proxies/' + GROUP and method == 'PUT' and data == {'name': 'New Node'}:
                raise ClashError('模拟新配置选择失败')
            return real_api(path, method, data, **kwargs)

        with patch.object(self.manager, 'api', side_effect=fail_select):
            failed = self.post('/api/clash/subscription')
        self.assertEqual(failed.status_code, 502)
        self.assertEqual((self.manager.directory / 'nodes.yaml').read_bytes(), before)
        self.assertEqual(self.manager.snapshot()['node'], '节点 B')
        fixture['content'] = b'invalid-subscription PRIVATE_TOKEN'
        failed = self.post('/api/clash/subscription')
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn('PRIVATE_TOKEN', failed.text)
        self.assertEqual((self.manager.directory / 'nodes.yaml').read_bytes(), before)
        self.assertTrue(self.manager.running())
        # Update a paused queue after its selected node is removed from the subscription.
        task = self.post('/api/tasks', {'action': 'download', 'urls': 'http://192.0.2.1/updated/download.mp4'}).json
        previous_subscription = store.get(task['id'], private=True)['payload']['clash']['subscription_id']
        store.set_downloads_paused(True)
        fixture['content'] = yaml.safe_dump({'proxies': [
            {'name': 'New Node', 'type': 'http', 'server': '127.0.0.1', 'port': server.server_port}]}).encode()
        updated = self.post('/api/clash/subscription')
        self.assertEqual(updated.status_code, 200, updated.json)
        self.assertNotEqual(self.manager.config['subscription_id'], previous_subscription)
        self.assertEqual(store.download_queue(), {'paused': True, 'queued': 1, 'running': 0})
        self.assertEqual(self.post('/api/downloads/resume').status_code, 200)
        subprocess.run([sys.executable, 'web_worker.py', '--state', str(self.root / 'state')], cwd=ROOT,
                       capture_output=True, check=True, timeout=30)
        current = store.get(task['id'])
        self.assertEqual(current['status'], 'succeeded', current)
        self.assertEqual(current['progress']['proxy_node'], 'New Node')
        restored = ClashManager(self.manager.directory)
        self.assertEqual(restored.status()['selected'], 'New Node')
        restored.stop()
        self.assertFalse(restored.running())
        restored.start()
        self.assertEqual(restored.status()['selected'], 'New Node')
        restored.stop()


if __name__ == '__main__':
    unittest.main()
