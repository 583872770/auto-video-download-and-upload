"""End-to-end remedies for retained failed tests, including races and statistics."""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from sqlalchemy import select
from tests import test_delivery, test_console_auth
from webapp.database import tasks, delivery_work
from webapp.disk_budget import DiskBudget, file_lock


class FailedTestResolutionTests(unittest.TestCase):
    def setUp(self):
        test_delivery.DeliveryTests.setUp(self)
        # Source fixture intervals are deliberately zero; isolate manual-retry pacing too.
        pacing=patch('webapp.cookie_pool.retry_interval',return_value=0)
        pacing.start();self.addCleanup(pacing.stop)
    info = test_delivery.DeliveryTests.info
    create = test_delivery.DeliveryTests.create

    def make_failed(self, outputs=('metadata', 'audio_only')):
        from web_worker import execute
        self.storage={'storage_mode':'test'}
        job=self.create(outputs)
        task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            result=execute('download',task['payload'],store=self.store,task_id=task['id'])
        self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        local=self.store.claim(self.store.owner,processing=True)
        self.store.finish(local['id'],error='本地转换失败',worker_id=self.store.owner,retryable=False)
        with self.store.connect() as db:
            work=dict(db.execute(select(delivery_work).where(delivery_work.c.task_id==task['id'])).mappings().one())
        response=self.client.get('/api/disk/failed-tests')
        self.assertEqual(response.status_code,200,response.json)
        items=response.json['items']
        item=next(item for item in items if item['task_id']==task['id'])
        return job,task,work,item

    def resolve(self,item,action,**kwargs):
        return self.client.post('/api/disk/failed-tests/resolve',json={
            'task_id':item['task_id'],'version':item['version'],'action':action,'confirmed':True,**kwargs},headers=self.headers)

    def test_discard_clears_warning_and_files_preserves_failed_record_and_statistics(self):
        job,task,work,item=self.make_failed()
        before=self.client.get('/api/jobs/'+job).json['test_file_bytes']
        self.assertGreater(before,0)
        self.assertNotIn('workspace',item)
        response=self.resolve(item,'discard')
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['files_removed'])
        self.assertFalse(Path(work['workspace']).exists())
        self.assertEqual(self.store.get(task['id'])['status'],'failed')
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_bytes'],before)
        self.assertEqual(self.client.get('/api/disk/failed-tests').json['items'],[])
        self.assertEqual(self.client.get('/api/disk').json['diagnostic']['retention']['completed_count'],0)
        self.assertEqual(self.resolve(item,'discard').status_code,409)

    def test_retry_resets_local_attempts_and_completes_without_network(self):
        from web_worker import execute
        job,task,work,item=self.make_failed()
        self.assertEqual(self.resolve(item,'retry').status_code,200)
        self.assertTrue(Path(work['workspace']).exists())
        local=self.store.claim(self.store.owner,processing=True)
        self.assertEqual(local['attempts'],1)
        self.assertEqual(local['id'],task['id'])
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',side_effect=AssertionError('source request')):
            result=execute('download',local['payload'],store=self.store,task_id=task['id'],worker_slot=-2)
        self.assertTrue(self.store.finish(task['id'],result=result,worker_id=self.store.owner))
        from webapp.delivery.processing import cleanup_completed
        self.assertTrue(cleanup_completed(self.store,result['_processing_workspace']))
        self.assertEqual(self.store.get(task['id'])['status'],'succeeded')

    def test_redownload_removes_only_old_workspace_requeues_network_preserves_stats(self):
        from web_worker import execute
        job,task,work,item=self.make_failed()
        before=self.client.get('/api/jobs/'+job).json['test_file_count']
        response=self.resolve(item,'redownload')
        self.assertEqual(response.status_code,200,response.json)
        self.assertFalse(Path(work['workspace']).exists())
        self.assertIsNone(self.store.claim(self.store.owner,processing=True))
        network=self.store.claim(self.store.owner)
        self.assertEqual(network['id'],task['id'])
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            result=execute('download',network['payload'],store=self.store,task_id=task['id'])
        self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_count'],before)

    def test_stale_action_after_retry_cannot_discard_new_attempt(self):
        _,task,work,item=self.make_failed()
        self.assertEqual(self.resolve(item,'retry').status_code,200)
        local=self.store.claim(self.store.owner,processing=True)
        self.store.finish(local['id'],error='still failed',worker_id=self.store.owner)
        self.assertEqual(self.resolve(item,'discard').status_code,409)
        self.assertTrue(Path(work['workspace']).exists())

    def test_locked_workspace_is_never_mutated(self):
        _,task,work,item=self.make_failed()
        with file_lock(self.store.directory/'disk-locks'/(work['disk_lease']+'.lock')) as locked:
            self.assertTrue(locked)
            self.assertEqual(self.resolve(item,'discard').status_code,409)
        self.assertEqual(self.store.get(task['id'])['status'],'failed')
        self.assertTrue(Path(work['workspace']).exists())

    def test_running_and_cross_instance_work_are_refused(self):
        _,task,work,item=self.make_failed()
        with self.store.connect() as db:
            db.execute(delivery_work.update().where(delivery_work.c.task_id==task['id']).values(instance_id='other'))
        self.assertEqual(self.client.get('/api/disk/failed-tests').json['items'],[])
        self.assertEqual(self.resolve(item,'discard').status_code,409)
        with self.store.connect() as db:
            db.execute(delivery_work.update().where(delivery_work.c.task_id==task['id']).values(instance_id='local'))
            db.execute(tasks.update().where(tasks.c.id==task['id']).values(status='running'))
        self.assertEqual(self.resolve(item,'discard').status_code,409)
        self.assertTrue(Path(work['workspace']).exists())

    def test_unrecorded_receipt_prevents_destructive_cleanup(self):
        _,_,work,item=self.make_failed()
        receipt=Path(work['workspace'])/'.completed-files.json'
        values=json.loads(receipt.read_text());values[0]['artifact']['id']='0'*64
        receipt.write_text(json.dumps(values))
        self.assertEqual(self.resolve(item,'discard').status_code,409)
        self.assertTrue(receipt.exists())

    def test_deletion_failure_is_retried_by_sweeper_after_plan_retires(self):
        _,_,work,item=self.make_failed()
        with patch('webapp.delivery.retention.shutil.rmtree',side_effect=PermissionError('busy')):
            response=self.resolve(item,'discard')
        self.assertEqual(response.status_code,200,response.json)
        self.assertFalse(response.json['files_removed'])
        self.assertTrue(Path(work['workspace']).exists())
        budget=DiskBudget(self.store.directory,self.store.runtime())
        budget.cleanup(self.store,grace=0)
        self.assertFalse(Path(work['workspace']).exists())

    def test_confirmation_csrf_and_admin_are_required(self):
        _,_,_,item=self.make_failed()
        self.assertEqual(self.resolve(item,'discard',confirmed=False).status_code,400)
        self.assertEqual(self.client.post('/api/disk/failed-tests/resolve',json={}).status_code,403)
        auth=test_console_auth.ConsoleAuthTests
        self.token=lambda client:auth.token(self,client)
        auth.setup_admin(self)
        for role in ('viewer','operator'):
            self.client.post('/api/auth/users',json=dict(username=role,role=role,password='secure-test-password'),headers=self.headers)
            client,headers=auth.login(self,role)
            self.assertEqual(client.get('/api/disk/failed-tests').status_code,200)
            self.assertEqual(client.post('/api/disk/failed-tests/resolve',json=dict(task_id=item['task_id'],
                version=item['version'],action='discard',confirmed=True),headers=headers).status_code,403)

    def test_invalid_source_stops_conversion_retries_and_requires_redownload(self):
        from web_worker import execute
        from webapp.worker.queue import drain_queue
        _,task,work,item=self.make_failed(('metadata','video_only'))
        plan=json.loads(work['plan'])
        source=Path(plan['jobs'][0]['inputs'][0])
        shutil.copyfile(self.media/'audio.m4a',source)
        self.assertEqual(self.resolve(item,'retry').status_code,200)
        with patch('webapp.worker.idle.PROCESSING_IDLE_SECONDS',0):
            drain_queue(self.store.directory,slot=-2,role='processing',execute_task=execute,store_factory=lambda *args:self.store)
        done=self.store.get(task['id'])
        self.assertEqual(done['status'],'failed')
        self.assertEqual(done['attempts'],1)
        self.assertEqual(done['progress']['local_recovery'],'redownload')
        fresh=self.client.get('/api/disk/failed-tests').json['items'][0]
        self.assertEqual(fresh['recommendation'],'redownload')
        self.assertEqual(self.resolve(fresh,'retry').status_code,409)
        self.assertEqual(self.resolve(fresh,'redownload').status_code,200)

    def test_truncated_mp4_is_rejected_even_with_parseable_header(self):
        from webapp.delivery.verify import inspect, InvalidSourceMedia
        from types import SimpleNamespace
        with patch('webapp.delivery.verify.subprocess.run',return_value=SimpleNamespace(
                returncode=0,stderr='stream 0, offset 0xb4c4: partial file',stdout='{"streams":[]}')):
            with self.assertRaises(InvalidSourceMedia):inspect(self.media/'video.mp4')
