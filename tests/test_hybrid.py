"""A shared cluster in which every node can manage and execute downloads."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from tests import test_fleet
from webapp.database import instances, settings
from webapp.store import TaskStore


class HybridTests(unittest.TestCase):
    serve = test_fleet.FleetTests.serve

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.url = 'sqlite:///' + (self.root / 'shared.sqlite3').as_posix()
        self.apps, self.clients, self.headers = {}, {}, {}
        for identity in ('alpha', 'beta'):
            base = self.root / identity
            base.mkdir()
            config = base / 'config.yaml'
            config.write_text(f'path: videos-{identity}\nproxy_mode: direct\nworker_count: 2\n'
                              'min_free_gb: 0\ndownload_method: downmp3\n', encoding='utf-8')
            app = create_app(config, base / 'state', start_worker=False, role='hybrid',
                             instance_id=identity, database_url=self.url)
            app.testing = True
            self.apps[identity] = app
            client = app.test_client()
            self.clients[identity] = client
            response = client.get('/')
            self.assertEqual(response.status_code, 200)
            token = re.search(r'name="csrf-token" content="([^"]+)"', response.text)[1]
            self.headers[identity] = {'X-CSRF-Token': token}
            self.serve(identity)
        self.a = self.apps['alpha'].extensions['task_store']
        self.b = self.apps['beta'].extensions['task_store']

    def test_browser_and_authenticated_local_api_have_separate_guards(self):
        client = self.clients['alpha']
        self.assertEqual(client.get('/jobs').status_code, 200)
        self.assertEqual(client.get('/api/instances').status_code, 200)
        self.assertEqual(client.put('/api/instances/limits', json={'global_limit': 3}).status_code, 403)
        self.assertEqual(client.get('/api/config', headers={'Authorization': 'Bearer wrong'}).status_code, 403)
        # A peer's local request must not recurse or follow an instance selection.
        result = client.get('/api/config', headers={'Authorization': 'Bearer ' + self.a.fleet.token,
                                                   'X-Instance-ID': 'beta'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['config']['path'], 'videos-alpha')

    def test_local_default_and_remote_settings_are_isolated(self):
        client = self.clients['alpha']
        self.assertEqual(client.get('/api/config').json['config']['path'], 'videos-alpha')
        response = client.put('/api/config', json={'path': 'alpha-new', 'worker_count': 1},
                              headers=self.headers['alpha'])
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.a.fleet.local_settings()['config']['path'], 'alpha-new')
        self.assertEqual(self.a.runtime()['worker_count'], 1)
        self.assertEqual(self.b.fleet.local_settings()['config']['path'], 'videos-beta')
        response = client.put('/api/config', json={'path': 'beta-new'},
                              headers={**self.headers['alpha'], 'X-Instance-ID': 'beta'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.b.fleet.local_settings()['config']['path'], 'beta-new')
        self.assertEqual(self.a.fleet.local_settings()['config']['path'], 'alpha-new')
        response = self.clients['beta'].put('/api/config', json={'downmp3_quality': '1080'},
                                           headers=self.headers['beta'])
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.a.fleet.policy()['config']['downmp3_quality'], 'best')
        self.assertEqual(self.b.fleet.policy()['config']['downmp3_quality'], '1080')

    def test_cookie_and_media_can_be_accessed_from_either_management_page(self):
        cookie = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tfixture\n'
        response = self.clients['alpha'].post('/api/youtube/cookie',
            data={'cookie': (BytesIO(cookie), 'cookies.txt')}, headers={**self.headers['alpha'], 'X-Instance-ID': 'beta'})
        self.assertEqual(response.status_code, 201, response.json)
        self.assertFalse((self.root / 'alpha/Cookies/youtube.txt').exists())
        self.assertTrue((self.root / 'beta/Cookies/youtube.txt').exists())
        for identity in ('alpha', 'beta'):
            root = self.root / identity / f'videos-{identity}'
            root.mkdir()
            (root / 'video.mp4').write_bytes(identity.encode() * 20)
            self.apps[identity].extensions['media_catalog'].reindex(root)
        for origin in ('alpha', 'beta'):
            for target in ('alpha', 'beta'):
                result = self.clients[origin].get('/media/videos/video.mp4?instance_id=' + target,
                                                  headers={'Range': 'bytes=0-3'})
                self.assertEqual(result.status_code, 206)
                self.assertEqual(result.data, target.encode()[:4])
        result = self.clients['alpha'].get('/api/overview')
        self.assertEqual(result.status_code, 200, result.json)

    def test_both_nodes_claim_once_under_shared_limit_and_pause_is_local(self):
        for store in (self.a, self.b):
            store.fleet.control(store.fleet.id, {'action': 'resume'})
        for number in range(6):
            self.a.create('download', {'urls': ['https://youtu.be/' + f'{number:011d}']})
        with self.a.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'fleet_budget').values(value='3'))
        self.a.activate_download_lists()
        with ThreadPoolExecutor(6) as pool:
            claimed = [item for item in pool.map(lambda store: store.claim(), [self.a, self.b] * 3) if item]
        self.assertEqual(len(claimed), 3)
        self.assertEqual(len({item['id'] for item in claimed}), 3)
        self.assertEqual({item['instance_id'] for item in claimed}, {'alpha', 'beta'})
        for item in claimed:
            store = self.a if item['instance_id'] == 'alpha' else self.b
            self.assertEqual(item['payload']['base'], str(self.root / item['instance_id']))
            store.finish(item['id'], result={}, worker_id=store.owner)
        self.a.fleet.control('alpha', {'action': 'drain'})
        self.assertIsNone(self.a.claim())
        self.assertIsNotNone(self.b.claim())
        self.assertEqual(self.clients['alpha'].get('/api/instances').status_code, 200)
        self.assertFalse(self.a.downloads_paused())

    def test_local_management_and_background_preflight_do_not_call_own_http_server(self):
        for store in (self.a, self.b):
            store.fleet.control(store.fleet.id, {'action': 'resume'})
        self.a.create('download', {'urls': ['https://youtu.be/npNnbj46_ik']})
        # Local reads and writes must work even if all HTTP worker threads are busy.
        with patch('requests.Session.request', side_effect=AssertionError('self HTTP call')):
            self.assertEqual(self.clients['alpha'].get('/api/config').status_code, 200)
            response = self.clients['alpha'].put('/api/config', json={'path': 'alpha-new'},
                                                 headers=self.headers['alpha'])
            self.assertEqual(response.status_code, 200, response.json)
        self.client = self.clients['alpha']
        with patch('webapp.environment.require_downmp3_environment') as check:
            response = self.client.post('/api/downloads/start', headers=self.headers['alpha'])
            self.assertEqual(response.status_code, 202, response.json)
            result = test_fleet.FleetTests.wait_startup(self)
            self.assertEqual(result['state'], 'succeeded', result)
            self.assertEqual(check.call_count, 2)

    def test_job_created_on_one_page_executes_and_is_billed_on_shared_task(self):
        for store in (self.a, self.b):
            store.fleet.control(store.fleet.id, {'action': 'resume'})
        response = self.clients['alpha'].post('/api/jobs', json={'customer': 'Client', 'name': 'Shared job',
            'urls': 'npNnbj46_ik\n2Gu7q-KUXCk', 'price': '10'},
            headers=self.headers['alpha'])
        self.assertEqual(response.status_code, 201, response.json)
        identity = response.json['id']
        self.a.set_downloads_paused(False)
        with patch('webapp.environment.require_downmp3_environment'):
            response = self.clients['beta'].post(f'/api/jobs/{identity}/control', json={'action': 'start'},
                                                headers=self.headers['beta'])
        self.assertEqual(response.status_code, 200, response.json)
        for store in (self.a, self.b):
            task = store.claim()
            self.assertIsNotNone(task)
            self.assertEqual(task['batch_id'], identity)
            path = self.root / (task['id'] + '.mp4')
            path.write_bytes(b'v' * 1024)
            self.assertTrue(store.statistics.record_file(task['id'], path))
            self.assertFalse(store.statistics.record_file(task['id'], path))
            self.assertTrue(store.finish(task['id'], result={}, worker_id=store.owner))
        left = self.clients['alpha'].get('/api/jobs/' + identity).json
        right = self.clients['beta'].get('/api/jobs/' + identity).json
        self.assertEqual(left, right)
        self.assertEqual(left['succeeded'], 2)
        self.assertEqual(left['file_bytes'], 2048)

    def test_imports_stay_with_origin_and_subprocess_restores_both_capabilities(self):
        first = self.a.create('import', {'base': str(self.root / 'alpha')})
        second = self.b.create('import', {'base': str(self.root / 'beta')})
        self.assertEqual(self.a.claim(actions=('import',))['id'], first['id'])
        self.assertIsNone(self.a.claim(actions=('import',)))
        self.assertEqual(self.b.claim(actions=('import',))['id'], second['id'])
        child = TaskStore(self.a.directory, self.url)
        self.assertTrue(child.fleet.can_download)
        self.assertTrue(child.fleet.can_manage)
        self.assertEqual(child.fleet.id, 'alpha')
        child.db.engine.dispose()

    def test_joining_or_restarting_does_not_copy_peer_policy(self):
        self.a.fleet.save_policy({'download_methods': ['downmp3'], 'downmp3_quality': '1080'})
        with self.a.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'beta').values(heartbeat=0))
        restarted = create_app(self.root / 'beta/config.yaml', self.b.directory, start_worker=False,
            role='hybrid', instance_id='beta', database_url=self.url)
        self.assertEqual(restarted.extensions['fleet'].policy()['config']['downmp3_quality'], 'best')
        self.assertEqual(self.a.fleet.policy()['config']['downmp3_quality'], '1080')

    def test_pure_manager_becomes_a_paused_download_node(self):
        with self.b.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'beta').values(role='manager', paused=0, heartbeat=0))
        restarted = create_app(self.root / 'beta/config.yaml', self.b.directory, start_worker=False,
            role='hybrid', instance_id='beta', database_url=self.url)
        self.assertTrue(restarted.extensions['fleet'].row()['paused'])


if __name__ == '__main__':
    unittest.main()
