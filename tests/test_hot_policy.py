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
    serve = test_fleet.FleetTests.serve

    def save(self, identity='alpha', **data):
        if not self.apps[identity].extensions['fleet'].url:
            self.serve(identity)
        response = self.client.put('/api/config', json=data,
            headers={**self.headers, 'X-Instance-ID':identity})
        self.assertEqual(response.status_code, 200, response.json)
        return response

    def test_remote_strategy_persists_in_file_registry_and_after_restart(self):
        import yaml
        from app import create_app
        from webapp.database import instances

        values = {'fragment_concurrency': 8, 'youtube_start_interval': 55, 'download_attempts': 5}
        self.save(**values)
        response = self.client.get('/api/config', headers={'X-Instance-ID': 'alpha'})
        self.assertEqual(response.status_code, 200, response.json)
        config_path = self.root / 'alpha/config.yaml'
        saved = yaml.safe_load(config_path.read_text(encoding='utf-8'))
        for key, value in values.items():
            self.assertEqual(response.json['config'][key], value)
            self.assertEqual(saved[key], value)
            self.assertEqual(self.a.fleet.local_settings()['config'][key], value)
        # Channel selection and instance concurrency are independent controls.
        self.assertNotIn('throughput_mode', response.json['config'])
        self.assertNotIn('scheduler_mode', self.b.fleet.policy()['config'])
        with self.a.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(heartbeat=0))
        restarted = create_app(config_path, self.a.directory, start_worker=False,
                               role='hybrid', instance_id='alpha', database_url=self.url)
        try:
            restored = restarted.test_client().get('/api/config').json['config']
            for key, value in values.items():
                self.assertEqual(restored[key], value)
        finally:
            restarted.extensions['task_store'].db.engine.dispose()

    def test_selected_instance_save_and_cross_instance_retry(self):
        self.queue(3)
        self.activate()
        old = self.a.claim(self.a.owner)
        self.save(youtube_start_interval=120, download_attempts=5,
                  fragment_concurrency=2)
        new = self.a.claim('another-alpha')
        config = new['payload']['config']
        self.assertNotIn('download_methods', config)
        self.assertEqual((config['youtube_start_interval'], new['max_attempts'], config['fragment_concurrency']), (120,5,2))
        self.assertEqual(new['policy_version'], self.a.fleet.policy()['version'])
        self.assertEqual(self.b.fleet.local_settings()['config']['youtube_start_interval'], 90)
        self.assertEqual(self.a.get(old['id'], private=True)['payload']['config']['youtube_start_interval'], 90)
        self.assertTrue(self.a.heartbeat(old['id'], self.a.owner))
        self.assertTrue(self.a.finish(old['id'], error='temporary', worker_id=self.a.owner, retryable=True))
        with self.a.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == old['id']).values(available_at=0))
        self.save('beta', youtube_start_interval=180)
        retried = self.b.claim(self.b.owner)
        self.assertEqual(retried['id'], old['id'])
        self.assertNotIn('download_methods', retried['payload']['config'])
        self.assertEqual(retried['payload']['config']['youtube_start_interval'], 180)
        self.assertTrue(retried['payload']['_execution_settings'])
        self.assertFalse(self.manager.downloads_paused())

    def test_save_never_activates_or_unpauses_work(self):
        self.queue(2)
        self.manager.set_downloads_paused(True)
        self.save(fragment_concurrency=8)
        self.assertIsNone(self.manager.fleet.policy(active=True))
        self.assertTrue(self.manager.downloads_paused())
        self.assertIsNone(self.a.claim(self.a.owner))
        self.activate()
        self.manager.set_downloads_paused(True)
        self.save(youtube_start_interval=120)
        self.assertTrue(self.manager.downloads_paused())
        self.assertIsNone(self.b.claim(self.b.owner))



    def test_invalid_remote_save_and_task_policy_writes_are_rejected(self):
        before = self.a.fleet.policy()
        self.serve('alpha')
        response = self.client.put('/api/config', json={'yt5s_quality':'invalid'},
            headers={**self.headers,'X-Instance-ID':'alpha'})
        self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.a.fleet.policy(), before)
        response = self.client.post('/api/jobs', json={'customer':'A','name':'B',
            'policy':{'download_methods':['yt_dlp']}}, headers=self.headers)
        self.assertEqual(response.status_code, 400, response.json)

    def test_preflight_checks_node_config_and_detects_changes_during_check(self):
        with patch('webapp.environment.require_native_environment') as check, \
                patch('webapp.youtube_auth.YouTubeAuth.check', side_effect=AssertionError('peer policy leaked')):
            value, status = self.a.fleet.check_local({'config':{}})
        self.assertEqual(status, 200, value)
        check.assert_called_once()
        with patch('webapp.environment.require_native_environment', side_effect=lambda config:
                self.a.fleet.save_policy({'fragment_concurrency':8})):
            with self.assertRaisesRegex(ValueError, '策略发生变化'):
                self.a.fleet.check_local({})


class HybridHotPolicyTests(unittest.TestCase):
    setUp = test_hybrid.HybridTests.setUp
    serve = test_fleet.FleetTests.serve

    def test_local_and_remote_management_saves_are_isolated(self):
        response = self.clients['alpha'].put('/api/config', json={'youtube_start_interval':90}, headers=self.headers['alpha'])
        self.assertEqual(response.status_code, 200, response.json)
        for video in ['npNnbj46_ik','2Gu7q-KUXCk','WMu6ANTg8mk']:
            self.a.create('download', {'urls':['https://youtu.be/'+video]})
        self.a.activate_download_lists()
        self.a.fleet.control('alpha', {'action':'resume'})
        self.b.fleet.control('beta', {'action':'resume'})
        old = self.a.claim(self.a.owner)
        response = self.clients['alpha'].put('/api/config', json={'youtube_start_interval':60}, headers=self.headers['alpha'])
        self.assertEqual(response.status_code, 200, response.json)
        response = self.clients['alpha'].put('/api/config', json={'youtube_start_interval':120},
            headers={**self.headers['alpha'],'X-Instance-ID':'beta'})
        self.assertEqual(response.status_code, 200, response.json)
        new = self.b.claim(self.b.owner)
        self.assertEqual(new['payload']['config']['youtube_start_interval'], 120)
        self.assertEqual(self.a.fleet.local_settings()['config']['youtube_start_interval'], 60)
        self.assertEqual(self.a.get(old['id'], private=True)['payload']['config']['youtube_start_interval'], 90)
        config = self.clients['alpha'].get('/api/config', headers={'X-Instance-ID':'beta'}).json['config']
        self.assertEqual(config['youtube_start_interval'], 120)
        self.assertEqual(self.a.claim('next-alpha')['payload']['config']['youtube_start_interval'], 60)


class StandaloneHotPolicyTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_settings_apply_between_claims_without_restart(self):
        response = self.client.put('/api/config', json={'youtube_start_interval': 90}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        for video in ['npNnbj46_ik', '2Gu7q-KUXCk']:
            self.store.create('download', {'base': str(self.root), 'urls': ['https://youtu.be/' + video]})
        self.store.activate_download_lists()
        old = self.store.claim(self.store.owner)
        response = self.client.put('/api/config', json={'youtube_start_interval': 120}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        new = self.store.claim('another-worker')
        self.assertEqual(new['payload']['config']['youtube_start_interval'], 120)
        self.assertEqual(self.store.get(old['id'], private=True)['payload']['config']['youtube_start_interval'], 90)
