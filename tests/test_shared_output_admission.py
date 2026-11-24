"""All output types use account-owned source permits with no second transfer cap."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import select
from tests.account_cookie_fixture import AccountCookieProfiles
from webapp.cookie_pool import CookiePool
from webapp.request_scheduler import RequestScheduler
from webapp.database import download_permits, tasks
from webapp.download_metrics import DownloadMeter
from webapp.downloads import DownloadLeaseLost, DownloadProgress
from webapp.store import TaskStore
from webapp.throughput import PipelineBusy
from webapp.worker.pipeline import SourceExecution, run_download


class SharedOutputAdmissionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = TaskStore(self.root/'state');self.addCleanup(self.store.db.engine.dispose)
        self.now = time.time()
        profiles = AccountCookieProfiles(self.root, self.store)
        self.ids = [profiles.add(str(i), f'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-{i}\n'.encode())['id'] for i in range(3)]
        self.config = dict(youtube_cookie_pool=self.ids, youtube_cookie_pool_start_interval=0,
            youtube_start_interval=0, youtube_hourly_start_limit=0, youtube_daily_start_limit=0)

    def pool(self):
        task = self.store.create('download', {'urls':['https://youtu.be/jNQXAC9IVRw']})
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id==task['id']).values(status='running',
                worker_id=self.store.owner, lease_until=self.now+86400))
        pool = CookiePool(self.store, self.config, self.root, task['id'])
        pool.router.clock = lambda db:self.now
        return pool

    def executor(self, pool, config, kind):
        router = RequestScheduler(self.store, config, pool.router.task_id)
        router.clock = lambda db:self.now
        progress = DownloadProgress(lambda value:None);progress.state['output_kind']=kind
        meter = DownloadMeter(self.store.directory);meter.begin()
        return SourceExecution(SimpleNamespace(scheduler=router, progress=progress,
            meter=meter, transfer_seconds=0, failure_phase='resolve'))

    def test_accounts_transfer_all_output_types_without_a_second_cap(self):
        with ExitStack() as stack:
            executors = []
            for kind in ('video_only','audio_only','subtitle'):
                pool = self.pool();config = stack.enter_context(pool.assignment())
                executor = self.executor(pool,config,kind)
                stack.enter_context(executor.transmitting())
                self.assertEqual(executor.session.progress.state['stage'],'downloading')
                executors.append(executor)
            self.assertTrue(all(e.session.progress.state['transfer_admitted'] for e in executors))
            _, active, _ = pool.router.snapshot([pool.global_scope, 'source-transfer:local'])
            self.assertEqual(active[pool.global_scope], 3)
            self.assertNotIn('source-transfer:local', active)
        with self.store.connect() as db:self.assertEqual(db.execute(select(download_permits)).all(),[])

    def test_video_and_subtitle_share_account_start_interval(self):
        self.config.update(youtube_cookie_pool=[self.ids[0]],youtube_start_interval=55)
        first=self.pool()
        with first.assignment() as config:
            with self.executor(first,config,'video_only').session.scheduler.permit('resolve'):pass
        second=self.pool()
        with self.assertRaises(PipelineBusy):
            with second.assignment():self.fail('Started a subtitle before the account deadline')
        self.now+=71
        with second.assignment() as config:
            with self.executor(second,config,'subtitle').transmitting():pass

    def test_failures_release_source_and_account_permits(self):
        for kind in ('video_only','audio_only','subtitle','thumbnail'):
            self.now+=71
            pool=self.pool()
            with patch('webapp.cookie_pool.account_interval',return_value=0), patch('webapp.cookie_pool.random.uniform',return_value=0):
                with self.assertRaisesRegex(RuntimeError,'before first progress'):
                    with pool.assignment() as config:
                        executor=self.executor(pool,config,kind)
                        with executor.transmitting():
                            self.assertEqual(executor.session.failure_phase,'transfer')
                            raise RuntimeError('before first progress')
            with self.store.connect() as db:self.assertEqual(db.execute(select(download_permits)).all(),[])

    def test_lost_owner_cannot_start_subtitle_transfer(self):
        pool=self.pool()
        with pool.assignment() as config:
            executor=self.executor(pool,config,'subtitle')
            with self.store.connect() as db:db.execute(tasks.update().where(tasks.c.id==pool.router.task_id).values(worker_id='replacement'))
            with self.assertRaises(DownloadLeaseLost):
                with executor.transmitting():self.fail('Stale owner admitted')

    def test_source_outage_requires_distinct_tasks_and_429_stays_local(self):
        pool=self.pool();router=pool.router
        for _ in range(3):router.outcome(success=False,error=RuntimeError('HTTP Error 503'))
        self.assertFalse(router.transaction_call(lambda db,s:router._state(db,'source:youtube')).get('cooldown_until'))
        for _ in range(2):self.pool().router.outcome(success=False,error=RuntimeError('HTTP Error 503'))
        before=router.transaction_call(lambda db,s:router._state(db,'source:youtube'))
        self.assertGreater(before['cooldown_until'],self.now)
        router.outcome(success=False,error=RuntimeError('HTTP Error 429'))
        self.assertEqual(router.transaction_call(lambda db,s:router._state(db,'source:youtube')),before)

    def test_no_anonymous_or_untyped_execution(self):
        with self.assertRaisesRegex(RuntimeError,'账号池'):run_download({'base':str(self.root),'config':{}})
        with self.assertRaisesRegex(RuntimeError,'交付配置'):run_download({'base':str(self.root),'config':{'_cookie_pool_selected':'fixture'}},store=self.store)

    def test_meter_counts_caption_and_video_bytes_once(self):
        meter=DownloadMeter(self.store.directory);meter.begin()
        for filename,size in [('video.mp4',1000),('caption.vtt',50)]:
            for status in ('downloading','finished'):meter.hook(dict(filename=filename,downloaded_bytes=size,status=status))
        self.assertEqual(meter.data['received_bytes'],1050)
