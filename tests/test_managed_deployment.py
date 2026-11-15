"""Deployment changes travel to the selected gateway and survive its restart."""
import json
import os
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy.engine import make_url

from tests import test_instance_management
from webapp.deployment import DeploymentGateway
from webapp.managed_deployment import configuration, update, validate_managed, test_connections


class ManagedDeploymentTests(unittest.TestCase):
    setUp = test_instance_management.InstanceManagementTests.setUp
    setup_fleet = test_instance_management.InstanceManagementTests.setup_fleet
    serve = test_instance_management.InstanceManagementTests.serve
    clients = test_instance_management.InstanceManagementTests.clients

    def gateways(self):
        gateways = {}
        for identity, app in self.apps.items():
            with patch.dict(os.environ, {}, clear=True):
                gateway = DeploymentGateway(self.root / identity / 'config.yaml',
                    self.root / identity / 'state', load_environment=False)
            gateway.profile.update(instance_id=identity, mysql_host=f'{identity}.mysql.test', mysql_port=3306,
                mysql_database='shared', mysql_user='fixture', mysql_password='fixture-mysql-secret',
                redis_url='redis://:fixture-redis-secret@redis.test:6379/2')
            gateway.active = app
            gateway.running_profile = dict(gateway.profile)
            app.extensions['deployment_gateway'] = gateway
            gateways[identity] = gateway
        return gateways

    def test_every_entry_reads_saves_and_tests_only_selected_gateway(self):
        gateways = self.gateways()
        for source, (client, headers) in self.clients().items():
            for target, gateway in gateways.items():
                with self.subTest(source=source, target=target):
                    selected = {**headers, 'X-Instance-ID': target}
                    response = client.get('/api/instance-deployment', headers=selected)
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(client.head('/api/instance-deployment', headers=selected).status_code, 200)
                    self.assertEqual(response.json['instance_id'], target)
                    self.assertNotIn('fixture-mysql-secret', response.text)
                    self.assertNotIn('fixture-redis-secret', response.text)
                    self.assertEqual(response.json['config']['mysql_host'], gateway.profile['mysql_host'])
                    data = {**response.json['config'], 'revision': response.json['revision'],
                            'instance_name': f'{source}-manages-{target}', 'mysql_port': 3307,
                            'instance_url': f'http://{target}.test:5000'}
                    before = {key: dict(value.profile) for key, value in gateways.items()}
                    with patch('webapp.managed_deployment.test_connections') as probe:
                        tested = client.post('/api/instance-deployment/test', headers=selected, json=data)
                        self.assertEqual(tested.status_code, 200, tested.json)
                        self.assertEqual(probe.call_args.args[0]['instance_id'], target)
                        result = client.put('/api/instance-deployment', headers=selected, json=data)
                    self.assertEqual(result.status_code, 200, result.json)
                    self.assertTrue(result.json['pending_restart'])
                    self.assertEqual(gateway.profile['mysql_password'], 'fixture-mysql-secret')
                    self.assertIn('fixture-redis-secret', gateway.profile['redis_url'])
                    self.assertEqual(gateway.running_profile['mysql_port'], 3306)
                    self.assertEqual(gateway.active, self.apps[target])
                    saved = json.loads(gateway.managed_path.read_text(encoding='utf-8'))['profile']
                    self.assertEqual(saved['instance_name'], data['instance_name'])
                    self.assertEqual(saved['mysql_port'], 3307)
                    for other in gateways:
                        if other != target:
                            self.assertEqual(gateways[other].profile, before[other])

    def test_connection_failure_conflict_and_disk_failure_leave_profile_unchanged(self):
        gateway = self.gateways()['alpha']
        original = dict(gateway.profile)
        data = {**configuration(gateway, 'alpha')['config'], 'revision': configuration(gateway, 'alpha')['revision'],
                'instance_name': 'new-name'}
        with patch('webapp.managed_deployment.test_connections', side_effect=ValueError('fixture unavailable')):
            with self.assertRaises(ValueError): update(gateway, data)
        self.assertEqual(gateway.profile, original)
        self.assertFalse(gateway.managed_path.exists())
        with patch('webapp.managed_deployment.test_connections'), patch('webapp.managed_deployment.write_profile', side_effect=OSError()):
            with self.assertRaises(ValueError): update(gateway, data)
        self.assertEqual(gateway.profile, original)
        self.assertFalse(gateway.pending_restart)
        with patch('webapp.managed_deployment.test_connections'):
            update(gateway, data)
            from werkzeug.exceptions import Conflict
            with self.assertRaises(Conflict): update(gateway, data)

    def test_name_only_save_updates_every_entry_without_restart_or_other_nodes(self):
        gateways = self.gateways()
        clients = self.clients()
        client, headers = clients['manager']
        selected = {**headers, 'X-Instance-ID': 'alpha'}
        original = {key: dict(value.profile) for key, value in gateways.items()}
        value = client.get('/api/instance-deployment', headers=selected).json
        with patch('webapp.managed_deployment.test_connections'):
            response = client.put('/api/instance-deployment', headers=selected,
                json={**value['config'], 'revision': value['revision'], 'instance_name': '洛杉矶'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['pending_restart'])
        self.assertIn('立即生效', response.json['message'])
        for entry, (reader, auth) in clients.items():
            with self.subTest(entry=entry):
                for query in ('', '?compact=1', '?poll=1'):
                    fleet = reader.get('/api/instances' + query, headers=auth).json
                    names = {item['id']: item['name'] for item in fleet['items']}
                    self.assertEqual(names, {'manager': 'manager', 'alpha': '洛杉矶', 'beta': 'beta'})
                config = reader.get('/api/instance-deployment', headers={**auth, 'X-Instance-ID': 'alpha'}).json
                self.assertEqual(config['config']['instance_name'], '洛杉矶')
        self.assertEqual(gateways['manager'].profile, original['manager'])
        self.assertEqual(gateways['beta'].profile, original['beta'])
        with patch.dict(os.environ, {}, clear=True):
            restarted = DeploymentGateway(gateways['alpha'].config_path, gateways['alpha'].directory,
                                          load_environment=False)
        self.assertEqual(restarted.profile['instance_name'], '洛杉矶')

    def test_instance_card_name_is_also_the_deployment_name_and_invalidates_revision(self):
        gateways = self.gateways()
        client, headers = self.clients()['manager']
        selected = {**headers, 'X-Instance-ID': 'alpha'}
        before = client.get('/api/instance-deployment', headers=selected).json
        renamed = client.put('/api/instances/alpha', headers=headers, json={'name': '东京'})
        self.assertEqual(renamed.status_code, 200, renamed.json)
        value = client.get('/api/instance-deployment', headers=selected).json
        self.assertEqual(value['config']['instance_name'], '东京')
        self.assertNotEqual(value['revision'], before['revision'])
        with patch('webapp.managed_deployment.test_connections'):
            stale = client.put('/api/instance-deployment', headers=selected,
                json={**before['config'], 'revision': before['revision']})
            self.assertEqual(stale.status_code, 400, stale.json)  # Peer RPC returns validation failures as 400.
            self.assertIn('已被其他页面修改', stale.json['error'])
            saved = client.put('/api/instance-deployment', headers=selected,
                json={**value['config'], 'revision': value['revision']})
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertFalse(saved.json['pending_restart'])
        self.assertEqual(gateways['alpha'].profile['instance_name'], '东京')

    def test_name_change_does_not_clear_a_pending_connection_restart(self):
        gateway = self.gateways()['alpha']
        with patch('webapp.managed_deployment.test_connections'):
            value = configuration(gateway, 'alpha')
            update(gateway, {**value['config'], 'revision': value['revision'], 'mysql_port': 3307})
            value = configuration(gateway, 'alpha')
            update(gateway, {**value['config'], 'revision': value['revision'], 'instance_name': '洛杉矶'})
        self.assertTrue(gateway.pending_restart)
        self.assertEqual(self.apps['alpha'].extensions['fleet'].row()['name'], '洛杉矶')

    def test_recovery_page_also_reads_and_updates_the_live_display_name(self):
        gateway = self.gateways()['alpha']
        fleet = self.apps['alpha'].extensions['fleet']
        fleet.control('alpha', {'name': '东京'})
        client = gateway.setup.test_client()
        with client.session_transaction() as session:
            session.update(deployment_admin=True, csrf='fixture-csrf')
        value = client.get('/api/deployment').json
        self.assertEqual(value['config']['instance_name'], '东京')
        with patch('webapp.deployment.test_mysql'):
            saved = client.post('/api/deployment/save', headers={'X-CSRF-Token': 'fixture-csrf'},
                json={**value['config'], 'instance_name': '洛杉矶'})
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertFalse(saved.json['pending_restart'])
        self.assertEqual(fleet.row()['name'], '洛杉矶')

    def test_recovery_remains_available_when_the_running_database_is_unreachable(self):
        from sqlalchemy.exc import OperationalError
        gateway = self.gateways()['alpha']
        fleet = self.apps['alpha'].extensions['fleet']
        client = gateway.setup.test_client()
        with client.session_transaction() as session:
            session.update(deployment_admin=True, csrf='fixture-csrf')
        error = OperationalError('fixture', {}, Exception('unreachable'))
        with patch.object(fleet, 'row', side_effect=error):
            value = client.get('/api/deployment')
        self.assertEqual(value.status_code, 200)
        with patch('webapp.deployment.test_mysql'), patch.object(fleet, 'control', side_effect=error):
            saved = client.post('/api/deployment/save', headers={'X-CSRF-Token': 'fixture-csrf'},
                json={**value.json['config'], 'instance_url': 'http://recovered.test:5000'})
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertTrue(saved.json['pending_restart'])
        self.assertEqual(gateway.profile['instance_url'], 'http://recovered.test:5000')

    def test_managed_connection_override_survives_restart_and_is_inherited_by_workers(self):
        gateway = self.gateways()['alpha']
        value = configuration(gateway, 'alpha')
        data = {**value['config'], 'revision': value['revision'], 'mysql_host': 'new.mysql.test',
                'mysql_password': 'new mysql $ password', 'redis_url': 'rediss://:new-secret@redis.new.test:6380/4'}
        with patch('webapp.managed_deployment.test_connections'):
            update(gateway, data)
        calls = []
        def factory(*args, **kwargs):
            calls.append((kwargs, os.environ.get('REDIS_URL')))
            return Flask('fixture')
        with patch.dict(os.environ, {'MYSQL_HOST': 'old.env.test', 'MYSQL_DATABASE': 'old',
            'MYSQL_USER': 'old', 'MYSQL_PASSWORD': 'old-password', 'REDIS_URL': 'redis://old:6379/0'}, clear=True):
            restarted = DeploymentGateway(gateway.config_path, gateway.directory, load_environment=False,
                                          app_factory=factory)
            self.assertTrue(restarted.auto_start)
            restarted.activate(dict(restarted.profile))
        url = make_url(calls[0][0]['database_url'])
        self.assertEqual((url.host, url.password), ('new.mysql.test', 'new mysql $ password'))
        self.assertEqual(calls[0][1], data['redis_url'])
        self.assertFalse(restarted.pending_restart)
        self.assertNotIn('new-secret', json.dumps(configuration(restarted, 'alpha')))

    def test_redis_disable_and_invalid_addresses(self):
        profile = self.gateways()['alpha'].profile
        value = validate_managed({'redis_enabled': False}, profile)
        self.assertEqual(value['redis_url'], '')
        for address in ('http://example.test', 'redis://host:notaport/0', 'redis://host/-1',
                        'redis://host/0?socket_timeout=600', 'redis:///0'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_managed({'redis_enabled': True, 'redis_url': address}, profile)

    def test_real_probe_wrapper_closes_redis_and_hides_connection_errors(self):
        profile = self.gateways()['alpha'].profile
        with patch('webapp.managed_deployment.test_mysql') as mysql, patch('redis.Redis.from_url') as redis:
            test_connections(profile)
            mysql.assert_called_once_with(profile)
            redis.return_value.ping.assert_called_once()
            redis.return_value.close.assert_called_once()
            redis.return_value.ping.side_effect = RuntimeError('fixture-redis-secret')
            with self.assertRaises(ValueError) as failure:
                test_connections(profile)
            self.assertNotIn('fixture-redis-secret', str(failure.exception))
            self.assertEqual(redis.return_value.close.call_count, 2)


if __name__ == '__main__':
    unittest.main()
