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
    def test_zero_unlimited_settings_save_reload_and_reject_negative_atomically(self):
        keys = ('youtube_cookie_pool_worker_limit', 'youtube_hourly_start_limit', 'youtube_daily_start_limit')
        response = self.client.put('/api/config', json=dict.fromkeys(keys, 0), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        saved = self.client.get('/api/config').json['config']
        self.assertEqual({k:saved[k] for k in keys}, dict.fromkeys(keys, 0))
        for key in keys:
            response = self.client.put('/api/config', json={key: -1}, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(self.client.get('/api/config').json['config'][key], 0)

    def test_database_budget_roundtrip_rejects_impossible_save_atomically(self):
        import yaml
        response = self.client.put('/api/config', json={
            'db_budget_mode': 'manual', 'db_connection_budget': 48,
            'youtube_cookie_pool_worker_limit': 21}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        saved = self.client.get('/api/config').json
        self.assertEqual(saved['database_budget']['mode'], 'manual')
        self.assertEqual(saved['config']['db_connection_budget'], 48)
        disk = yaml.safe_load(Path(saved['config_path']).read_text(encoding='utf-8'))
        self.assertEqual(disk['db_budget_mode'], 'manual')
        for values in ({'db_connection_budget': 8}, {'db_budget_mode': 'invalid'},
                       {'db_connection_budget': True}, {'db_connection_budget': 10001}):
            response = self.client.put('/api/config', json=values, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(self.client.get('/api/config').json['config']['db_connection_budget'], 48)
        response = self.client.put('/api/config', json={'db_budget_mode': 'auto'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.client.get('/api/config').json['database_budget']['mode'], 'auto')

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

    def test_job_has_no_policy_override_columns(self):
        identity = self.create()
        self.assertNotIn('policy', job_profiles.c)
        self.assertNotIn('policy_version', job_profiles.c)
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




if __name__ == '__main__':
    unittest.main()
