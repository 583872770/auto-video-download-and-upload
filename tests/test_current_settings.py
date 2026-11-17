"""Task-center imports and current settings have one execution contract."""
import unittest
from unittest.mock import patch
from tests import test_jobs, test_scale
from webapp.bulk import run_import

class CurrentSettingsTests(unittest.TestCase):
    setUp = test_jobs.JobTests.setUp
    upload = test_scale.ScaleTests.upload
    create = test_jobs.JobTests.create
    control = test_jobs.JobTests.control
    detail = test_jobs.JobTests.detail

    def test_import_is_offline_and_downloads_wait_for_task_start(self):
        with patch('webapp.environment.require_native_environment', side_effect=AssertionError('no tools')):
            imported = self.upload(b'npNnbj46_ik\n2Gu7q-KUXCk\n')
            run_import(self.store, imported['payload'], lambda value: None)
        self.store.finish(imported['id'], result={})
        self.assertIsNone(self.store.claim(self.store.owner, actions=('download',)))
        self.control(self._import_job, 'start')
        first = self.store.claim(self.store.owner, actions=('download',))
        self.assertIn('_delivery_contract', first['payload']['config'])
        self.assertEqual(first['batch_id'], self._import_job)

    def test_next_claim_uses_new_policy_and_keeps_running_attempt(self):
        job = self.create(urls='npNnbj46_ik\n2Gu7q-KUXCk')
        self.control(job, 'start')
        first = self.store.claim(self.store.owner)
        self.assertEqual(self.client.put('/api/config', json={'fragment_concurrency': 8}, headers=self.headers).status_code, 200)
        second = self.store.claim('second-owner')
        self.assertEqual(second['payload']['config']['fragment_concurrency'], 8)
        self.assertEqual(self.store.get(first['id'], private=True)['payload']['config']['fragment_concurrency'], 4)

    def test_retired_modes_are_not_silently_accepted(self):
        for values in ({'youtube_cookie_usage': 'on_demand'}, {'anonymous_fallback': True},
                       {'scheduler_mode': 'auto'}, {'builtin_transfer_limit': 1}):
            self.assertEqual(self.client.put('/api/config', json=values, headers=self.headers).status_code, 400)
