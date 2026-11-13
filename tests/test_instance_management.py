"""Every console can manage every peer without borrowing local state."""
from io import BytesIO
import re
import unittest
from unittest.mock import patch

from tests import test_fleet
from tests.account_cookie_fixture import AccountCookieProfiles as CookieProfiles


class InstanceManagementTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet
    serve = test_fleet.FleetTests.serve

    def clients(self):
        result = {}
        for identity, app in self.apps.items():
            self.serve(identity)
            client = app.test_client()
            token = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text)[1]
            result[identity] = (client, {'X-CSRF-Token': token})
        return result

    def test_configuration_roundtrip_and_paths_from_every_entry(self):
        for source, (client, headers) in self.clients().items():
            for target in self.apps:
                with self.subTest(source=source, target=target):
                    selected = {**headers, 'X-Instance-ID': target}
                    saved = {'path': f'{source}-to-{target}', 'download_methods': ['downmp3'],
                             'postprocess_limit': 3, 'disk_log_budget_mb': 72,
                             'youtube_cookie_pool_start_interval': 7,
                             'db_budget_mode': 'manual', 'db_connection_budget': 60 if target == 'alpha' else 64}
                    before = {name: app.extensions['fleet'].local_settings()['config'].copy()
                              for name, app in self.apps.items()}
                    response = client.put('/api/config', json=saved, headers=selected)
                    self.assertEqual(response.status_code, 200, response.json)
                    response = client.get('/api/config', headers=selected)
                    self.assertEqual(response.status_code, 200, response.json)
                    for key, value in saved.items():
                        self.assertEqual(response.json['config'][key], value)
                    self.assertEqual(response.json['database_budget']['requested_total'], saved['db_connection_budget'])
                    self.assertEqual(response.json['config_path'], str(self.root / target / 'config.yaml'))
                    self.assertEqual(response.json['media_directory'], str(self.root / target / saved['path']))
                    for other, app in self.apps.items():
                        if other != target:
                            self.assertEqual(app.extensions['fleet'].local_settings()['config'], before[other])

    def test_cookie_library_timeline_export_and_delete_from_every_entry(self):
        clients = self.clients()
        for source, (client, headers) in clients.items():
            for target, app in self.apps.items():
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    selected = {**headers, 'X-Instance-ID': target}
                    cookie = ('# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\t'
                              f'fixture-{source}-{target}\n').encode()
                    response = client.post('/api/youtube/cookies', headers=selected,
                        data={'name': f'{source}-{target}', 'cookie': (BytesIO(cookie), 'cookies.txt')})
                    self.assertEqual(response.status_code, 201, response.json)
                    library = CookieProfiles(self.root / target, app.extensions['task_store'])
                    item = next(i for i in library.items() if i['name'] == f'{source}-{target}')
                    response = client.get(f'/api/youtube/cookies/{item["id"]}/timeline?limit=3', headers=selected)
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(response.json['profile']['instance_id'], target)
                    response = client.post('/api/youtube/cookies/export', json={'id': item['id']}, headers=selected)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(f'fixture-{source}-{target}'.encode(), response.data)
                    response.close()
                    response = client.post('/api/youtube/cookies/delete', json={'id': item['id']}, headers=selected)
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertFalse(library.items())

    def test_disk_cleanup_and_proxy_operations_use_selected_instance(self):
        for source, (client, headers) in self.clients().items():
            for target, app in self.apps.items():
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    selected = {**headers, 'X-Instance-ID': target}
                    with patch('webapp.disk_budget.DiskBudget.cleanup', return_value={'checked': True}) as cleanup:
                        response = client.post('/api/disk/cleanup', headers=selected)
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertIs(cleanup.call_args.args[0], app.extensions['task_store'])
                    with patch.object(app.extensions['clash'], 'test_youtube', return_value={'instance': target}) as probe:
                        response = client.post('/api/clash/test', headers=selected)
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(response.json['instance'], target)
                    probe.assert_called_once()

    def test_missing_or_offline_target_never_changes_entry_configuration(self):
        client, headers = self.clients()['alpha']
        from sqlalchemy import update
        from webapp.database import instances
        with self.manager.connect() as db:
            db.execute(update(instances).where(instances.c.id == 'beta').values(heartbeat=0))
        original = (self.root / 'alpha/config.yaml').read_bytes()
        for target in ('beta', 'missing'):
            for path, payload in (('/api/config', {'path': 'must-not-save'}),
                                  ('/api/youtube/cookies/delete', {'id': '0' * 32})):
                response = client.open(path, method='PUT' if path == '/api/config' else 'POST',
                                       headers={**headers, 'X-Instance-ID': target}, json=payload)
                self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual((self.root / 'alpha/config.yaml').read_bytes(), original)

    def test_shared_accounts_cannot_bypass_roles_by_switching_entry_or_target(self):
        clients = self.clients()
        admin, headers = clients['alpha']
        response = admin.post('/api/auth/setup', headers=headers, json={
            'username': 'admin', 'password': 'fixture-admin-password',
            'bootstrap_key': self.apps['alpha'].extensions['console_auth'].bootstrap_key})
        self.assertEqual(response.status_code, 201, response.json)
        headers = {'X-CSRF-Token': re.search(r'name="csrf-token" content="([^"]+)"', admin.get('/').text)[1]}
        for role in ('viewer', 'operator'):
            response = admin.post('/api/auth/users', headers=headers, json={
                'username': role, 'password': 'fixture-role-password', 'role': role})
            self.assertEqual(response.status_code, 201, response.json)
            for source, app in self.apps.items():
                client = app.test_client()
                token = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/login').text)[1]
                response = client.post('/api/auth/login', headers={'X-CSRF-Token': token}, json={
                    'username': role, 'password': 'fixture-role-password'})
                self.assertEqual(response.status_code, 200, response.json)
                token = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text)[1]
                for target in self.apps:
                    with self.subTest(source=source, target=target, role=role):
                        selected = {'X-CSRF-Token': token, 'X-Instance-ID': target}
                        self.assertEqual(client.get('/api/config', headers=selected).status_code, 200)
                        self.assertEqual(client.put('/api/config', headers=selected,
                            json={'path': 'forbidden-role-change'}).status_code, 403)
                        self.assertEqual(client.post('/api/youtube/cookies/delete', headers=selected,
                            json={'id': '0' * 32}).status_code, 403)
                        self.assertEqual(client.get('/api/instance-deployment', headers=selected).status_code, 403)

    def test_timeline_rpc_allowlist_is_read_only_and_scoped(self):
        from webapp.console_auth import rpc_allowed
        app = self.apps['alpha']
        for identity in ('a' * 32, 'legacy'):
            for method in ('GET', 'HEAD', 'POST', 'PUT', 'DELETE'):
                with app.test_request_context(f'/api/youtube/cookies/{identity}/timeline', method=method):
                    self.assertEqual(rpc_allowed(), method in ('GET', 'HEAD'))
        for path in ('/api/youtube/cookies/invalid/timeline', '/api/auth/users', '/api/jobs', '/api/deployment'):
            with app.test_request_context(path):
                self.assertFalse(rpc_allowed())

    def test_connection_check_does_not_wait_for_another_instances_downloads(self):
        from tests.test_jobs import JobTests
        from webapp.database import instances, settings
        from webapp.workbench import Workbench
        clients = self.clients()
        client, headers = clients['alpha']
        self.client, self.headers = client, headers
        job = JobTests.create(self, urls='npNnbj46_ik\n2Gu7q-KUXCk')
        with patch('webapp.fleet_routes.rpc', return_value={'ready': True}):
            for target in ('alpha', 'beta'):
                response = client.post('/api/jobs/workbench', headers={**headers, 'X-Instance-ID': target},
                    json={'action': 'start', 'job_id': job})
                self.assertEqual(response.status_code, 200, response.json)
        task = self.a.claim(self.a.owner)
        self.assertIsNotNone(task)
        Workbench(self.b).control('beta', 'pause')
        with self.manager.connect() as db:
            # The legacy global switch is running while only beta is paused.
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='false'))
        selected = {**headers, 'X-Instance-ID': 'beta'}
        response = client.get('/api/clash', headers=selected)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['connection_apply_locked'], response.json)
        response = client.post('/api/downloads/connection', headers=selected)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNotNone(self.b.download_connection())
        self.assertIsNone(self.a.download_connection())
        self.assertEqual(self.a.get(task['id'])['status'], 'running')
        with self.manager.connect() as db:
            alpha = db.execute(instances.select().where(instances.c.id == 'alpha')).mappings().one()
        self.assertFalse(alpha['paused'])


if __name__ == '__main__':
    unittest.main()
