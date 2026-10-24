"""Durable stop/resume, owner fencing and actual partial-file resumption."""
from concurrent.futures import ThreadPoolExecutor
import http.server
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from app import create_app
from tests import test_scale
from webapp.control import ROOT
from webapp.store import TaskStore


class ControlContract:
    def test_subscription_update_requires_paused_and_drained_queue_and_preserves_jobs(self):
        from webapp.clash import ClashError

        manager = self.app.extensions['clash']
        task = self.store.create('download', {'config': {}, 'base': str(self.root),
                                             'urls': ['https://example.test/one.mp4']})
        url = {'url': 'https://example.test/subscription'}
        with patch.object(manager, 'import_subscription', return_value=2) as update:
            self.assertTrue(self.client.get('/api/clash').json['subscription_locked'])
            self.assertEqual(self.client.post('/api/clash/subscription', json=url, headers=self.headers).status_code, 409)
            self.store.claim('simulated-owner')
            self.store.set_downloads_paused(True)
            self.assertTrue(self.client.get('/api/clash').json['subscription_locked'])
            self.assertEqual(self.client.post('/api/clash/subscription', json=url, headers=self.headers).status_code, 409)
            update.assert_not_called()
            self.store.stop_running_downloads(lambda owner: True)
            state = self.client.get('/api/clash').json
            self.assertTrue(state['locked'])  # Manual node changes remain locked.
            self.assertFalse(state['subscription_locked'])
            self.assertEqual(state['subscription_lock_reason'], '')
            response = self.client.post('/api/clash/subscription', json=url, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            update.assert_called_once_with(url['url'])
            with patch.object(self.app.extensions['youtube_login'], 'status', return_value={'active': True}):
                self.assertTrue(self.client.get('/api/clash').json['subscription_locked'])
                self.assertEqual(self.client.post('/api/clash/subscription', json=url, headers=self.headers).status_code, 409)
            update.assert_called_once()
        config_before = self.config.read_bytes()
        with patch.object(manager, 'import_subscription', side_effect=ClashError('fixture update failed')):
            self.assertEqual(self.client.post('/api/clash/subscription', json=url, headers=self.headers).status_code, 502)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.store.download_queue(), {'paused': True, 'queued': 1, 'running': 0})
        self.assertEqual((self.store.get(task['id'])['status'], self.store.get(task['id'])['attempts']), ('queued', 0))

    def test_paused_queue_unlocks_rotation_without_losing_jobs(self):
        manager = self.app.extensions['clash']
        (manager.directory / 'nodes.yaml').write_text(
            'proxies:\n  - {name: A, type: http, server: 127.0.0.1, port: 1234}\n'
            '  - {name: B, type: http, server: 127.0.0.1, port: 1235}\n', encoding='utf-8')
        task = self.store.create('download', {'config': {}, 'base': str(self.root),
                                             'urls': ['https://example.test/one.mp4']})
        config = {'enabled': True, 'nodes': ['A', 'B'], 'every': 3, 'count_failures': False}
        self.assertTrue(self.client.get('/api/clash').json['rotation_locked'])
        self.assertEqual(self.client.post('/api/clash/rotation', json=config, headers=self.headers).status_code, 409)
        self.store.claim('simulated-owner')
        self.store.set_downloads_paused(True)
        self.assertTrue(self.client.get('/api/clash').json['rotation_locked'])
        self.assertEqual(self.client.post('/api/clash/rotation', json=config, headers=self.headers).status_code, 409)
        self.store.stop_running_downloads(lambda owner: True)
        state = self.client.get('/api/clash').json
        self.assertTrue(state['locked'])
        self.assertFalse(state['rotation_locked'])
        response = self.client.post('/api/clash/rotation', json=config, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.store.download_queue(), {'paused': True, 'queued': 1, 'running': 0})
        self.assertEqual(self.store.get(task['id'])['attempts'], 0)
        with patch.object(self.app.extensions['youtube_login'], 'status', return_value={'active': True}):
            self.assertTrue(self.client.get('/api/clash').json['rotation_locked'])
            self.assertEqual(self.client.post('/api/clash/rotation', json=config, headers=self.headers).status_code, 409)

    def test_stop_persists_blocks_downloads_and_imports_but_not_checks(self):
        payload = {'config': {}, 'base': str(self.root)}
        download = self.store.create('download', {**payload, 'urls': ['https://example.test/one.mp4']})
        self.store.create('import', payload)
        response = self.client.post('/api/downloads/stop')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.store.downloads_paused())
        response = self.client.post('/api/downloads/stop', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['download_queue'], {'paused': True, 'running': 0, 'queued': 2})
        self.assertTrue(TaskStore(self.store.directory).downloads_paused())
        self.assertTrue(self.store.pending())
        self.assertFalse(self.store.pending(runnable=True))
        self.assertIsNone(self.store.claim('worker'))
        check = self.store.create('check', payload)
        self.assertEqual(self.store.claim('worker')['id'], check['id'])
        self.store.finish(check['id'], result={'message': 'done'}, worker_id='worker')
        self.assertEqual(self.client.post('/api/downloads/stop', headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post('/api/downloads/resume', headers=self.headers).status_code, 200)
        self.assertEqual(self.store.claim('worker')['id'], download['id'])

    def test_running_owners_are_fenced_and_manual_stop_does_not_consume_attempts_or_rotation(self):
        task = self.store.create('download', {'config': {}, 'base': str(self.root), 'urls': ['https://example.test/one.mp4']})
        self.store.claim('simulated-owner')
        self.store.update_progress(task['id'], {'rotation_id': 'generation', 'percent': 42, 'speed': 100}, 'simulated-owner')
        self.assertEqual(self.client.post('/api/downloads/stop', headers=self.headers).status_code, 202)
        self.assertEqual(self.client.post('/api/downloads/resume', headers=self.headers).status_code, 409)
        self.assertEqual(self.store.stop_running_downloads(lambda owner: True), 1)
        current = self.store.get(task['id'])
        self.assertEqual((current['status'], current['attempts'], current['progress']['percent']), ('queued', 0, 42))
        self.assertIsNone(current['progress']['speed'])
        self.assertFalse(self.store.finish(task['id'], error='old executor', worker_id='simulated-owner'))
        self.assertFalse(self.store.update_progress(task['id'], {}, 'simulated-owner'))
        self.assertEqual(self.store.rotation_completed('generation'), 0)
        self.assertEqual(self.store.counts(), {'queued': 1, 'running': 0, 'total': 1})
        self.client.post('/api/downloads/resume', headers=self.headers)
        self.store.claim('new-owner')
        self.store.finish(task['id'], result={'message': 'done'}, worker_id='new-owner')
        self.assertEqual(self.store.rotation_completed('generation'), 1)

    def test_resume_waits_until_old_executor_has_been_terminated(self):
        task = self.store.create('download', {'config': {}, 'base': str(self.root)})
        self.store.claim('owner')
        self.store.set_downloads_paused(True)
        entered, release = threading.Event(), threading.Event()

        def terminate(owner):
            entered.set()
            self.assertTrue(release.wait(10))
            return True

        with ThreadPoolExecutor(2) as pool:
            stopping = pool.submit(self.store.stop_running_downloads, terminate)
            self.assertTrue(entered.wait(5))
            resumed = pool.submit(self.store.set_downloads_paused, False)
            try:
                time.sleep(0.1)
                self.assertFalse(resumed.done())
            finally:
                release.set()
            self.assertEqual(stopping.result(timeout=10), 1)
            resumed.result(timeout=10)
        self.assertFalse(self.store.downloads_paused())
        self.assertEqual(self.store.claim('new-owner')['id'], task['id'])


class SQLiteControlTests(ControlContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_unrelated_process_is_never_terminated(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.store.create('download', {'config': {}, 'base': str(self.root)})
            self.store.claim(f'{socket.gethostname()}:{child.pid}:unrelated')
            response = self.client.post('/api/downloads/stop', headers=self.headers)
            self.assertEqual(response.status_code, 202)
            self.assertIsNone(child.poll())
        finally:
            child.kill()
            child.wait(timeout=5)


class PostgreSQLControlTests(test_scale.PostgreSQLFixture, ControlContract, unittest.TestCase):
    def test_two_real_download_workers_stop_and_resume_partial_files(self):
        body = b'0123456789abcdef' * (256 * 1024)
        ranges = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requested = self.headers.get('Range', '')
                start = int(re.match(r'bytes=(\d+)-', requested)[1]) if requested else 0
                if start:
                    ranges.append(start)
                self.send_response(206 if start else 200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(body) - start))
                if start:
                    self.send_header('Content-Range', f'bytes {start}-{len(body)-1}/{len(body)}')
                self.end_headers()
                try:
                    for offset in range(start, len(body), 65536):
                        self.wfile.write(body[offset:offset + 65536])
                        time.sleep(0.04)
                except (ConnectionError, OSError):
                    pass

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.store.set_runtime({'worker_count': 2, 'min_free_gb': 0})
        payload = {'config': {'path': 'videos', 'proxy_mode': 'direct', 'shard_downloads': True}, 'base': str(self.root)}
        created = self.store.create_batch([{**payload, 'urls': [f'http://127.0.0.1:{server.server_port}/{name}.mp4']}
                                         for name in ('one', 'two', 'three')])
        processes = []

        def launch(slot):
            child = subprocess.Popen([sys.executable, '-u', str(ROOT / 'web_worker.py'), '--state', str(self.store.directory),
                                      '--slot', str(slot), '--role', 'download'], cwd=ROOT,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(child)
            return child

        def cleanup():
            for child in processes:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
        self.addCleanup(cleanup)
        launch(0)
        launch(1)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            current = [self.store.get(item['id']) for item in created]
            if sum(item['status'] == 'running' and (item['progress'] or {}).get('downloaded_bytes', 0) > 0 for item in current) == 2:
                break
            time.sleep(0.05)
        else:
            self.fail('Both workers did not report actual file progress')
        response = self.client.post('/api/downloads/stop', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['download_queue'], {'paused': True, 'running': 0, 'queued': 3})
        self.assertTrue(all(child.poll() is not None for child in processes))
        partial = list((self.root / 'videos').rglob('*.part'))
        self.assertTrue(partial)
        self.assertTrue(all(path.stat().st_size > 0 for path in partial))
        paused_files = {str(path): path.stat().st_size for path in partial}
        self.assertEqual(launch(0).wait(timeout=15), 0)
        self.assertEqual({str(path): path.stat().st_size for path in partial}, paused_files)
        restarted = create_app(self.config, self.store.directory, start_worker=False)
        self.assertTrue(restarted.extensions['task_store'].downloads_paused())
        self.assertEqual(self.client.post('/api/downloads/resume', headers=self.headers).status_code, 200)
        resumed = [launch(0), launch(1)]
        for child in resumed:
            self.assertEqual(child.wait(timeout=45), 0)
        completed = [self.store.get(item['id']) for item in created]
        self.assertTrue(all(item['status'] == 'succeeded' for item in completed), completed)
        self.assertTrue(all(item['attempts'] == 1 for item in completed))
        files = list((self.root / 'videos').rglob('*.mp4'))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(path.read_bytes() == body for path in files))
        self.assertTrue(ranges, 'Resumed download should request a saved byte range')
