"""Instance-policy saves preserve current attempts and affect subsequent claims."""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select

from app import create_app
from tests import test_hybrid, test_scale
from webapp.database import instances, job_profiles


class SettingsSaveContract:
    def create(self, name='Existing'):
        self.client.put('/api/config', json={'download_methods':['downmp3']}, headers=self.headers)
        response = self.client.post('/api/jobs', json={'customer':'Customer', 'name':name,
            'urls':'npNnbj46_ik\n2Gu7q-KUXCk'}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['id']

    def start(self, identity):
        if self.store.fleet:
            self.store.fleet.control(self.store.fleet.id, {'action':'resume'})
        with patch('app.require_downmp3_environment'), patch('webapp.fleet_routes.rpc'):
            response = self.client.post(f'/api/jobs/{identity}/control',
                json={'action':'start', 'resume_system':True}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)

    def test_instance_mode_changes_on_next_claim_with_running_snapshot_preserved(self):
        identity = self.create()
        self.start(identity)
        first = self.store.claim(self.store.owner)
        self.assertEqual(first['payload']['config']['storage_mode'], 'local')
        response = self.client.put('/api/config', json={'storage_mode':'test',
            'downmp3_quality':'720', 'max_log_mb':3}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.client.get('/api/config').json['config']['storage_mode'], 'test')
        self.assertTrue(self.store.heartbeat(first['id'], self.store.owner))
        self.assertFalse(self.store.downloads_paused())
        self.assertEqual(self.store.get(first['id'], private=True)['payload']['config']['storage_mode'], 'local')
        self.assertEqual(self.store.claim('next-owner')['payload']['config']['storage_mode'], 'test')
        created = self.create('New task')
        self.assertEqual(self.client.get(f'/api/jobs/{created}').json['policy_scope'], 'instance')
        current = self.client.get(f'/api/jobs/{identity}').json
        response = self.client.put(f'/api/jobs/{identity}', json={'edit_version':current['edit_version'],
            'policy':{'storage_mode':'test'}}, headers=self.headers)
        self.assertEqual(response.status_code, 400, response.json)

    def test_unchanged_storage_fields_allow_other_save_during_cleanup(self):
        config = self.client.get('/api/config').json['config']
        self.store.create('clear_media', {})
        response = self.client.put('/api/config', json={**{key:config[key] for key in
            ('path','dir','storage_mode')}, 'downmp3_quality':'720'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        response = self.client.put('/api/config', json={'path':'different-directory'}, headers=self.headers)
        self.assertEqual(response.status_code, 409, response.json)

    def test_historical_job_policy_does_not_lock_instance_mode(self):
        identity = self.create()
        with self.store.connect() as db:
            policy = json.loads(db.execute(select(job_profiles.c.policy).where(job_profiles.c.id==identity)).scalar_one())
            policy['storage_mode'] = 'local'
            db.execute(job_profiles.update().where(job_profiles.c.id==identity).values(policy=json.dumps(policy)))
        self.start(identity)
        self.store.claim(self.store.owner)
        response = self.client.put('/api/config', json={'storage_mode':'test'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)


class StandaloneSettingsSaveTests(SettingsSaveContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


class HybridSettingsSaveTests(SettingsSaveContract, unittest.TestCase):
    serve = test_hybrid.HybridTests.serve

    def setUp(self):
        test_hybrid.HybridTests.setUp(self)
        self.store = self.a
        self.client = self.clients['alpha']
        self.headers = self.headers['alpha']

    def test_legacy_instance_overrides_cannot_undo_saved_download_strategy(self):
        for target in ('alpha', 'beta'):
            with self.subTest(target=target):
                with self.store.connect() as db:
                    db.execute(instances.update().where(instances.c.id == target).values(limits=json.dumps({
                        'download_method': 'yt_dlp', 'download_methods': ['yt_dlp'],
                        'scheduler_mode': 'ordered_fallback', 'worker_count': 3})))
                headers = {**self.headers, 'X-Instance-ID': target}
                submitted = {'download_methods': ['downmp3', 'bergung'], 'scheduler_mode': 'auto'}
                response = self.client.put('/api/config', json=submitted, headers=headers)
                self.assertEqual(response.status_code, 200, response.json)
                saved = self.client.get('/api/config', headers=headers).json['config']
                for key, value in submitted.items():
                    self.assertEqual(saved[key], value)
                self.assertEqual(saved['worker_count'], 3)
                fleet = self.apps[target].extensions['fleet']
                self.assertEqual(fleet.local_settings()['config']['download_methods'], submitted['download_methods'])
                item = next(row for row in fleet.list()['items'] if row['id'] == target)
                self.assertEqual(item['policy']['scheduler_mode'], 'auto')
                self.assertEqual(item['policy']['download_methods'], submitted['download_methods'])
                fleet.control(target, {'worker_count': 4})
                overrides = json.loads(fleet.row()['limits'])
                self.assertEqual(overrides, {'worker_count': 4})

    def test_restart_discards_legacy_strategy_overrides_and_keeps_resource_controls(self):
        with self.store.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(heartbeat=0,
                limits=json.dumps({'download_methods': ['yt_dlp'], 'scheduler_mode': 'auto',
                                  'worker_count': 4, 'yt5s_browser_count': 3, 'min_free_gb': 1})))
        app = create_app(self.root / 'alpha/config.yaml', self.root / 'alpha/state', start_worker=False,
                         role='hybrid', instance_id='alpha', database_url=self.url)
        self.addCleanup(app.extensions['task_store'].db.engine.dispose)
        fleet = app.extensions['fleet']
        self.assertEqual(json.loads(fleet.row()['limits']),
                         {'worker_count': 4, 'yt5s_browser_count': 3, 'min_free_gb': 1})
        config = app.test_client().get('/api/config').json['config']
        self.assertEqual(config['download_methods'], ['downmp3'])
        self.assertEqual(config['worker_count'], 4)


if __name__ == '__main__':
    unittest.main()
