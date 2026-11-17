"""The retained instance policy is strict, atomic and pinned per attempt."""
import unittest
from unittest.mock import patch
from tests import test_jobs, test_hybrid

class SettingsSaveContract:
    def save(self, values):
        return self.client.put('/api/config', json=values, headers=self.headers)

    def test_zero_limits_and_invalid_save_is_atomic(self):
        keys = ('download_task_limit', 'youtube_hourly_start_limit', 'youtube_daily_start_limit')
        self.assertEqual(self.save(dict.fromkeys(keys, 0)).status_code, 200)
        before = self.client.get('/api/config').json['config']
        for key in keys:
            self.assertEqual(self.save({key: -1}).status_code, 400)
            self.assertEqual(self.client.get('/api/config').json['config'], before)

    def test_only_automatic_database_budget_is_supported(self):
        self.assertEqual(self.client.get('/api/config').json['database_budget']['mode'], 'auto')
        for value in ({'db_budget_mode': 'manual'}, {'db_connection_budget': 48},
                      {'download_connection_limit': 64}, {'youtube_cookie_usage': 'always'},
                      {'download_methods': ['yt_dlp']}):
            self.assertEqual(self.save(value).status_code, 400)

    def test_one_pool_interval_and_per_file_fragment_limit(self):
        for interval in (0, 3, 600):
            self.assertEqual(self.save({'youtube_cookie_pool_start_interval': interval,
                                       'fragment_concurrency': 8}).status_code, 200)
            config = self.client.get('/api/config').json['config']
            self.assertEqual(config['youtube_cookie_pool_start_interval'], interval)
            self.assertEqual(config['fragment_concurrency'], 8)
        for value in (-1, 601, True, 1.5):
            self.assertEqual(self.save({'youtube_cookie_pool_start_interval': value}).status_code, 400)

    def test_saving_policy_does_not_rewrite_inflight_snapshot(self):
        created = self.client.post('/api/jobs', json={'customer': 'Fixture', 'name': 'Pinned',
            'urls': 'npNnbj46_ik\n2Gu7q-KUXCk'}, headers=self.headers)
        self.assertEqual(created.status_code, 201, created.json)
        with patch('webapp.environment.require_native_environment'):
            started = self.client.post('/api/jobs/' + created.json['id'] + '/control',
                json={'action': 'start', 'resume_system': True}, headers=self.headers)
        self.assertEqual(started.status_code, 200, started.json)
        first = self.store.claim(self.store.owner)
        previous = first['payload']['config']['youtube_start_interval']
        self.assertEqual(self.save({'youtube_start_interval': 55}).status_code, 200)
        self.assertEqual(self.store.get(first['id'], private=True)['payload']['config']['youtube_start_interval'], previous)
        self.assertEqual(self.store.claim('next-owner')['payload']['config']['youtube_start_interval'], 55)
        self.assertTrue(self.store.heartbeat(first['id'], self.store.owner))

    def test_storage_is_only_configured_on_task(self):
        before = self.client.get('/api/config').json['config']
        for values in ({'path': 'other'}, {'storage_mode': 'test'}, {'shard_downloads': False}):
            self.assertEqual(self.save(values).status_code, 400)
        self.assertEqual(self.client.get('/api/config').json['config'], before)

class StandaloneSettingsSaveTests(SettingsSaveContract, unittest.TestCase):
    setUp = test_jobs.JobTests.setUp

class HybridSettingsSaveTests(SettingsSaveContract, unittest.TestCase):
    serve = test_hybrid.HybridTests.serve
    def setUp(self):
        test_hybrid.HybridTests.setUp(self)
        self.store = self.a
        self.client = self.clients['alpha']
        self.headers = self.headers['alpha']
