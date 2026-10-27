"""Policy saves affect the next claim without changing in-flight snapshots."""
import json
import unittest
from unittest.mock import patch

from tests import test_fleet, test_hybrid, test_scale
from webapp.database import settings, tasks


class FleetHotPolicyTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet
    queue = test_fleet.FleetTests.queue
    activate = test_fleet.FleetTests.activate

    def save(self, **data):
        response = self.client.put('/api/config', json=data, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return response

    def test_manager_save_updates_claims_and_retries_across_workers(self):
        self.queue(3)
        self.activate()
        old = self.a.claim(self.a.owner)
        response = self.save(download_methods=['yt5s', 'downmp3'], yt5s_quality='best',
                             downmp3_quality='best', downmp3_timeout=180, download_attempts=5,
                             fragment_concurrency=2)
        self.assertIn('后续领取', response.json['message'])
        latest = self.manager.fleet.policy()
        self.assertEqual(self.manager.fleet.policy(active=True)['version'], latest['version'])
        new = self.b.claim(self.b.owner)
        config = new['payload']['config']
        self.assertEqual(new['policy_version'], latest['version'])
        self.assertEqual(config['download_methods'], ['yt5s', 'downmp3'])
        self.assertEqual((config['yt5s_quality'], config['downmp3_quality']), ('best', 'best'))
        self.assertEqual((config['downmp3_timeout'], new['max_attempts'], config['fragment_concurrency']),
                         (180, 5, 2))
        self.assertEqual(config['path'], 'videos-beta')
        running = self.a.get(old['id'], private=True)
        pinned = self.a.effective_download_payload(old['id'], running['payload'])
        self.assertEqual(pinned['config']['downmp3_quality'], '720')
        self.assertEqual(running['policy_version'], old['policy_version'])
        self.assertTrue(self.a.heartbeat(old['id'], self.a.owner))
        self.assertTrue(self.a.finish(old['id'], error='temporary', worker_id=self.a.owner, retryable=True))
        with self.a.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == old['id']).values(available_at=0))
        retried = self.a.claim(self.a.owner)
        self.assertEqual(retried['id'], old['id'])
        self.assertEqual(retried['payload']['config']['yt5s_quality'], 'best')
        self.assertTrue(retried['payload']['_restart_transfer'])
        self.assertFalse(self.manager.downloads_paused())

    def test_save_never_activates_or_unpauses_work(self):
        self.queue(2)
        self.manager.set_downloads_paused(True)
        self.save(yt5s_quality='best')
        self.assertIsNone(self.manager.fleet.policy(active=True))
        self.assertTrue(self.manager.downloads_paused())
        self.assertIsNone(self.a.claim(self.a.owner))
        self.activate()
        self.manager.set_downloads_paused(True)
        self.save(downmp3_quality='best')
        self.assertTrue(self.manager.downloads_paused())
        self.assertIsNone(self.b.claim(self.b.owner))

    def test_pre_upgrade_saved_policy_is_used_without_another_save_or_activation(self):
        self.queue(1)
        self.activate()
        saved = self.manager.fleet.policy()
        saved['version'] += 1
        saved['config'].update(yt5s_quality='best', downmp3_quality='best')
        with self.manager.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'fleet_policy').values(value=json.dumps(saved)))
        self.assertNotEqual(self.manager.fleet.policy(active=True)['version'], saved['version'])
        with patch('webapp.fleet_routes.rpc', return_value={'ready': True}) as preflight:
            response = self.client.put('/api/instances/alpha', json={'action': 'resume'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(preflight.call_args.kwargs['data']['config']['yt5s_quality'], 'best')
        claimed = self.a.claim(self.a.owner)
        self.assertEqual(claimed['policy_version'], saved['version'])
        self.assertEqual(claimed['payload']['config']['yt5s_quality'], 'best')

    def test_storage_mode_remains_pinned_until_explicit_activation(self):
        self.queue(2)
        self.activate()
        policy = self.manager.fleet.policy()['config']
        self.manager.fleet.save_policy({**policy, 'storage_mode': 'test', 'yt5s_quality': 'best'})
        self.assertEqual(self.manager.fleet.policy()['config']['storage_mode'], 'test')
        self.assertEqual(self.manager.fleet.policy(active=True)['config']['storage_mode'], 'local')
        claimed = self.a.claim(self.a.owner)
        self.assertEqual(claimed['payload']['config']['storage_mode'], 'local')
        self.assertEqual(claimed['payload']['config']['yt5s_quality'], 'best')
        self.assertTrue(self.a.finish(claimed['id'], result={}, worker_id=self.a.owner))
        self.manager.set_downloads_paused(True)
        self.activate()
        self.assertEqual(self.b.claim(self.b.owner)['payload']['config']['storage_mode'], 'test')

    def test_invalid_save_preserves_saved_and_execution_policies(self):
        self.queue(1)
        self.activate()
        before = (self.manager.fleet.policy(), self.manager.fleet.policy(active=True))
        response = self.client.put('/api/config', json={'yt5s_quality': 'invalid'}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual((self.manager.fleet.policy(), self.manager.fleet.policy(active=True)), before)

    def test_job_policy_changes_are_hot_and_global_defaults_do_not_override_jobs(self):
        ids = []
        for name, quality in [('A', '720'), ('B', '1080')]:
            response = self.client.post('/api/jobs', json={'customer': name, 'name': name,
                'urls': 'npNnbj46_ik\n2Gu7q-KUXCk', 'policy': {
                    'download_methods': ['downmp3'], 'downmp3_quality': quality}}, headers=self.headers)
            self.assertEqual(response.status_code, 201, response.json)
            identity = response.json['id']
            ids.append(identity)
            with patch('webapp.fleet_routes.rpc', return_value={'ready': True}):
                response = self.client.post(f'/api/jobs/{identity}/control', json={'action': 'start'}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
        first = self.a.claim(self.a.owner)
        self.assertEqual(first['batch_id'], ids[0])
        self.save(downmp3_quality='360')
        detail = self.client.get('/api/jobs/' + ids[0]).json
        response = self.client.put('/api/jobs/' + ids[0], json={'edit_version': detail['edit_version'],
            'policy': {'downmp3_quality': 'best'}}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        second = self.b.claim(self.b.owner)
        third = self.b.claim(self.b.owner)
        self.assertEqual((second['batch_id'], second['payload']['config']['downmp3_quality']), (ids[0], 'best'))
        self.assertEqual((third['batch_id'], third['payload']['config']['downmp3_quality']), (ids[1], '1080'))
        self.assertEqual(self.a.get(first['id'], private=True)['payload']['config']['downmp3_quality'], '720')


class HybridHotPolicyTests(unittest.TestCase):
    setUp = test_hybrid.HybridTests.setUp
    serve = test_fleet.FleetTests.serve

    def test_local_and_remote_management_saves_publish_to_all_workers(self):
        response = self.clients['alpha'].put('/api/config', json={'downmp3_quality': '720'},
                                            headers=self.headers['alpha'])
        self.assertEqual(response.status_code, 200, response.json)
        self.a.create('download', {'urls': ['https://youtu.be/npNnbj46_ik']})
        self.a.create('download', {'urls': ['https://youtu.be/2Gu7q-KUXCk']})
        self.a.activate_download_lists()
        self.a.fleet.control('alpha', {'action': 'resume'})
        self.b.fleet.control('beta', {'action': 'resume'})
        old = self.a.claim(self.a.owner)
        response = self.clients['alpha'].put('/api/config', json={'downmp3_quality': '480'},
                                            headers=self.headers['alpha'])
        self.assertEqual(response.status_code, 200, response.json)
        response = self.clients['alpha'].put('/api/config', json={'downmp3_quality': 'best'},
            headers={**self.headers['alpha'], 'X-Instance-ID': 'beta'})
        self.assertEqual(response.status_code, 200, response.json)
        new = self.b.claim(self.b.owner)
        self.assertEqual(new['payload']['config']['downmp3_quality'], 'best')
        self.assertEqual(new['policy_version'], self.a.fleet.policy()['version'])
        self.assertEqual(self.a.get(old['id'], private=True)['payload']['config']['downmp3_quality'], '720')


class StandaloneHotPolicyTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_settings_apply_between_claims_without_restart(self):
        response = self.client.put('/api/config', json={'download_method': 'downmp3',
            'downmp3_quality': '720'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        for video in ['npNnbj46_ik', '2Gu7q-KUXCk']:
            self.store.create('download', {'base': str(self.root), 'urls': ['https://youtu.be/' + video]})
        self.store.activate_download_lists()
        old = self.store.claim(self.store.owner)
        response = self.client.put('/api/config', json={'downmp3_quality': 'best'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        new = self.store.claim('another-worker')
        self.assertEqual(new['payload']['config']['downmp3_quality'], 'best')
        self.assertEqual(self.store.get(old['id'], private=True)['payload']['config']['downmp3_quality'], '720')
