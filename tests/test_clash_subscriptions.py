"""Saved subscriptions survive switching, failed core updates, and upgrades."""
from io import BytesIO
import json
from unittest.mock import Mock, patch
import unittest

import yaml

from tests import test_clash, test_hybrid
from webapp.clash import ClashError, ClashManager, GROUP


class SavedSubscriptionTests(unittest.TestCase):
    setUp = test_clash.ClashTests.setUp
    post = test_clash.ClashTests.post

    def fixtures(self):
        self.content = {}
        self.requests = []

        def fetch(request, **kwargs):
            self.requests.append(request.full_url)
            return BytesIO(self.content[request.full_url])

        for target, value in ((self.manager, 'running'), (self.manager, 'start'),
                              (self.manager, 'validate_runtime'), (self.manager, 'api')):
            mocked = patch.object(target, value, return_value=True if value == 'running' else {})
            mocked.start()
            self.addCleanup(mocked.stop)
        for mocked in (patch('webapp.clash.validate_subscription_url'),
                       patch('webapp.clash.build_opener', return_value=Mock(open=fetch))):
            mocked.start()
            self.addCleanup(mocked.stop)

    def save(self, name, nodes, identity='', token=None):
        url = 'https://example.test/' + (token or name)
        self.content[url] = yaml.safe_dump({'proxies': [
            {'name': node, 'type': 'http', 'server': 'proxy.example.test', 'port': 8080}
            for node in nodes]}).encode()
        response = self.post('/api/clash/subscription', {'action': 'save', 'id': identity,
                                                       'name': name, 'url': url})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['saved_subscription_id']

    def test_save_switch_restore_restart_and_delete_without_leaking_credentials(self):
        self.fixtures()
        first = self.save('常用', ['共享名称', 'A2'], token='PRIVATE_TOKEN_A')
        self.assertEqual(self.manager.config['active_subscription_id'], first)
        self.manager.select('A2')
        self.manager.configure_rotation({'enabled': True, 'nodes': ['A2', '共享名称'],
                                         'every': 3, 'count_failures': False})
        self.manager.config['worker_nodes'] = {'1': 'A2'}
        self.manager.save()
        before = (self.manager.directory / 'runtime.yaml').read_bytes()
        second = self.save('备用', ['共享名称', 'B2'], token='PRIVATE_TOKEN_B')
        self.assertEqual(self.manager.config['active_subscription_id'], first)
        self.assertEqual((self.manager.directory / 'runtime.yaml').read_bytes(), before)
        requested = list(self.requests)
        self.manager.delays = {'共享名称': {'ms': 1}}
        self.manager.testing.update(done=2, total=2)
        (self.manager.directory / 'route-health.json').write_text(
            json.dumps({'共享名称': {'until': 9999999999}}), encoding='utf-8')
        response = self.post('/api/clash/subscription', {'action': 'switch', 'id': second})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['active_subscription_id'], second)
        self.assertFalse(response.json['rotation']['enabled'])
        self.assertEqual(self.manager.config['worker_nodes'], {})
        self.assertEqual(self.manager.delays, {})
        self.assertEqual(response.json['testing']['total'], 0)
        self.assertTrue(all(node['cooling_until'] == 0 for node in response.json['nodes']))
        self.assertEqual([node['name'] for node in self.manager.nodes()], ['共享名称', 'B2'])
        self.manager.select('B2')
        response = self.post('/api/clash/subscription', {'action': 'switch', 'id': first})
        self.assertEqual(response.json['selected'], 'A2')
        self.assertEqual(response.json['rotation']['nodes'], ['A2', '共享名称'])
        self.assertEqual(response.json['rotation']['every'], 3)
        self.assertTrue(response.json['rotation']['enabled'])
        self.assertEqual(self.manager.config['worker_nodes'], {'1': 'A2'})
        self.assertEqual(self.requests, requested)  # Switching uses cached nodes, not the network.
        for url in ('/api/clash', '/api/config', '/api/status', '/'):
            text = self.client.get(url).text
            self.assertNotIn('PRIVATE_TOKEN', text)
            self.assertNotIn('proxy.example.test', text)
        restarted = ClashManager(self.manager.directory)
        self.assertEqual(restarted.config['active_subscription_id'], first)
        self.assertEqual(len(restarted.config['subscriptions']), 2)
        self.assertEqual(restarted.config['selected'], 'A2')
        self.assertEqual(self.post('/api/clash/subscription', {'action': 'delete', 'id': first}).status_code, 502)
        deleted = self.post('/api/clash/subscription', {'action': 'delete', 'id': second})
        self.assertEqual(deleted.status_code, 200, deleted.json)
        self.assertEqual(len(deleted.json['subscriptions']), 1)
        self.assertFalse(self.manager.subscription_file(second).exists())

    def test_failed_switch_and_invalid_update_preserve_active_and_saved_profiles(self):
        self.fixtures()
        first = self.save('First', ['A'])
        second = self.save('Second', ['B'])
        paths = [self.manager.directory / name for name in ('settings.json', 'nodes.yaml', 'runtime.yaml')]
        paths += [self.manager.subscription_file(first), self.manager.subscription_file(second)]
        before = {path: path.read_bytes() for path in paths}

        def fail_target(path, method='GET', data=None, **kwargs):
            if path == '/proxies/' + GROUP and data == {'name': 'B'}:
                raise ClashError('模拟切换失败')
            return {}

        with patch.object(self.manager, 'api', side_effect=fail_target):
            response = self.post('/api/clash/subscription', {'action': 'switch', 'id': second})
        self.assertEqual(response.status_code, 502)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.content['https://example.test/Second'] = b'PRIVATE_BAD_RESPONSE'
        failed = self.post('/api/clash/subscription', {'action': 'save', 'id': second, 'name': 'Changed'})
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn('PRIVATE_BAD_RESPONSE', failed.text)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def test_original_subscription_migrates_with_private_cached_nodes_and_selection(self):
        self.manager.config.update(subscription_url='https://example.test/?PRIVATE_LEGACY',
                                   selected='Old Node', subscription_id='old-revision')
        self.manager.config.pop('subscriptions')
        self.manager.config.pop('active_subscription_id')
        nodes = {'proxies': [{'name': 'Old Node', 'type': 'http', 'server': 'proxy.test', 'port': 8080}]}
        (self.manager.directory / 'nodes.yaml').write_text(yaml.safe_dump(nodes), encoding='utf-8')
        self.manager.save()
        migrated = ClashManager(self.manager.directory)
        identity = migrated.config['active_subscription_id']
        self.assertTrue(identity)
        self.assertEqual(migrated.config['selected'], 'Old Node')
        self.assertEqual(migrated.config['subscription_id'], 'old-revision')
        self.assertEqual(yaml.safe_load(migrated.subscription_file(identity).read_text()), nodes)
        self.assertEqual(len(ClashManager(self.manager.directory).config['subscriptions']), 1)
        public = json.dumps(migrated.status())
        self.assertNotIn('PRIVATE_LEGACY', public)

    def test_queue_locks_all_profile_mutations_and_ids_are_validated(self):
        self.fixtures()
        identity = self.save('First', ['A'])
        for invalid in ('../settings', [], 'a' * 32):
            response = self.post('/api/clash/subscription', {'action': 'switch', 'id': invalid})
            self.assertEqual(response.status_code, 502, response.json)
        self.manager.testing['running'] = True
        self.assertEqual(self.post('/api/clash/subscription', {'action': 'switch', 'id': identity}).status_code, 502)
        self.manager.testing['running'] = False
        self.app.extensions['task_store'].create('download', {'urls': ['https://example.test/video.mp4']})
        for action in ('save', 'switch', 'delete'):
            response = self.post('/api/clash/subscription', {'action': action, 'id': identity})
            self.assertEqual(response.status_code, 409, response.json)

    def test_invalid_new_profiles_and_core_validation_leave_saved_data_intact(self):
        self.fixtures()
        identity = self.save('First', ['A'])
        before = (self.manager.directory / 'settings.json').read_bytes()
        invalid = [
            {'id': '', 'name': 'Blank Link'},
            {'id': '', 'name': 'Duplicate', 'url': 'https://example.test/First'},
            {'id': identity, 'name': 'Bad\nName'},
        ]
        for data in invalid:
            with self.subTest(data=data):
                response = self.post('/api/clash/subscription', {'action': 'save', **data})
                self.assertEqual(response.status_code, 502, response.json)
                self.assertEqual((self.manager.directory / 'settings.json').read_bytes(), before)
        with patch.object(self.manager, 'validate_runtime', side_effect=ClashError('不支持此协议')):
            response = self.post('/api/clash/subscription', {'action': 'save', 'id': identity, 'name': 'Changed'})
        self.assertEqual(response.status_code, 502)
        self.assertEqual((self.manager.directory / 'settings.json').read_bytes(), before)
        with patch('webapp.clash.MAX_SUBSCRIPTIONS', 1):
            response = self.post('/api/clash/subscription', {'action': 'save', 'id': '', 'name': 'Second',
                                                           'url': 'https://example.test/Second'})
        self.assertEqual(response.status_code, 502)
        self.assertEqual((self.manager.directory / 'settings.json').read_bytes(), before)


