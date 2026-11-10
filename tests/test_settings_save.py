"""Instance-policy saves preserve current attempts and affect subsequent claims."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import select

from app import create_app
from tests import test_hybrid, test_scale
from webapp.database import instances, job_profiles, settings


class SettingsSaveContract:
    def test_task_rest_is_saved_as_local_instance_setting_and_reaches_next_claim(self):
        key = 'youtube_task_rest_interval'
        self.assertEqual(self.client.get('/api/config').json['config'][key], 0)
        identity = self.create()
        self.start(identity)
        first = self.store.claim(self.store.owner)
        self.assertEqual(first['payload']['config'][key], 0)
        response = self.client.put('/api/config', json={key: 30}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.client.get('/api/config').json['config'][key], 30)
        self.assertEqual(self.store.get(first['id'], private=True)['payload']['config'][key], 0)
        self.assertEqual(self.store.claim('next-owner')['payload']['config'][key], 30)
        for invalid in (-1, 3601, True, 1.5):
            response = self.client.put('/api/config', json={key: invalid}, headers=self.headers)
            self.assertEqual(response.status_code, 400)

    def test_cached_frontend_retired_connection_field_is_ignored_and_not_persisted(self):
        response = self.client.put('/api/config', json={
            'builtin_connection_limit': 1, 'download_connection_limit': 24}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        saved = self.client.get('/api/config').json
        self.assertNotIn('builtin_connection_limit', saved['config'])
        self.assertEqual(saved['config']['download_connection_limit'], 24)
        import yaml
        stored = yaml.safe_load(Path(saved['config_path']).read_text(encoding='utf-8'))
        self.assertNotIn('builtin_connection_limit', stored)
        self.assertEqual(stored['download_connection_limit'], 24)

    def test_pool_start_interval_saves_and_rejects_invalid_values(self):
        key = 'youtube_cookie_pool_start_interval'
        self.assertEqual(self.client.get('/api/config').json['config'][key], 5)
        for value in (3, 0, 600):
            with self.subTest(value=value):
                response = self.client.put('/api/config', json={key: value}, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(self.client.get('/api/config').json['config'][key], value)
                if self.store.fleet:
                    self.assertEqual(self.store.runtime()[key], value)
                else:
                    with self.store.connect() as db:
                        saved = json.loads(db.execute(select(settings.c.value).where(
                            settings.c.key == 'download_settings')).scalar_one())
                    self.assertEqual(saved['config'][key], value)
        for value in (-1, 601, True, 1.5, 'bad'):
            with self.subTest(invalid=value):
                response = self.client.put('/api/config', json={key: value}, headers=self.headers)
                self.assertEqual(response.status_code, 400, response.json)
                self.assertEqual(self.client.get('/api/config').json['config'][key], 600)

    def test_pool_interval_change_applies_to_next_claim_without_rewriting_running_task(self):
        key = 'youtube_cookie_pool_start_interval'
        identity = self.create()
        self.start(identity)
        first = self.store.claim(self.store.owner)
        self.assertEqual(first['payload']['config'][key], 5)
        response = self.client.put('/api/config', json={key: 3}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.store.get(first['id'], private=True)['payload']['config'][key], 5)
        self.assertEqual(self.store.claim('next-owner')['payload']['config'][key], 3)

    def test_cookie_session_limit_saves_as_instance_policy(self):
        response = self.client.put('/api/config', json={'youtube_cookie_session_limit': 5}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.client.get('/api/config').json['config']['youtube_cookie_session_limit'], 5)
        if self.store.fleet:
            self.assertEqual(self.store.runtime()['youtube_cookie_session_limit'], 5)
        else:
            with self.store.connect() as db:
                saved = json.loads(db.execute(select(settings.c.value).where(settings.c.key == 'download_settings')).scalar_one())
            self.assertEqual(saved['config']['youtube_cookie_session_limit'], 5)

    def test_adaptive_pacing_saves_as_instance_policy(self):
        submitted = {'youtube_pacing_mode': 'adaptive', 'youtube_start_interval': 120,
                     'youtube_hourly_start_limit': 80, 'youtube_daily_start_limit': 1200}
        response = self.client.put('/api/config', json=submitted, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        saved = self.client.get('/api/config').json['config']
        for key, value in submitted.items():
            self.assertEqual(saved[key], value)

    def test_routing_save_preserves_explicit_concurrency_mode(self):
        for mode, routing in (('manual', 'auto'), ('auto', 'ordered_fallback')):
            response = self.client.put('/api/config', json={
                'throughput_mode': mode, 'scheduler_mode': routing}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            saved = self.client.get('/api/config').json['config']
            self.assertEqual(saved['throughput_mode'], mode)
            self.assertEqual(saved['scheduler_mode'], routing)

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
