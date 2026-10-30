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

        values = {'download_methods': ['bergung', 'downmp3'], 'scheduler_mode': 'auto',
                  'bergung_quality': '1080', 'downmp3_timeout': 240,
                  'auto_channel_attempts': 4, 'shared_parser': False,
                  'download_connection_limit': 24}
        self.save(**values)
        response = self.client.get('/api/config', headers={'X-Instance-ID': 'alpha'})
        self.assertEqual(response.status_code, 200, response.json)
        config_path = self.root / 'alpha/config.yaml'
        saved = yaml.safe_load(config_path.read_text(encoding='utf-8'))
        for key, value in values.items():
            self.assertEqual(response.json['config'][key], value)
            self.assertEqual(saved[key], value)
            self.assertEqual(self.a.fleet.local_settings()['config'][key], value)
        self.assertEqual(response.json['config']['throughput_mode'], 'auto')
        self.assertEqual(self.b.fleet.policy()['config']['scheduler_mode'], 'ordered_fallback')
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
        self.save(download_methods=['yt5s','downmp3'], yt5s_quality='best',
                  downmp3_quality='best', downmp3_timeout=180, download_attempts=5,
                  fragment_concurrency=2)
        new = self.a.claim('another-alpha')
        config = new['payload']['config']
        self.assertEqual(config['download_methods'], ['yt5s','downmp3'])
        self.assertEqual((config['downmp3_timeout'], new['max_attempts'], config['fragment_concurrency']), (180,5,2))
        self.assertEqual(new['policy_version'], self.a.fleet.policy()['version'])
        self.assertEqual(self.b.fleet.policy()['config']['downmp3_quality'], '720')
        self.assertEqual(self.a.get(old['id'], private=True)['payload']['config']['downmp3_quality'], '720')
        self.assertTrue(self.a.heartbeat(old['id'], self.a.owner))
        self.assertTrue(self.a.finish(old['id'], error='temporary', worker_id=self.a.owner, retryable=True))
        with self.a.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == old['id']).values(available_at=0))
        self.save('beta', download_methods=['yt1d','downmp3'], yt1d_quality='2160', yt1d_timeout=120)
        retried = self.b.claim(self.b.owner)
        self.assertEqual(retried['id'], old['id'])
        self.assertEqual(retried['payload']['config']['download_methods'], ['yt1d','downmp3'])
        self.assertEqual(retried['payload']['config']['yt1d_quality'], '2160')
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

    def test_legacy_global_policy_no_longer_overrides_instance(self):
        self.queue(1)
        self.activate()
        with self.manager.connect() as db:
            for key in ('fleet_policy','fleet_active_policy'):
                db.execute(self.manager.db.upsert(settings, dict(key=key,
                    value=json.dumps({'version':99,'config':{'storage_mode':'test','downmp3_quality':'1080'}})), ['key']))
        claimed = self.a.claim(self.a.owner)
        self.assertEqual(claimed['payload']['config']['downmp3_quality'], '720')
        self.assertEqual(claimed['payload']['config']['storage_mode'], 'local')
        self.assertNotEqual(claimed['policy_version'], 99)

    def test_same_job_uses_each_instance_policy_and_preserves_billing(self):
        from webapp.database import job_profiles
        self.save(download_methods=['yt5s','downmp3'], yt5s_quality='720')
        self.save('beta', download_methods=['yt1d','downmp3'], yt1d_quality='1080', storage_mode='test')
        response = self.client.post('/api/jobs', json={'customer':'A','name':'Shared',
            'urls':'npNnbj46_ik\n2Gu7q-KUXCk', 'price':'1'}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        identity = response.json['id']
        # Historical task policies remain stored for compatibility, but never apply.
        with self.manager.connect() as db:
            db.execute(job_profiles.update().where(job_profiles.c.id==identity).values(
                policy=json.dumps({'download_methods':['yt_dlp'],'storage_mode':'local'})))
        with patch('webapp.fleet_routes.rpc', return_value={'ready':True}):
            response = self.client.post(f'/api/jobs/{identity}/control',
                json={'action':'start','resume_system':True}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        first, second = self.a.claim(self.a.owner), self.b.claim(self.b.owner)
        self.assertEqual(first['batch_id'], second['batch_id'])
        self.assertEqual(first['payload']['config']['download_methods'], ['yt5s','downmp3'])
        self.assertEqual(second['payload']['config']['download_methods'], ['yt1d','downmp3'])
        fixture = self.root / 'fixture.mp4'
        fixture.write_bytes(b'x'*1000)
        self.assertTrue(self.a.statistics.record_file(first['id'], fixture))
        self.assertTrue(self.b.statistics.record_file(second['id'], fixture, test_mode=True))
        detail = self.client.get('/api/jobs/'+identity).json
        self.assertEqual(detail['file_bytes_exact'], '1000')
        self.assertEqual(detail['test_file_bytes_exact'], '1000')
        self.assertIsNone(detail['policy'])
        items = {item['id']:item for item in self.client.get('/api/instances').json['items']}
        self.assertEqual(items['alpha']['policy']['download_methods'], ['yt5s','downmp3'])
        self.assertEqual(items['beta']['policy']['storage_mode'], 'test')
        # Remote policy persists in the selected node's YAML and survives restart.
        from app import create_app
        from webapp.database import instances
        with self.b.connect() as db:
            db.execute(instances.update().where(instances.c.id=='beta').values(heartbeat=0))
        restarted = create_app(self.root/'beta/config.yaml', self.b.directory, start_worker=False,
            role='worker', instance_id='beta', database_url=self.url)
        self.assertEqual(restarted.extensions['fleet'].policy()['config']['yt1d_quality'], '1080')
        self.assertEqual(restarted.extensions['fleet'].policy()['config']['storage_mode'], 'test')

    def test_invalid_remote_save_and_task_policy_writes_are_rejected(self):
        before = self.a.fleet.policy()
        self.serve('alpha')
        response = self.client.put('/api/config', json={'yt5s_quality':'invalid'},
            headers={**self.headers,'X-Instance-ID':'alpha'})
        self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.a.fleet.policy(), before)
        response = self.client.post('/api/jobs', json={'customer':'A','name':'B',
            'policy':{'download_methods':['downmp3']}}, headers=self.headers)
        self.assertEqual(response.status_code, 400, response.json)

    def test_preflight_checks_node_config_and_detects_changes_during_check(self):
        with patch('webapp.environment.require_downmp3_environment') as check, \
                patch('webapp.youtube_auth.YouTubeAuth.check', side_effect=AssertionError('peer policy leaked')):
            value, status = self.a.fleet.check_local({'config':{'download_methods':['yt_dlp']}})
        self.assertEqual(status, 200, value)
        check.assert_called_once()
        with patch('webapp.environment.require_downmp3_environment', side_effect=lambda:
                self.a.fleet.save_policy({'downmp3_quality':'1080'})):
            with self.assertRaisesRegex(ValueError, '策略发生变化'):
                self.a.fleet.check_local({})


class HybridHotPolicyTests(unittest.TestCase):
    setUp = test_hybrid.HybridTests.setUp
    serve = test_fleet.FleetTests.serve

    def test_local_and_remote_management_saves_are_isolated(self):
        response = self.clients['alpha'].put('/api/config', json={'downmp3_quality':'720'}, headers=self.headers['alpha'])
        self.assertEqual(response.status_code, 200, response.json)
        for video in ['npNnbj46_ik','2Gu7q-KUXCk','WMu6ANTg8mk']:
            self.a.create('download', {'urls':['https://youtu.be/'+video]})
        self.a.activate_download_lists()
        self.a.fleet.control('alpha', {'action':'resume'})
        self.b.fleet.control('beta', {'action':'resume'})
        old = self.a.claim(self.a.owner)
        response = self.clients['alpha'].put('/api/config', json={'downmp3_quality':'480'}, headers=self.headers['alpha'])
        self.assertEqual(response.status_code, 200, response.json)
        response = self.clients['alpha'].put('/api/config', json={'downmp3_quality':'best'},
            headers={**self.headers['alpha'],'X-Instance-ID':'beta'})
        self.assertEqual(response.status_code, 200, response.json)
        new = self.b.claim(self.b.owner)
        self.assertEqual(new['payload']['config']['downmp3_quality'], 'best')
        self.assertEqual(self.a.fleet.policy()['config']['downmp3_quality'], '480')
        self.assertEqual(self.a.get(old['id'], private=True)['payload']['config']['downmp3_quality'], '720')
        config = self.clients['alpha'].get('/api/config', headers={'X-Instance-ID':'beta'}).json['config']
        self.assertEqual(config['downmp3_quality'], 'best')
        self.assertEqual(self.a.claim('next-alpha')['payload']['config']['downmp3_quality'], '480')


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
