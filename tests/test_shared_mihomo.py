"""Shared real Mihomo: listener isolation, concurrent startup and legacy upgrade."""
import http.server
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

import psutil
import yaml

from webapp.clash import ClashError, ClashManager, GROUP, ROOT, parse_subscription


BINARY = ROOT / '.tools/mihomo' / ('mihomo.exe' if os.name == 'nt' else 'mihomo')


@unittest.skipUnless(BINARY.is_file(), 'Project Mihomo binary not installed')
class SharedMihomoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = ClashManager(self.root / 'clash', binary=BINARY)
        self.addCleanup(self.manager.stop)
        self.nodes = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.fixture = {}
        started, release, fixture = self.started, self.release, self.fixture
        for name in ('A', 'B'):
            class Handler(http.server.BaseHTTPRequestHandler):
                node = name

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
                    payload = fixture['subscription'] if self.path.endswith('/subscription') else self.node.encode()
                    slow = self.path.endswith('/slow')
                    if slow:
                        payload *= 1024
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    if slow:
                        self.wfile.write(payload[:1])
                        self.wfile.flush()
                        started.set()
                        release.wait(15)
                        self.wfile.write(payload[1:])
                    else:
                        self.wfile.write(payload)

                def log_message(self, *args):
                    pass

            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.nodes.append({'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': server.server_port})
        (self.manager.directory / 'nodes.yaml').write_text(yaml.safe_dump({'proxies': self.nodes}), encoding='utf-8')
        self.manager.config.update(selected='A', subscription_id='local-fixture')
        self.manager.save()

    def test_switching_another_slot_keeps_active_transfer_open(self):
        self.manager.start()
        active = self.manager.for_worker(1)
        active.select('B', automatic=True)
        opener = build_opener(ProxyHandler({'http': active.snapshot()['proxy']}))
        with opener.open('http://192.0.2.1/slow', timeout=20) as response:
            self.assertEqual(response.read(1), b'B')
            self.assertTrue(self.started.wait(3))
            pid = self.manager.owned_process().pid
            extra = self.manager.for_worker(15)
            extra.select('A', automatic=True)
            extra.select('B', automatic=True)
            self.release.set()
            self.assertEqual(response.read(), b'B' * 1023)
            self.assertEqual(self.manager.owned_process().pid, pid)

    def test_failed_subscription_reload_restores_every_workers_last_selection(self):
        self.manager.start()
        worker = self.manager.for_worker(1)
        worker.select('B', automatic=True)
        before = (self.manager.directory / 'settings.json').read_bytes()
        self.fixture['subscription'] = yaml.safe_dump({'proxies': [{**self.nodes[0], 'name': 'C'}]}).encode()
        url = f'http://127.0.0.1:{self.nodes[0]["port"]}/subscription'
        real_api = self.manager.api

        def fail_select(path, method='GET', data=None, **kwargs):
            if path == '/proxies/' + GROUP and method == 'PUT' and data == {'name': 'C'}:
                raise ClashError('Local subscription rollback fixture')
            return real_api(path, method, data, **kwargs)

        with patch.object(self.manager, 'api', side_effect=fail_select):
            with self.assertRaises(ClashError):
                self.manager.import_subscription(url)
        self.assertEqual((self.manager.directory / 'settings.json').read_bytes(), before)
        self.assertEqual(self.route(self.manager), 'A')
        self.assertEqual(self.route(worker), 'B')
        ports = dict(self.manager.config['worker_ports'])
        self.manager.import_subscription(url)
        self.assertEqual(self.manager.config['worker_ports'], ports)
        self.assertEqual(worker.snapshot()['node'], 'C')

    def route(self, manager):
        snapshot = manager.snapshot()
        opener = build_opener(ProxyHandler({'http': snapshot['proxy']}))
        with opener.open('http://192.0.2.1/local-test', timeout=5) as response:
            return response.read().decode()

    def test_listeners_route_independently_reuse_one_pid_and_survive_restart(self):
        self.manager.start()
        pid = self.manager.owned_process().pid
        first, second = self.manager.for_worker(1), self.manager.for_worker(2)
        first.select('B', automatic=True)
        second.select('A', automatic=True)
        self.assertEqual(first.status()['selected'], 'B')
        self.assertEqual([self.route(view) for view in (self.manager, first, second)], ['A', 'B', 'A'])
        first.select('A', automatic=True)
        second.select('B', automatic=True)
        self.assertEqual([self.route(view) for view in (self.manager, first, second)], ['A', 'A', 'B'])
        self.assertEqual({view.owned_process().pid for view in (self.manager, first, second)}, {pid})
        self.assertEqual(len({view.proxy_port for view in (self.manager, first, second)}), 3)
        self.assertFalse((self.manager.directory / 'workers').exists())
        runtime = yaml.safe_load((self.manager.directory / 'runtime.yaml').read_text(encoding='utf-8'))
        self.assertEqual(len(runtime['listeners']), 15)
        self.assertTrue(all(listener['listen'] == '127.0.0.1' for listener in runtime['listeners']))
        real_api = ClashManager.api

        def no_reload(view, path, *args, **kwargs):
            self.assertNotEqual(path, '/configs', 'Reusing worker slots must not reload the core')
            return real_api(view, path, *args, **kwargs)

        with patch.object(ClashManager, 'api', new=no_reload):
            extra = self.manager.for_worker(15)
            extra.select('B', automatic=True)
            self.assertEqual(self.route(extra), 'B')
        self.assertEqual(self.manager.owned_process().pid, pid)
        ports = dict(first.config['worker_ports'])
        self.manager.stop()
        restored = ClashManager(self.manager.directory, binary=BINARY)
        restored.start()
        self.assertEqual(restored.config['worker_ports'], ports)
        self.assertEqual(self.route(restored.for_worker(2)), 'B')
        self.assertEqual(self.route(restored), 'A')

    def test_cold_start_from_four_processes_keeps_all_selections_and_one_core(self):
        script = (
            'import json,sys; from webapp.clash import ClashManager; '
            'm=ClashManager(sys.argv[1]).for_worker(int(sys.argv[2])); '
            'm.select(sys.argv[3], automatic=True); '
            'print(json.dumps(dict(pid=m.owned_process().pid, snapshot=m.snapshot())))'
        )
        children = []
        for slot in range(4):
            children.append(subprocess.Popen([sys.executable, '-c', script, str(self.manager.directory),
                str(slot), ('A', 'B')[slot % 2]], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True))
        outputs = []
        try:
            for child in children:
                output, error = child.communicate(timeout=45)
                self.assertEqual(child.returncode, 0, error)
                outputs.append(json.loads(output))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=10)
        self.assertEqual(len({item['pid'] for item in outputs}), 1)
        self.assertEqual(len({item['snapshot']['proxy'] for item in outputs}), 4)
        self.assertEqual([item['snapshot']['node'] for item in outputs], ['A', 'B', 'A', 'B'])
        self.manager.reload()
        self.assertEqual(self.manager.config['worker_nodes'], {'1': 'B', '2': 'A', '3': 'B'})
        process_file = str(self.manager.directory / 'runtime.yaml')
        cores = []
        for process in psutil.process_iter(['pid', 'name', 'cmdline']):
            if process.info['name'] and process.info['name'].lower() == BINARY.name.lower():
                if process_file in (process.info['cmdline'] or []):
                    cores.append(process.pid)
        self.assertEqual(cores, [outputs[0]['pid']])

    def test_upgrade_retains_main_core_retires_old_worker_and_preserves_selection(self):
        # Start a genuine pre-upgrade root and child, using the old runtime shape.
        legacy = ClashManager(self.manager.directory / 'workers' / '1', binary=BINARY)
        (legacy.directory / 'nodes.yaml').write_text(yaml.safe_dump({'proxies': self.nodes}), encoding='utf-8')
        legacy.config['selected'] = 'B'
        legacy.save()
        self.addCleanup(legacy.stop)
        # Persisting the old shape makes start load a core with only mixed-port.
        for manager in (self.manager, legacy):
            with patch.object(manager, '_ensure_worker_port'), patch.object(manager, '_stop_legacy_workers'):
                manager.start()
            manager.config.pop('listener_layout', None)
            manager.save()
        root_pid = self.manager.owned_process().pid
        self.assertTrue(legacy.running())
        self.manager.start()
        self.assertEqual(self.manager.owned_process().pid, root_pid)
        self.assertFalse(legacy.running())
        worker = self.manager.for_worker(1)
        worker.select('B', automatic=True)
        self.assertEqual(self.route(worker), 'B')
        self.assertEqual(self.route(self.manager), 'A')

    def test_reserved_worker_group_names_cannot_override_local_routing(self):
        node = {**self.nodes[0], 'name': 'download_worker_1'}
        with self.assertRaises(ClashError):
            parse_subscription(yaml.safe_dump({'proxies': [node]}).encode())
        for slot in (-1, 16, True):
            with self.assertRaises(ValueError):
                self.manager.for_worker(slot)


if __name__ == '__main__':
    unittest.main()
