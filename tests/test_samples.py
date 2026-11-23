"""Real test files, bounded retention, durable review and forced fresh acquisition."""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event, select
from tests import test_delivery as fixture, test_console_auth
from webapp.database import test_samples as samples, delivery_artifacts, tasks
from webapp.delivery import samples as retention
from webapp.disk_budget import DiskBudget, file_lock


class SampleTests(unittest.TestCase):
    setUp = fixture.DeliveryTests.setUp
    create = fixture.DeliveryTests.create
    execute = fixture.DeliveryTests.execute
    run_task = fixture.DeliveryTests.run_task
    info = fixture.DeliveryTests.info
    cleanup_result = fixture.DeliveryTests.cleanup_result

    def configure(self, job, **values):
        url = f'/api/jobs/{job}/sample-policy'
        policy = self.client.get(url).json
        response = self.client.put(url, json={**policy, **values}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def kept(self, job):
        response = self.client.get(f'/api/jobs/{job}/samples')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['items']

    def resample(self, job, task):
        response = self.client.post(f'/api/jobs/{job}/videos/{task["id"]}/resample',
            json={'confirmed': True}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        return self.execute()

    def budget(self):
        return DiskBudget(self.store.directory, self.store.runtime())

    def test_all_real_outputs_preview_range_review_and_cleanup_keep_receipts(self):
        self.storage = {'storage_mode': 'test'}
        job = self.create(['video_only', 'audio_only', 'av', 'subtitle', 'thumbnail', 'metadata'])
        task, result = self.execute()
        sample = self.kept(job)[0]
        self.assertEqual(len(sample['files']), 6)
        self.assertTrue(result['files_deleted'])  # Original workspace retired.
        self.assertTrue(all('source' not in f and 'name' not in f and 'path' not in f for f in sample['files']))
        original = self.client.get('/api/jobs/' + job).json['test_file_bytes']
        for item in sample['files']:
            response = self.client.get(item['content_url'], headers={'Range': 'bytes=0-15'})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(len(response.data), 16)
            response.close()
        subtitle = next(f for f in sample['files'] if f['kind'] == 'subtitle')
        with self.client.get(subtitle['content_url']) as response:
            self.assertIn('Hello world.', response.text)
        with self.client.get(next(f for f in sample['files'] if f['kind'] == 'metadata')['content_url']) as response:
            self.assertEqual(response.json['id'], task['video_id'])
        url = f'/api/jobs/{job}/samples/{sample["id"]}/action'
        response = self.client.post(url, json={'action': 'accept', 'revision': sample['revision']}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        sample = self.kept(job)[0]
        # Opening files protects them against TTL and pressure while viewed.
        with self.store.connect() as db:
            db.execute(samples.update().values(expires=time.time() - 1))
        with patch.object(DiskBudget, '_free', return_value=0):
            self.assertEqual(retention.sweep(self.store, self.budget()), 0)
        with self.store.connect() as db:
            db.execute(samples.update().values(view_until=0))
        self.assertEqual(retention.sweep(self.store, self.budget()), 1)
        self.assertFalse(self.kept(job))
        results = self.client.get(f'/api/jobs/{job}/videos/{task["id"]}/results').json
        self.assertEqual(results['review']['status'], 'accepted')
        self.assertEqual(results['samples'], [])
        self.assertEqual(self.client.get('/api/jobs/' + job).json['test_file_bytes'], original)
        self.assertEqual(self.client.get('/api/jobs/' + job + '/videos').json['items'][0]['file_state'], 'cleaned')
        self.assertEqual(self.client.get(subtitle['content_url']).status_code, 404)

    def test_fast_mode_fresh_resample_fetches_and_does_not_double_bill(self):
        self.storage = {'storage_mode': 'test'}
        job = self.create(['metadata'])
        self.configure(job, enabled=False)
        task, _ = self.execute()
        self.assertEqual(self.kept(job), [])
        before = self.client.get('/api/jobs/' + job).json['test_file_count']
        response = self.client.post(f'/api/jobs/{job}/videos/{task["id"]}/resample', json={'confirmed': True}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        fresh = self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info', return_value=self.info()) as extract:
            result = self.run_task(fresh)
            self.assertGreater(extract.call_count, 0)
        self.assertTrue(self.store.finish(fresh['id'], result=result, worker_id=self.store.owner))
        self.cleanup_result(result)
        self.assertEqual(len(self.kept(job)), 1)
        self.assertNotIn('_force_test_sample', self.store.get(task['id'], private=True)['payload'])
        self.assertEqual(retention.sweep(self.store, self.budget()), 0)
        self.assertEqual(self.client.get('/api/jobs/' + job).json['test_file_count'], before)

    def test_fast_mode_and_ready_sample_do_not_query_all_artifact_receipts(self):
        self.storage = {'storage_mode': 'test'}
        job = self.create(['metadata'])
        self.configure(job, enabled=False)
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT') and 'delivery_artifacts' in statement:
                statements.append(statement)
        event.listen(self.store.db.engine, 'before_cursor_execute', capture)
        try:
            task, _ = self.execute()
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', capture)
        self.assertFalse(any('delivery_artifacts.task_id =' in s and
                             'delivery_artifacts.instance_id =' in s for s in statements), statements)
        self.assertFalse(self.kept(job))
        self.resample(job, task)
        with self.store.connect() as db:
            sample = db.execute(select(samples).where(samples.c.task_id == task['id'],
                samples.c.state == 'ready')).mappings().one()
        statements.clear()
        event.listen(self.store.db.engine, 'before_cursor_execute', capture)
        try:
            retention.capture(self.store, self.budget(),
                {'category': 'test', 'path': sample['workspace'], 'id': sample['id']}, self.store.get(task['id']))
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', capture)
        self.assertEqual(statements, [])

    def test_quantity_rotation_and_global_zero_limit(self):
        self.storage = {'storage_mode': 'test'}
        job = self.create(['metadata'])
        self.configure(job, maximum=1)
        task, _ = self.execute()
        old = self.kept(job)[0]
        self.resample(job, task)
        current = self.kept(job)
        self.assertEqual(len(current), 1)
        self.assertNotEqual(current[0]['id'], old['id'])
        self.assertFalse((self.store.directory / 'test-samples' / old['id']).exists())
        budget = self.budget();budget.config['disk_sample_budget_mb'] = 0
        self.assertEqual(retention.sweep(self.store, budget), 1)

    def test_crash_between_moves_resumes_same_sample(self):
        self.storage = {'storage_mode': 'test'}
        job = self.create(['audio_only', 'video_only', 'metadata'])
        task = self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info', return_value=self.info()):
            result = self.run_task(task)
        self.assertTrue(self.store.finish(task['id'], result=result, worker_id=self.store.owner))
        import os
        original = os.replace
        counter = 0
        def interrupted(source, target):
            nonlocal counter
            if 'test-samples' in Path(target).parts:
                counter += 1
                if counter == 2:
                    raise OSError('simulated crash')
            return original(source, target)
        with patch('webapp.delivery.samples.os.replace', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'simulated crash'):
                self.cleanup_result(result)
        self.assertEqual(self.kept(job), [])
        self.cleanup_result(result)
        sample = self.kept(job)[0]
        self.assertEqual(len(sample['files']), 3)
        self.assertEqual(len(list((self.store.directory / 'test-samples').iterdir())), 1)

    def test_policy_revision_confirmation_and_role_boundaries(self):
        self.storage = {'storage_mode': 'test'}
        job = self.create(['metadata'])
        task, _ = self.execute();sample = self.kept(job)[0]
        url = f'/api/jobs/{job}/sample-policy'
        stale = self.client.get(url).json
        self.configure(job, maximum=2)
        self.assertEqual(self.client.put(url, json=stale, headers=self.headers).status_code, 409)
        action = f'/api/jobs/{job}/samples/{sample["id"]}/action'
        self.assertEqual(self.client.post(action, json={'action': 'delete', 'revision': 1}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(action, json={}).status_code, 403)
        self.assertEqual(self.client.post(f'/api/disk/samples/{sample["id"]}/action', json={}, headers=self.headers).status_code, 403)
        auth = test_console_auth.ConsoleAuthTests
        self.token = lambda client: auth.token(self, client)
        auth.setup_admin(self)
        for role in ('viewer', 'operator'):
            self.client.post('/api/auth/users', json=dict(username=role, role=role, password='secure-test-password'), headers=self.headers)
            client, headers = auth.login(self, role)
            with client.get(sample['files'][0]['content_url']) as response:
                self.assertEqual(response.status_code, 200)
            self.assertEqual(client.put(url, json=self.client.get(url).json, headers=headers).status_code, 403)
            self.assertEqual(client.post(action, json=dict(action='delete', revision=1, confirmed=True), headers=headers).status_code, 403)
            self.assertEqual(client.post(action, json=dict(action='accept', revision=1), headers=headers).status_code, 403 if role == 'viewer' else 200)

    def test_global_exact_id_sample_filter_and_small_batch_queries(self):
        self.storage = {'storage_mode': 'test'}
        job = self.create(['metadata'])
        task, _ = self.execute()
        response = self.client.get('/api/jobs/videos?q=npNnbj46_ik&samples_only=1')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual([r['id'] for r in response.json['items']], [task['id']])
        self.assertEqual(response.json['items'][0]['file_state'], 'sample')
        self.assertEqual(self.client.get('/api/jobs/videos?q=wrong').status_code, 400)
        self.assertEqual(self.client.get('/api/jobs/videos?q=NpNnbj46_ik').json['items'], [])

    def test_observation_http_events_separate_from_final_success(self):
        self.storage = {'storage_mode': 'test'}
        job = self.create(['metadata'])
        task, _ = self.execute()
        from webapp.delivery.observation import incident
        from webapp.download_policy import failure_evidence
        evidence = failure_evidence(RuntimeError('HTTP Error 403: Forbidden'), task_id=task['id'], profile_id=self.profile)
        incident(self.store, task, evidence, {'storage_mode': 'test'})
        incident(self.store, task, evidence, {'storage_mode': 'test'})
        response = self.client.get(f'/api/jobs/{job}/observation?hours=24')
        self.assertEqual(response.status_code, 200, response.json)
        value = response.json
        self.assertEqual(value['totals']['succeeded'], 1)
        self.assertEqual(value['totals']['http403'], 1)
        self.assertEqual(value['totals']['failed'], 0)
        self.assertEqual(value['success_rate'], 100)
        self.assertEqual(value['details'][0]['status'], 'succeeded')
        self.assertEqual(self.client.get(f'/api/jobs/{job}/observation?hours=2000').status_code, 400)

    def test_expired_failed_test_reclaims_unprocessed_files_without_losing_receipts(self):
        from tests.test_retention import FailedTestResolutionTests
        from datetime import datetime, timezone, timedelta
        from webapp.database import delivery_work
        job, task, work, _ = FailedTestResolutionTests.make_failed(self)
        before = self.client.get('/api/jobs/' + job).json['test_file_bytes']
        self.assertGreater(before, 0)
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                finished_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()))
        self.budget().cleanup(self.store, grace=0)
        self.assertFalse(Path(work['workspace']).exists())
        with self.store.connect() as db:
            self.assertFalse(db.execute(select(delivery_work)).first())
        self.assertEqual(self.store.get(task['id'])['status'], 'failed')
        self.assertEqual(self.client.get('/api/jobs/' + job).json['test_file_bytes'], before)

    def test_sample_owner_routing_and_rpc_boundary(self):
        from unittest.mock import MagicMock
        from webapp.console_auth import rpc_allowed
        self.storage = {'storage_mode': 'test'}
        job = self.create(['metadata']);self.execute();sample = self.kept(job)[0]
        with self.store.connect() as db:
            db.execute(samples.update().values(instance_id='worker-two'))
        fleet = MagicMock();fleet.id = 'manager'
        with patch.object(self.store, 'fleet', fleet), patch('webapp.adapters.fleet.rpc', return_value={'message': 'saved'}) as rpc:
            response = self.client.post(f'/api/jobs/{job}/samples/{sample["id"]}/action',
                json={'action': 'accept', 'revision': 1}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(rpc.call_args.args[1], 'worker-two')
            self.assertEqual(rpc.call_args.kwargs['data']['job_id'], job)
        for path, method, expected in [
            (f'/api/disk/samples/{sample["id"]}/files/{sample["files"][0]["id"]}', 'GET', True),
            (f'/api/disk/samples/{sample["id"]}/action', 'POST', True),
            (f'/api/jobs/{job}/sample-policy', 'PUT', False),
            (f'/api/jobs/{job}/videos/{sample["task_id"]}/resample', 'POST', False)]:
            with self.app.test_request_context(path, method=method):
                self.assertEqual(rpc_allowed(), expected)

    def test_preview_waits_for_brief_sample_maintenance(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.storage = {'storage_mode': 'test'}
        job = self.create(['metadata']);self.execute();sample = self.kept(job)[0]
        requested = Event()
        def fetch():
            requested.set()
            with self.app.test_client() as client:
                with client.get(sample['files'][0]['content_url']) as response:
                    return response.status_code, response.json['id']
        with ThreadPoolExecutor(max_workers=1) as pool:
            with file_lock(self.store.directory / 'sample-maintenance.lock') as owned:
                self.assertTrue(owned)
                future = pool.submit(fetch)
                self.assertTrue(requested.wait(2))
                time.sleep(.15)
                self.assertFalse(future.done(), 'A short maintenance lock must not reject preview immediately')
            self.assertEqual(future.result(timeout=10), (200, 'npNnbj46_ik'))