class HybridSavedSubscriptionTests(unittest.TestCase):
    serve = test_hybrid.HybridTests.serve
    fixtures = SavedSubscriptionTests.fixtures
    save = SavedSubscriptionTests.save
    post = test_clash.ClashTests.post

    def setUp(self):
        test_hybrid.HybridTests.setUp(self)
        self.app = self.apps['alpha']
        self.client = self.clients['alpha']
        self.headers = {**self.headers['alpha'], 'X-Instance-ID': 'beta'}
        self.manager = self.apps['beta'].extensions['clash']

    def test_remote_profile_changes_only_affect_the_selected_instance(self):
        self.fixtures()
        first = self.save('Remote First', ['A'])
        second = self.save('Remote Backup', ['B'])
        response = self.post('/api/clash/subscription', {'action': 'switch', 'id': second})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['active_subscription_id'], second)
        remote = self.client.get('/api/clash', headers=self.headers).json
        self.assertEqual(len(remote['subscriptions']), 2)
        self.assertEqual(remote['selected'], 'B')
        local = self.client.get('/api/clash', headers={'X-Instance-ID': 'alpha'}).json
        self.assertEqual(local['subscriptions'], [])
        self.assertFalse(local['subscription_saved'])
        self.assertEqual(self.client.get('/api/config', headers={'X-Instance-ID': 'alpha'}).json['config']['proxy_mode'], 'direct')
        self.assertEqual(self.client.get('/api/config', headers=self.headers).json['config']['proxy_mode'], 'clash')
        self.assertEqual(self.post('/api/clash/subscription', {'action': 'delete', 'id': first}).status_code, 200)
