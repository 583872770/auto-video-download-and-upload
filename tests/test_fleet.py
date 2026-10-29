"""Multiple isolated instances share one queue, never machine-local configuration."""
from concurrent.futures import ThreadPoolExecutor
import http.server
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.serving import make_server

from app import create_app
from webapp.database import instances, settings, tasks
from webapp.store import TaskStore


class FleetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.url = 'sqlite:///' + (self.root / 'shared.sqlite3').as_posix()
        self.setup_fleet()

    def setup_fleet(self):
        self.apps = {}
        for identity, role, count in [('manager', 'manager', 1), ('alpha', 'worker', 2), ('beta', 'worker', 3)]:
            base = self.root / identity
            base.mkdir()
            config = base / 'config.yaml'
            config.write_text(f'path: videos-{identity}\nproxy_mode: direct\nworker_count: {count}\n'
                              'min_free_gb: 0\ndownload_method: downmp3\ndownmp3_quality: "720"\n', encoding='utf-8')
            app = create_app(config, base / 'state', start_worker=False, role=role,
                             instance_id=identity, instance_name=identity, database_url=self.url)
            app.testing = True
            self.apps[identity] = app
        self.manager = self.apps['manager'].extensions['task_store']
        self.a = self.apps['alpha'].extensions['task_store']
        self.b = self.apps['beta'].extensions['task_store']
        self.client = self.apps['manager'].test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]
        self.headers = {'X-CSRF-Token': token}
        self.a.fleet.control('alpha', {'action': 'resume'})
        self.b.fleet.control('beta', {'action': 'resume'})

    def queue(self, count=10):
        return [self.manager.create('download', {'base': 'Z:/manager-only',
            'urls': ['https://youtu.be/' + f'{number:011d}']}) for number in range(count)]

    def activate(self, limit=32):
        with self.manager.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'fleet_budget').values(value=str(limit)))
        self.manager.activate_download_lists()

    def serve(self, identity):
        app = self.apps[identity]
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            thread.join(timeout=3)
        self.addCleanup(cleanup)
        fleet = app.extensions['fleet']
        address = f'http://127.0.0.1:{server.server_port}'
        fleet.url = address
        with fleet.store.connect() as db:
            db.execute(instances.update().where(instances.c.id == identity).values(url=address))
        return address

    def test_global_and_instance_caps_are_atomic(self):
        self.queue()
        self.activate(limit=4)
        with ThreadPoolExecutor(8) as pool:
            found = list(pool.map(lambda store: store.claim(store.owner), [self.a, self.b] * 4))
        claimed = [item for item in found if item]
        self.assertEqual(len(claimed), 4)
        self.assertEqual(len({item['id'] for item in claimed}), 4)
        self.assertLessEqual(sum(item['instance_id'] == 'alpha' for item in claimed), 2)
        self.assertLessEqual(sum(item['instance_id'] == 'beta' for item in claimed), 3)
        self.assertIsNone(self.manager.claim())
        # A full cluster must not repeatedly launch subprocesses just to reject claims.
        self.assertNotIn('download', self.a.runnable_counts())
        self.assertNotIn('download', self.b.runnable_counts())

    def test_auto_pipeline_has_bounded_extra_preparation_and_zero_global_limit(self):
        from webapp.throughput import _atomic, policy_key
        self.queue(12)
        self.activate(limit=0)
        self.a.fleet.save_policy({'throughput_mode': 'auto', 'worker_count': 2, 'yt5s_browser_count': 1})
        config = self.a.fleet.local_settings()['config']
        _atomic(self.a.directory / 'throughput.json', {'time': time.time(), 'policy': policy_key(config),
                'transfers': 2, 'workers': 4, 'browsers': 1})
        with ThreadPoolExecutor(8) as pool:
            found = list(pool.map(lambda store: store.claim(store.owner), [self.a, self.b] * 4))
        claimed = [item for item in found if item]
        self.assertEqual(len(claimed), 7)
        self.assertEqual(sum(item['instance_id'] == 'alpha' for item in claimed), 4)
        self.assertEqual(len({item['id'] for item in claimed}), 7)
        self.assertIsNone(self.a.claim())
        # A configured cluster cap still includes preparation tasks.
        with self.manager.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'fleet_budget').values(value='3'))
        self.assertIsNone(self.a.claim())
        self.assertIsNone(self.b.claim())

    def test_machine_paths_and_strategy_versions_are_isolated(self):
        self.queue(3)
        version = self.manager.fleet.policy()['version']
        self.activate()
        first = self.a.claim(self.a.owner)
        self.assertEqual(first['payload']['base'], str(self.root / 'alpha'))
        self.assertEqual(first['payload']['config']['path'], 'videos-alpha')
        self.assertEqual(first['policy_version'], version)
        self.b.fleet.save_policy({'download_method': 'downmp3', 'downmp3_quality': '1080'})
        second = self.b.claim(self.b.owner)
        self.assertEqual(second['payload']['config']['path'], 'videos-beta')
        self.assertEqual(second['payload']['config']['downmp3_quality'], '1080')
        self.assertEqual(second['policy_version'], version + 1)
        third = self.b.claim(self.b.owner)
        self.assertEqual(third['payload']['config']['downmp3_quality'], '1080')
        self.assertEqual(self.a.get(first['id'], private=True)['payload']['config']['downmp3_quality'], '720')

    def test_worker_registration_does_not_overwrite_policy_or_other_limits(self):
        self.a.fleet.control('alpha', {'worker_count': 1})
        self.assertEqual(self.a.runtime()['worker_count'], 1)
        self.assertEqual(self.b.runtime()['worker_count'], 3)
        self.assertEqual(self.manager.fleet.policy()['config']['downmp3_quality'], '720')
        self.a.fleet.save_local({'path': 'other-local', 'worker_count': 16}, self.root / 'alpha', self.root / 'alpha/clash')
        self.assertEqual(self.a.runtime()['worker_count'], 1)
        self.assertEqual(self.manager.fleet.policy()['config']['downmp3_quality'], '720')

    def test_drain_blocks_only_selected_instance(self):
        self.queue()
        self.activate()
        first = self.a.claim(self.a.owner)
        self.manager.fleet.control('alpha', {'action': 'drain'})
        self.assertIsNone(self.a.claim(self.a.owner))
        self.assertIsNotNone(self.b.claim(self.b.owner))
        self.assertTrue(self.a.finish(first['id'], result={}, worker_id=self.a.owner))
        self.assertFalse(self.manager.downloads_paused())

    def test_expired_attempt_cannot_report_or_bill_after_takeover(self):
        jobs = self.queue(1)
        self.activate()
        old = self.a.claim(self.a.owner)
        with self.manager.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == old['id']).values(lease_until=time.time() - 1))
        self.assertFalse(self.a.update_progress(old['id'], {'stage': 'downloading'}, self.a.owner))
        self.assertFalse(self.a.heartbeat(old['id'], self.a.owner))
        self.manager.recover(expired_only=True)
        with self.manager.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == old['id']).values(available_at=0))
        new = self.b.claim(self.b.owner)
        self.assertEqual(new['id'], jobs[0]['id'])
        self.assertTrue(new['payload']['_restart_transfer'])
        self.assertFalse(self.a.finish(old['id'], result={}, worker_id=self.a.owner))
        self.assertFalse(self.a.heartbeat(old['id'], self.a.owner))
        self.assertTrue(self.b.finish(new['id'], result={}, worker_id=self.b.owner))

    def test_billing_is_idempotent_across_hosts_and_same_path_names_do_not_collide(self):
        self.queue(2)
        self.activate()
        first = self.a.claim(self.a.owner)
        path = self.root / 'video.mp4'
        path.write_bytes(b'first')
        self.assertTrue(self.a.statistics.record_file(first['id'], path))
        self.assertFalse(self.a.statistics.record_file(first['id'], path))
        second = self.b.claim(self.b.owner)
        self.assertTrue(self.b.statistics.record_file(second['id'], path))
        self.assertEqual(self.manager.statistics.dashboard()['file_count'], 2)

    def test_media_namespaces_and_http_ranges(self):
        self.serve('alpha')
        self.serve('beta')
        shared = self.root / 'same-path'
        shared.mkdir()
        video = shared / 'video.mp4'
        video.write_bytes(b'0123456789')
        a = self.apps['alpha'].extensions['media_catalog']
        b = self.apps['beta'].extensions['media_catalog']
        a.index_file(video, shared)
        b.index_file(video, shared)
        self.assertNotEqual(a.page(shared)['items'][0]['id'], b.page(shared)['items'][0]['id'])
        a.reindex(self.root / 'empty')
        self.assertEqual(b.page(shared)['total'], 1)
        local = self.root / 'alpha/videos-alpha'
        local.mkdir()
        (local / 'video.mp4').write_bytes(b'0123456789')
        response = self.client.get('/media/videos/video.mp4?instance_id=alpha', headers={'Range': 'bytes=2-5'})
        self.assertEqual(response.status_code, 206, response.text)
        self.assertEqual(response.data, b'2345')
        self.assertEqual(response.headers['Content-Range'], 'bytes 2-5/10')
        response.close()

    def test_overview_uses_workers_current_directories_without_interpreting_remote_paths(self):
        catalog = self.apps['alpha'].extensions['media_catalog']
        old = self.root / 'alpha/videos-alpha'
        old.mkdir()
        (old / 'old.mp4').write_bytes(b'old')
        catalog.reindex(old)
        self.assertEqual(self.client.get('/api/overview').json['media']['count'], 1)
        new = self.root / 'alpha/new'
        new.mkdir()
        (new / 'new.mp4').write_bytes(b'new')
        catalog.reindex(new)
        self.a.fleet.save_local({'path': 'new', 'worker_count': 1}, self.root / 'alpha', self.root / 'alpha/clash')
        response = self.client.get('/api/overview').json
        self.assertEqual(response['media']['count'], 1)
        self.assertEqual([item['name'] for item in response['recent_media']], ['new.mp4'])
        self.assertEqual(response['recent_media'][0]['instance_id'], 'alpha')

    def test_agent_authentication_and_private_fields(self):
        app = self.apps['alpha']
        self.assertEqual(app.test_client().get('/api/config').status_code, 200)  # Former workers also have a local console.
        self.assertEqual(app.test_client().get('/health').status_code, 200)
        self.assertEqual(app.test_client().get('/api/config', headers={'Authorization': 'Bearer wrong'}).status_code, 403)
        response = self.client.get('/api/instances')
        self.assertEqual(response.status_code, 200)
        for row in response.json['items']:
            self.assertNotIn('token', row)
            self.assertNotIn('config', row)

    def test_manager_settings_target_only_one_worker(self):
        self.serve('alpha')
        result = self.client.put('/api/config', json={'path': 'new-alpha', 'download_methods': ['downmp3']},
                                 headers={**self.headers, 'X-Instance-ID': 'alpha'})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.a.fleet.local_settings()['config']['path'], 'new-alpha')
        self.assertEqual(self.b.fleet.local_settings()['config']['path'], 'videos-beta')

    def test_download_node_auth_failure_does_not_expire_management_login(self):
        manager = self.apps['manager']
        response = self.client.post('/api/auth/setup', headers=self.headers, json={
            'username':'admin', 'password':'fixture-manager-password',
            'bootstrap_key':manager.extensions['console_auth'].bootstrap_key})
        self.assertEqual(response.status_code, 201, response.json)
        # Simulate a worker URL incorrectly pointing back to the manager's login-protected API.
        address = self.serve('manager')
        with self.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(url=address))
        response = self.client.get('/api/config', headers={'X-Instance-ID':'alpha'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('拒绝了实例认证', response.json['error'])
        self.assertNotIn('请先登录管理账户', response.json['error'])
        response = self.client.get('/api/status', headers={'X-Instance-ID':'alpha'})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json['error_type'], 'instance_auth')
        self.assertEqual(response.json['instance_id'], 'alpha')
        self.assertEqual(self.client.get('/api/auth/status').json['user']['username'], 'admin')
        self.assertEqual(self.client.get('/api/jobs').status_code, 200)
        self.assertEqual(self.client.get('/api/overview').status_code, 200)
        # Correctly addressed workers continue to use the node credential, not browser cookies.
        self.serve('alpha')
        self.assertEqual(self.client.get('/api/config', headers={'X-Instance-ID':'alpha'}).status_code, 200)
        self.assertEqual(self.client.get('/api/status', headers={'X-Instance-ID':'alpha'}).status_code, 200)

    def test_another_machine_loopback_address_is_reported_before_sending_node_credential(self):
        with self.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(
                url='http://127.0.0.1:5000', host='another-machine'))
        with patch('requests.Session.request', side_effect=AssertionError('must not send credential')):
            response = self.client.get('/api/config', headers={'X-Instance-ID':'alpha'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('本机回环地址', response.json['error'])
        self.assertIn('/setup', response.json['error'])
        self.assertEqual(self.client.get('/api/overview').status_code, 200)

    def test_cookie_upload_streams_to_selected_worker_only(self):
        self.serve('beta')
        cookie = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-only-secret\n'
        response = self.client.post('/api/youtube/cookie', data={'cookie': (BytesIO(cookie), 'cookies.txt')},
                                    headers={**self.headers, 'X-Instance-ID': 'beta'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue((self.root / 'beta/Cookies/youtube.txt').is_file())
        self.assertFalse((self.root / 'alpha/Cookies/youtube.txt').exists())
        self.assertFalse((self.root / 'manager/Cookies/youtube.txt').exists())
        self.assertEqual(self.b.fleet.local_settings()['config']['youtube_cookie'], 'Cookies/youtube.txt')

    def test_apply_connection_is_proxied_and_scoped_to_one_worker(self):
        self.serve('beta')
        self.queue(1)
        self.manager.set_downloads_paused(True)
        response = self.client.post('/api/downloads/connection', headers={**self.headers, 'X-Instance-ID': 'beta'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(self.b.download_connection())
        self.assertIsNone(self.a.download_connection())
        self.assertIsNone(self.manager.download_connection())

    def test_import_affinity_and_legacy_claims_are_blocked(self):
        job = self.manager.create('import', {'base': str(self.root / 'manager'), 'import_id': 'fixture'})
        self.assertIsNone(self.a.claim(actions=('import',)))
        self.assertEqual(self.manager.claim(actions=('import',))['id'], job['id'])
        standalone = TaskStore(self.root / 'legacy', self.url)
        self.queue(1)
        self.activate()
        self.assertIsNone(standalone.claim())

    def wait_startup(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.client.get('/api/downloads/startup').json
            if result['state'] != 'running':
                return result
            time.sleep(0.02)
        self.fail('Cluster startup timed out')

    def test_manager_preflights_workers_and_resume_uses_same_startup(self):
        self.serve('alpha')
        self.serve('beta')
        self.queue(3)
        with patch('webapp.environment.require_downmp3_environment') as check:
            response = self.client.post('/api/downloads/start', headers=self.headers)
            self.assertEqual(response.status_code, 202)
            result = self.wait_startup()
            self.assertEqual(result['state'], 'succeeded', result)
            self.assertEqual(check.call_count, 2)
            stages = {step['stage'] for step in result['steps']}
            self.assertTrue({'instance_ready:alpha', 'instance_ready:beta'} <= stages)
            self.client.post('/api/downloads/stop', headers=self.headers)
            self.assertTrue(self.manager.downloads_paused())
            self.client.post('/api/downloads/resume', headers=self.headers)
            self.assertEqual(self.wait_startup()['state'], 'succeeded')

    def test_failed_preflight_keeps_queue_paused(self):
        self.serve('alpha')
        self.serve('beta')
        self.queue(1)
        self.manager.set_downloads_paused(True)
        with patch('webapp.environment.require_downmp3_environment', side_effect=ValueError('fixture missing tools')):
            self.client.post('/api/downloads/start', headers=self.headers)
            result = self.wait_startup()
        self.assertEqual(result['state'], 'failed', result)
        self.assertTrue(self.manager.downloads_paused())
        self.assertIsNone(self.a.claim())

    def test_node_stop_and_status_do_not_affect_another_node(self):
        self.queue(3)
        self.activate()
        first, second = self.a.claim(self.a.owner), self.b.claim(self.b.owner)
        self.a.worker_status(self.a.owner, '0', 'paused', first['id'], 'alpha disk')
        self.b.worker_status(self.b.owner, '0', 'running', second['id'])
        self.assertEqual(len(self.manager.workers()), 0)  # Worker status is local to each unified instance.
        self.assertEqual(len(self.a.workers()), 1)
        self.manager.fleet.control('alpha', {'action': 'stop'})
        killed = []
        self.assertEqual(self.a.stop_running_downloads(lambda owner: killed.append(owner) or True), 1)
        self.assertEqual(killed, [self.a.owner])
        self.assertEqual(self.b.get(second['id'])['status'], 'running')
        self.assertEqual(self.a.get(first['id'])['status'], 'queued')
        self.assertIsNone(self.a.claim())

    def test_boot_identity_fences_old_executors_and_errors_survive_heartbeat(self):
        from webapp.fleet import Fleet
        with self.assertRaisesRegex(ValueError, '实例 ID 已在线'):
            duplicate = Fleet(self.b, 'worker', identity='alpha')
            duplicate.initialize({}, self.root, self.root / 'clash')
        self.b.fleet = self.apps['beta'].extensions['fleet']
        self.a.fleet.beat('fixture cookie error')
        self.a.fleet.beat()
        self.assertEqual(self.a.fleet.row()['error'], 'fixture cookie error')
        self.a.fleet.control('alpha', {'action': 'resume'})
        self.assertEqual(self.a.fleet.row()['error'], '')
        self.queue(1)
        self.activate()
        job = self.a.claim(self.a.owner)
        with self.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(boot_id='replacement'))
        self.assertFalse(self.a.heartbeat(job['id'], self.a.owner))
        self.assertFalse(self.a.update_progress(job['id'], {}, self.a.owner))
        self.assertFalse(self.a.finish(job['id'], result={}, worker_id=self.a.owner))

    def test_container_pid_reuse_waits_for_heartbeat_expiry_before_registration(self):
        from webapp.fleet import Fleet, InstanceIdentityConflict, ONLINE_SECONDS
        old = self.a.fleet.row()
        replacement = Fleet(self.b, 'worker', identity='alpha')
        with patch('psutil.pid_exists', return_value=True), \
                patch.object(replacement, 'clock', return_value=old['heartbeat'] + 2):
            with self.assertRaises(InstanceIdentityConflict) as raised:
                replacement.initialize({}, self.root, self.root / 'clash')
        self.assertEqual(raised.exception.identity, 'alpha')
        self.assertEqual(raised.exception.host, old['host'])
        self.assertEqual(raised.exception.retry_after, ONLINE_SECONDS - 2)
        self.assertEqual(self.a.fleet.row()['boot_id'], old['boot_id'])
        # If the old container is actually alive, its renewed heartbeat prevents takeover.
        renewed = old['heartbeat'] + ONLINE_SECONDS - 1
        with self.a.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(heartbeat=renewed))
        with patch('psutil.pid_exists', return_value=True), \
                patch.object(replacement, 'clock', return_value=old['heartbeat'] + ONLINE_SECONDS + 1):
            with self.assertRaises(InstanceIdentityConflict):
                replacement.initialize({}, self.root, self.root / 'clash')
        self.assertEqual(self.a.fleet.row()['boot_id'], old['boot_id'])
        with patch('psutil.pid_exists', return_value=True), \
                patch.object(replacement, 'clock', return_value=renewed + ONLINE_SECONDS + 1):
            replacement.initialize({}, self.root, self.root / 'clash')
        self.assertEqual(replacement.row()['boot_id'], replacement.boot_id)
        self.assertFalse(self.a.fleet.beat())  # The old process cannot reclaim the new boot.

    def test_real_worker_subprocesses_download_into_their_own_directories(self):
        # Exercise runtime attachment and SQL connections in fresh interpreters,
        # using only a local fixture server; no external provider/production DB.
        content = b'local-video-fixture' * 32768
        transfers = threading.Barrier(2)
        class Video(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                transfers.wait(timeout=15)
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                for start in range(0, len(content), 32768):
                    try:
                        self.wfile.write(content[start:start + 32768])
                    except (BrokenPipeError, ConnectionResetError):
                        return  # Generic extractor may close its sniffing request early.
                    time.sleep(0.01)
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Video)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.addCleanup(close_server)
        for store in (self.a, self.b):
            store.fleet.save_policy({'download_methods': ['yt_dlp'], 'download_method': 'yt_dlp', 'download_format': 'best'})
            store.fleet.control(store.fleet.id, {'worker_count': 1})
        self.manager.create('download', {'urls': [f'http://127.0.0.1:{server.server_port}/0.mp4']})
        self.manager.create('download', {'urls': [f'http://127.0.0.1:{server.server_port}/1.mp4']})
        self.activate(limit=2)
        processes = []
        from webapp.control import ROOT
        def cleanup():
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        self.addCleanup(cleanup)
        for store in (self.a, self.b):
            process = subprocess.Popen([sys.executable, str(ROOT / 'web_worker.py'), '--state', str(store.directory),
                '--slot', '0', '--role', 'download'], cwd=ROOT, env=dict(os.environ, VIDEO_WORKER_DATABASE_URL=self.url,
                PYTHONUTF8='1'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
            processes.append(process)
        for process in processes:
            self.assertEqual(process.wait(timeout=40), 0)
        jobs = self.manager.page(actions=('download',))['items']
        self.assertEqual([item['status'] for item in jobs], ['succeeded', 'succeeded'],
                         [(item['error'], self.manager.logs(item['id'])) for item in jobs])
        self.assertEqual({item['instance_id'] for item in jobs}, {'alpha', 'beta'})
        for identity in ('alpha', 'beta'):
            files = list((self.root / identity / f'videos-{identity}').rglob('*.mp4'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_size, len(content))
        self.assertEqual(self.manager.statistics.dashboard()['file_bytes'], len(content) * 2)


if __name__ == '__main__':
    unittest.main()
