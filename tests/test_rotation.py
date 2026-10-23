"""Durable rotation counters and real node switching between queued videos."""
import http.server
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

import yaml

from app import create_app
from webapp.clash import ClashManager, ROOT


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\n', encoding='utf-8')
        self.app = create_app(self.config, self.root / 'state', start_worker=False)
        self.client = self.app.test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]
        self.headers = {'X-CSRF-Token': token}
        self.manager = self.app.extensions['clash']
        self.store = self.app.extensions['task_store']

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def nodes(self, names=('A', 'B', 'C')):
        return [{'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': 1234} for name in names]

    def seed(self):
        (self.manager.directory / 'nodes.yaml').write_text(
            yaml.safe_dump({'proxies': self.nodes()}), encoding='utf-8')
        self.manager.config['selected'] = 'B'
        self.manager.save()

    def rotation(self, **overrides):
        return {'enabled': True, 'nodes': ['A', 'B', 'C'], 'every': 2,
                'count_failures': True, **overrides}

    def test_validation_order_persistence_idempotence_and_queue_lock(self):
        self.seed()
        good = self.rotation(nodes=['C', 'B', 'A'])
        response = self.post('/api/clash/rotation', good)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['rotation']['nodes'], ['C', 'B', 'A'])
        self.assertEqual(response.json['rotation']['next_node'], 'B')
        generation = response.json['rotation']['id']
        self.assertEqual(ClashManager(self.manager.directory).rotation()['id'], generation)
        self.assertEqual(self.post('/api/clash/rotation', good).json['rotation']['id'], generation)
        before = (self.manager.directory / 'settings.json').read_bytes()
        for invalid in (self.rotation(nodes=['A']), self.rotation(nodes=['A', 'A']),
                        self.rotation(nodes=['A', 'missing']), self.rotation(nodes='A'),
                        self.rotation(nodes=[{}, 'A']), self.rotation(every=0), self.rotation(every=True),
                        self.rotation(every=1.5), self.rotation(every=1001), self.rotation(enabled=1),
                        self.rotation(count_failures='yes'), {}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.post('/api/clash/rotation', invalid).status_code, 400)
                self.assertEqual((self.manager.directory / 'settings.json').read_bytes(), before)
        self.assertEqual(self.client.post('/api/clash/rotation', json=good).status_code, 403)
        self.store.create('download', {})
        self.assertEqual(self.post('/api/clash/rotation', self.rotation(every=3)).status_code, 409)

    def test_ledger_counts_failures_optionally_and_never_skips_or_other_configurations(self):
        for status, skipped, generation in [('succeeded', False, 'rotation-1'),
                                             ('failed', False, 'rotation-1'),
                                             ('succeeded', True, 'rotation-1'),
                                             ('succeeded', False, 'rotation-2')]:
            task = self.store.create('download', {})
            self.store.update_progress(task['id'], {'rotation_id': generation})
            self.store.finish(task['id'], result={'skipped': skipped}, error='failed' if status == 'failed' else None)
        task = self.store.create('download', {})
        self.store.update_progress(task['id'], {'rotation_id': 'rotation-1'})
        self.assertEqual(self.store.rotation_completed('rotation-1'), 2)
        self.assertEqual(self.store.rotation_completed('rotation-1', False), 1)
        self.assertEqual(self.store.rotation_completed('rotation-2'), 1)

    @unittest.skipUnless((ROOT / '.tools/mihomo/mihomo.exe').is_file(), 'Project Mihomo binary not installed')
    def test_real_proxy_rotation_failures_archive_skips_and_restart(self):
        requests = []
        content = b'rotation fixture video\n' * 100
        nodes = []

        def handler_for(node):
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
                        body = yaml.safe_dump({'proxies': nodes}).encode('utf-8')
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    requests.append((node, self.path))
                    if '/missing.mp4' in self.path:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Length', str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

                def log_message(self, *args):
                    pass
            return Handler

        for name in ('A', 'B', 'C'):
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler_for(name))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            nodes.append({'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': server.server_port})
        (self.manager.directory / 'nodes.yaml').write_text(yaml.safe_dump({'proxies': nodes}), encoding='utf-8')
        self.manager.config['selected'] = 'A'
        self.manager.save()
        self.manager.start()
        self.addCleanup(self.manager.stop)
        self.config.write_text('path: videos\nproxy_mode: clash\n', encoding='utf-8')
        rotation = self.rotation(nodes=['B', 'A', 'C'])
        self.assertEqual(self.post('/api/clash/rotation', rotation).status_code, 200)

        def batch(names):
            result = self.post('/api/downloads', {'urls': [f'http://192.0.2.1/{name}.mp4' for name in names]})
            self.assertEqual(result.status_code, 202, result.json)
            return result.json['items']

        def drain():
            subprocess.run([sys.executable, 'web_worker.py', '--state', str(self.root / 'state')],
                           cwd=ROOT, capture_output=True, check=True, timeout=45)

        first = batch(['missing', 'one'])
        drain()
        self.assertEqual([self.store.get(task['id'])['status'] for task in first], ['failed', 'succeeded'],
                         [self.store.get(task['id']) for task in first])
        self.assertEqual(self.client.get('/api/clash').json['rotation']['completed'], 2)
        second = batch(['one', 'two', 'three'])
        drain()
        tasks = [self.store.get(task['id']) for task in first + second]
        self.assertEqual([task['progress']['proxy_node'] for task in tasks], ['A', 'A', 'C', 'C', 'C'])
        self.assertTrue(tasks[2]['result']['skipped'])
        state = self.client.get('/api/clash').json
        self.assertEqual(state['rotation']['completed'], 4)
        self.assertEqual(state['rotation']['next_node'], 'B')
        self.assertEqual(state['selected'], 'C')  # Flask's original instance sees worker writes.

        restored = ClashManager(self.manager.directory)
        restored.stop()
        self.manager.process.wait(timeout=5)
        restored.start()
        self.addCleanup(restored.stop)
        third = batch(['four'])
        drain()
        self.assertEqual(self.store.get(third[0]['id'])['progress']['proxy_node'], 'B')
        self.assertEqual(self.client.get('/api/clash').json['rotation']['completed'], 5)
        for name, expected in [('missing', {'A'}), ('one', {'A', 'C'}), ('two', {'C'}), ('three', {'C'}), ('four', {'B'})]:
            actual = [node for node, url in requests if f'/{name}.mp4' in url]
            self.assertTrue(actual, name)
            self.assertEqual(set(actual), expected, name)
        # A manual choice starts a fresh ledger at that node.
        self.assertEqual(self.post('/api/clash/select', {'name': 'C'}).status_code, 200)
        state = self.client.get('/api/clash').json['rotation']
        self.assertEqual(state['completed'], 0)
        self.assertEqual(state['next_node'], 'C')
        # Subscription updates remove missing pool entries, disabling only if fewer than two remain.
        subscription_url = f'http://127.0.0.1:{nodes[0]["port"]}/subscribe'
        nodes.pop()
        response = self.post('/api/clash/subscription', {'url': subscription_url})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['rotation']['enabled'])
        self.assertEqual(response.json['rotation']['nodes'], ['B', 'A'])
        self.assertEqual(response.json['rotation']['completed'], 0)
        nodes.pop()
        response = self.post('/api/clash/subscription', {'url': subscription_url})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['rotation']['enabled'])
        self.assertEqual(response.json['rotation']['nodes'], ['A'])

    @unittest.skipUnless((ROOT / '.tools/mihomo/mihomo.exe').is_file(), 'Project Mihomo binary not installed')
    def test_two_worker_processes_have_independent_proxy_nodes_and_rotation_counts(self):
        requests = []

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
                requests.append((self.server.node, self.path))
                body = b'parallel transport fixture' * 4096
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    for offset in range(0, len(body), 8192):
                        self.wfile.write(body[offset:offset + 8192])
                        time.sleep(0.02)
                except (ConnectionError, OSError):
                    pass  # yt-dlp may close its initial format probe early.

            def log_message(self, *args):
                pass

        nodes = []
        for name in ('A', 'B'):
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.node = name
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            nodes.append({'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': server.server_port})
        (self.manager.directory / 'nodes.yaml').write_text(yaml.safe_dump({'proxies': nodes}), encoding='utf-8')
        self.manager.config['selected'] = 'A'
        self.manager.save()
        self.manager.start()
        self.addCleanup(self.manager.stop)
        self.config.write_text('path: videos\nproxy_mode: clash\nworker_count: 2\n', encoding='utf-8')
        self.store.set_runtime({'worker_count': 2, 'min_free_gb': 0})
        response = self.post('/api/clash/rotation', self.rotation(nodes=['A', 'B'], every=1))
        self.assertEqual(response.status_code, 200, response.json)
        generation = response.json['rotation']['id']
        response = self.post('/api/downloads', {'urls': [f'http://192.0.2.1/parallel-{i}.mp4' for i in range(12)]})
        self.assertEqual(response.status_code, 202, response.json)
        children = [subprocess.Popen([sys.executable, 'web_worker.py', '--state', str(self.root / 'state'),
                                     '--slot', str(slot), '--role', 'download'], cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for slot in (0, 1)]
        try:
            for child in children:
                self.assertEqual(child.wait(timeout=90), 0)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
        completed = [self.store.get(item['id']) for item in response.json['items']]
        self.assertTrue(all(item['status'] == 'succeeded' for item in completed), completed)
        self.assertEqual({item['progress']['worker_slot'] for item in completed}, {0, 1})
        for slot in (0, 1):
            items = [item for item in completed if item['progress']['worker_slot'] == slot]
            self.assertGreaterEqual(len(items), 2)
            self.assertEqual([item['progress']['proxy_node'] for item in items],
                             [('A', 'B')[i % 2] for i in range(len(items))])
            identity = generation + (':1' if slot else '')
            self.assertEqual(self.store.rotation_completed(identity), len(items))
            for item in items:
                path = item['url'].split('192.0.2.1')[1]
                self.assertEqual({node for node, url in requests if path in url}, {item['progress']['proxy_node']})
        child_manager = self.manager.for_worker(1)
        self.assertNotEqual(child_manager.config['proxy_port'], self.manager.config['proxy_port'])
        self.manager.stop()
        self.assertFalse(child_manager.status()['running'])


if __name__ == '__main__':
    unittest.main()
