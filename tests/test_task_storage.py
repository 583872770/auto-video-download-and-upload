"""Scope boundaries: tasks own destinations, instances own execution resources."""
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import select
from tests.test_scale import ScaleTests
from webapp.database import delivery_artifacts, delivery_contracts, delivery_runs, tasks
from webapp.delivery.artifacts import current, save_contract, storage_config
from webapp.task_storage import FIELDS, normalize


class TaskStorageTests(TestCase):
    setUp = ScaleTests.setUp

    def create(self, directory):
        response = self.client.post('/api/jobs', json={'customer': 'C', 'name': directory,
            'urls': 'npNnbj46_ik', 'delivery': {'storage': {'path': directory}}}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['id']

    def update(self, job, directory):
        detail = self.client.get('/api/jobs/' + job).json
        spec = detail['delivery']['contract']['spec']
        spec['storage']['path'] = directory
        return self.client.put('/api/jobs/' + job, json={'edit_version': detail['edit_version'],
            'delivery': spec}, headers=self.headers)

    def test_instance_api_rejects_task_fields_and_never_returns_them(self):
        self.assertFalse(FIELDS & self.client.get('/api/config').json['config'].keys())
        for key in ('path', 'storage_mode', 'oss_bucket'):
            response = self.client.put('/api/config', json={key: 'invalid'}, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.json)

    def test_two_tasks_keep_independent_rules_and_execution_pins_destination(self):
        from webapp.database import download_lists
        a, b = self.create('customer-a'), self.create('customer-b')
        with self.store.connect() as db:
            db.execute(download_lists.update().values(released=1))
        task = self.store.claim(self.store.owner)
        self.assertEqual(task['batch_id'], a)
        self.assertEqual(task['payload']['config']['path'], 'customer-a')
        self.assertEqual(self.update(a, 'changed-a').status_code, 409)
        self.assertEqual(self.update(b, 'changed-b').status_code, 200)
        # An ordinary instance policy write cannot alter the claimed destination.
        self.assertEqual(self.client.put('/api/config', json={'download_task_limit': 3}, headers=self.headers).status_code, 200)
        self.assertEqual(self.store.effective_download_payload(task['id'], task['payload'])['config']['path'], 'customer-a')
        self.store.finish(task['id'], result={}, worker_id=self.store.owner)
        self.assertEqual(self.update(a, 'future-a').status_code, 200)
        # Retry uses the pinned version until an explicit "use latest" repair.
        with self.store.connect() as db:
            self.assertEqual(json.loads(db.execute(select(delivery_runs.c.spec).where(
                delivery_runs.c.task_id == task['id'])).scalar_one())['storage']['path'], 'customer-a')
        next_task = self.store.claim(self.store.owner)
        self.assertEqual(next_task['payload']['config']['path'], 'changed-b')

    def test_storage_secret_is_task_owned_and_environment_is_not_a_fallback(self):
        value = {'storage_mode': 'oss', 'oss_endpoint': 'oss-cn-hangzhou.aliyuncs.com',
                 'oss_bucket': 'scope-fixture', 'oss_access_key_id': 'fixture', 'oss_access_key_secret': 'saved-secret'}
        with self.store.connect() as db:
            first = save_contract(db, 'j' * 32, {'storage': value})
            second = save_contract(db, 'j' * 32, {'outputs': ['subtitle'],
                'storage': {**value, 'oss_access_key_secret': ''}})
            self.assertEqual(second['spec']['storage']['oss_access_key_secret'], 'saved-secret')
            self.assertEqual(first['spec']['storage']['oss_endpoint'], 'https://oss-cn-hangzhou.aliyuncs.com')
        with patch.dict('os.environ', {'OSS_ACCESS_KEY_SECRET': 'env-secret', 'OSS_ACCESS_KEY_ID': 'env-id'}):
            with self.assertRaises(ValueError):
                normalize({**value, 'oss_access_key_secret': '', 'oss_access_key_id': ''})

    def test_artifact_destination_survives_new_contract_and_run_version(self):
        with self.store.connect() as db:
            save_contract(db, 'j' * 32, {'storage': {'oss_bucket': 'original-bucket'}})
            save_contract(db, 'j' * 32, {'storage': {'oss_bucket': 'new-bucket'}})
            self.assertEqual(storage_config(db, {'job_id': 'j' * 32, 'contract_version': 1})['oss_bucket'], 'original-bucket')
            self.assertEqual(current(db, 'j' * 32)['spec']['storage']['oss_bucket'], 'new-bucket')

    def test_invalid_subdirectories_rejected_before_any_filesystem_operation(self):
        for directory in ('../outside', '/absolute', 'C:\\outside', 'a/./b', 'a/../b'):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                normalize({'dir': directory})
