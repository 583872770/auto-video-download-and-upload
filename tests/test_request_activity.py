import os
import tempfile
import time
import unittest
from unittest.mock import patch
from sqlalchemy import select
from webapp.store import TaskStore
from webapp.database import tasks, download_permits
from webapp.request_scheduler import RequestScheduler
from webapp.throughput import PipelineBusy
from webapp.downloads import DownloadLeaseLost


class RequestActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=TaskStore(self.tmp.name);self.addCleanup(self.store.db.engine.dispose)
        self.task=self.store.create('download',{'urls':['https://youtu.be/jNQXAC9IVRw']})
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id==self.task['id']).values(
                status='running',worker_id=self.store.owner,lease_until=time.time()+60))
        self.router=RequestScheduler(self.store,{},self.task['id'])

    def test_redis_failure_defers_request_instead_of_bypassing_gate(self):
        with patch.object(self.store.progress_cache,'_call',return_value=None):
            with self.assertRaisesRegex(PipelineBusy,'Redis'):
                with self.router.permit('transfer'):self.fail('request entered')

    def test_previous_owner_cannot_start_request_or_publish_activity(self):
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id==self.task['id']).values(worker_id='replacement'))
        with patch.object(self.store.progress_cache,'_call') as redis:
            with self.assertRaises(DownloadLeaseLost):
                with self.router.permit('transfer'):pass
        redis.assert_not_called()

    @unittest.skipUnless(os.environ.get('REDIS_URL'),'real isolated Redis required')
    def test_real_redis_cleans_activity_on_exception_and_keeps_sql_account_permits_separate(self):
        cache=self.store.progress_cache;keys=[]
        call=cache._call
        def recorded(method,*args,**kwargs):
            if method=='set':keys.append(args[0])
            return call(method,*args,**kwargs)
        with patch.object(cache,'_call',side_effect=recorded):
            with self.assertRaisesRegex(RuntimeError,'transfer fixture'):
                with self.router.permit('transfer'):
                    self.assertIsNotNone(call('get',keys[-1]))
                    with self.store.connect() as db:
                        self.assertEqual(db.execute(select(download_permits)).all(),[])
                    raise RuntimeError('transfer fixture')
        self.assertIsNone(call('get',keys[-1]))
