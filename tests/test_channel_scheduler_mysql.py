"""Run the same admission contracts against an isolated MySQL database.

Uses VIDEO_TEST_MYSQL_URL with the existing test administrator fixture;
never point that variable at the application database.
"""
from tests import test_channel_scheduler as contract
from tests import test_mysql as mysql
from webapp.store import TaskStore
from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from webapp.database import download_permits, tasks
from webapp.channel_scheduler import ChannelScheduler


class MySQLChannelSchedulerTests(mysql.MySQLFixture, contract.AdmissionTests):
    def setUp(self):
        mysql.MySQLFixture.setUp(self)
        self.first = TaskStore(self.root / 'a', self.store.db.url)
        self.second = TaskStore(self.root / 'b', self.store.db.url)
        self.config = {'download_method':'bergung', 'worker_count':8,
            'download_channels':[contract.channel('bergung', transfer_limit=1),
                                 contract.channel('alias'), contract.channel('other','independent')]}

    def test_parallel_admission_and_release_leave_no_permits(self):
        jobs = [self.first.create('download', {'base': str(self.root), 'config': {},
                    'urls': [f'https://example.test/{index}']}) for index in range(8)]
        with self.first.connect() as db:
            db.execute(tasks.update().where(tasks.c.id.in_([job['id'] for job in jobs])).values(
                status='running', worker_id=self.first.owner, lease_until=time.time() + 120))
        def transfer(job):
            scheduler = ChannelScheduler(self.first, {'download_method': 'yt_dlp'}, job['id'])
            for _ in range(5):
                with scheduler.permit(scheduler.channels[0], 'transfer', timeout=10):
                    time.sleep(.005)
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(transfer, jobs))
        with self.first.connect() as db:
            self.assertEqual(db.execute(select(download_permits)).all(), [])

    def test_completion_deadlock_rolls_back_subtitle_enqueue_and_counters(self):
        job = self.first.create('download', {'base': str(self.root), 'config': {},
                               'urls': ['https://youtu.be/jNQXAC9IVRw']})
        result = {'message': 'done', '_subtitle_jobs': [{'base': str(self.root), 'config': {},
                   'video_file': str(self.root / 'video.mp4'), 'urls': ['https://youtu.be/jNQXAC9IVRw']}]}
        settle = self.first.statistics.settle
        calls = []
        def deadlock(db):
            settle(db)
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError('fixture', {}, Exception(1213, 'Deadlock'))
        with patch.object(self.first.statistics, 'settle', side_effect=deadlock):
            self.assertTrue(self.first.finish(job['id'], result=result))
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.first.finish(job['id'], result=result))
        self.assertEqual(len(self.first.get(job['id'])['subtitle_tasks']), 1)
        self.assertEqual(self.first.counts(actions=('subtitles',))['total'], 1)
        self.assertEqual(self.first.counts(actions=('download',))['succeeded'], 1)
