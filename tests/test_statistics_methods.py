"""Selected-mode accounting remains exact across retries, fallback and upgrades."""
from concurrent.futures import ThreadPoolExecutor
import csv
from io import StringIO
import unittest

from sqlalchemy import event

from tests.test_mysql import MySQLFixture
from tests.test_scale import ScaleTests
from tests.test_statistics import StatisticsContract
from webapp.database import download_sessions, method_tasks, method_totals, settings, tasks
from webapp.downloads import DownloadLeaseLost
from webapp.store import TaskStore


class MethodStatisticsContract:
    job = StatisticsContract.job
    claim = StatisticsContract.claim
    final_file = StatisticsContract.final_file
    overview = StatisticsContract.overview

    def methods(self, identity=None, query=''):
        url = '/api/statistics/sessions/' + identity if identity else '/api/statistics'
        response = self.client.get(url, query_string={'q': query})
        self.assertEqual(response.status_code, 200, response.json)
        return {row['method']: row for row in response.json['methods']}

    def test_start_configuration_groups_three_modes_and_test_sizes_without_double_counting(self):
        for index, method in enumerate(('yt_dlp', 'yt5s', 'yt5s_fallback')):
            self.job(f'{index:011d}', download_method=method, storage_mode='test' if index == 2 else 'local')
        from webapp.settings import DEFAULTS
        jobs = []
        for method in ('yt_dlp', 'yt5s', 'yt5s_fallback'):
            self.store.set_download_settings({**DEFAULTS, 'download_method': method},
                                             self.root, self.store.directory / 'clash')
            jobs.append(self.claim())
        identity = jobs[0]['payload']['download_session_id']
        for index, job in enumerate(jobs):
            self.assertEqual(job['payload']['download_session_id'], identity)
            path = self.final_file(f'{index}.mp4', 101 * (index + 1))
            if index == 2:
                self.store.statistics.methods.fallback(job['id'])
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: self.store.statistics.record_file(
                    job['id'], path, test_mode=index == 2), range(2)))
            self.assertEqual(sorted(results), [False, True])
            if index == 2:
                path.unlink()
            self.store.finish(job['id'], result={'test_mode': index == 2,
                'download_method': 'yt_dlp' if index == 2 else 'yt5s', 'fallback_used': index == 2}, worker_id=self.store.owner)
        rows = self.methods(identity)
        self.assertEqual(set(rows), {'yt_dlp', 'yt5s', 'downmp3'})
        self.assertEqual((rows['yt_dlp']['attempted'], rows['yt_dlp']['succeeded'], rows['yt_dlp']['file_bytes']), (2, 1, 101))
        self.assertEqual((rows['yt_dlp']['file_bytes'], rows['yt_dlp']['test_file_bytes'], rows['yt_dlp']['test_succeeded'], rows['yt_dlp']['fallback_tasks']), (101, 303, 1, 1))
        self.assertEqual(rows['yt_dlp']['downloaded_file_bytes_exact'], '404')
        self.assertEqual(rows['yt_dlp']['success_rate'], 100)
        self.assertEqual((rows['yt5s']['attempted'], rows['yt5s']['succeeded'], rows['yt5s']['failed'], rows['yt5s']['file_bytes']), (2, 1, 1, 202))
        self.assertEqual(self.methods(), rows)
        self.assertEqual(TaskStore(self.store.directory).statistics.method_summary(identity), list(rows.values()))
        self.assertEqual(self.overview()['summary']['file_bytes'], 303)

    def test_fallback_retries_release_and_failure_count_each_task_once(self):
        self.job(download_method='yt5s_fallback')
        job = self.claim()
        identity = job['payload']['download_session_id']
        self.store.statistics.methods.fallback(job['id'])
        self.store.finish(job['id'], error='both providers failed', retryable=True, worker_id=self.store.owner)
        row = self.methods(identity)['yt_dlp']
        self.assertEqual((row['attempted'], row['running'], row['failed'], row['unfinished'], row['fallback_tasks']), (1, 0, 0, 1, 1))
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == job['id']).values(available_at=0))
        again = self.claim()
        self.assertEqual(again['payload']['download_session_id'], identity)
        self.store.statistics.methods.fallback(job['id'])
        self.store.release(job['id'], self.store.owner, 'cookie expired', delay=0)
        row = self.methods(identity)['yt_dlp']
        self.assertEqual((row['attempted'], row['running'], row['unfinished'], row['fallback_tasks']), (1, 0, 1, 1))
        self.assertIsNotNone(self.claim())
        self.store.finish(job['id'], error='permanent failure', worker_id=self.store.owner)
        self.assertFalse(self.store.finish(job['id'], error='duplicate', worker_id=self.store.owner))
        with self.assertRaises(DownloadLeaseLost):
            self.store.statistics.methods.fallback(job['id'])
        row = self.methods(identity)['yt_dlp']
        self.assertEqual((row['attempted'], row['failed'], row['running'], row['unfinished'], row['fallback_tasks']), (1, 1, 0, 0, 1))
        self.assertEqual(row['success_rate'], 0)

    def test_pause_resume_attributes_completed_files_to_new_session(self):
        self.job(download_method='yt5s')
        first = self.claim()
        old_id = first['payload']['download_session_id']
        path = self.final_file(size=1234)
        self.store.statistics.record_file(first['id'], path)
        self.store.set_downloads_paused(True)
        self.store.stop_running_downloads(lambda owner: True)
        old = self.methods(old_id)
        self.assertEqual((old['yt5s']['unfinished'], old['yt5s']['running'], old['yt5s']['file_bytes']), (1, 0, 1234))
        self.store.set_downloads_paused(False)
        resumed = self.claim()
        new_id = resumed['payload']['download_session_id']
        self.assertNotEqual(new_id, old_id)
        self.assertFalse(self.store.statistics.record_file(resumed['id'], path))
        self.store.statistics.record_file(resumed['id'], self.final_file('next.mp4', 567))
        self.store.finish(resumed['id'], result={}, worker_id=self.store.owner)
        self.assertEqual(self.methods(old_id), old)
        new = self.methods(new_id)['yt5s']
        self.assertEqual((new['attempted'], new['succeeded'], new['file_bytes']), (1, 1, 567))
        total = self.methods()['yt5s']
        self.assertEqual((total['attempted'], total['succeeded'], total['unfinished'], total['file_bytes']), (2, 1, 1, 1801))

    def test_recovery_and_parallel_completion_preserve_counters(self):
        for index in range(4):
            self.job(f'{index:011d}', download_method='yt5s_fallback')
        jobs = [self.claim() for _ in range(4)]
        identity = jobs[0]['payload']['download_session_id']
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == jobs[0]['id']).values(lease_until=0))
        self.store.recover(expired_only=True)
        row = self.methods(identity)['yt5s']
        self.assertEqual((row['attempted'], row['running'], row['failed']), (4, 3, 0))
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == jobs[0]['id']).values(available_at=0))
        resumed = self.claim()
        self.assertEqual(resumed['id'], jobs[0]['id'])

        def finish(job):
            self.store.statistics.methods.fallback(job['id'])
            self.store.finish(job['id'], result={'fallback_used': True}, worker_id=self.store.owner)

        with ThreadPoolExecutor(4) as pool:
            list(pool.map(finish, jobs))
        row = self.methods(identity)['yt_dlp']
        self.assertEqual((row['attempted'], row['running'], row['succeeded'], row['fallback_tasks']), (4, 0, 4, 4))

    def test_customer_filter_and_csv_exports_keep_method_numbers_exact(self):
        for index, (method, label) in enumerate([('yt_dlp', '客户 A'), ('yt5s', '客户 B'), ('yt5s_fallback', '客户 A')]):
            self.job(f'{index:011d}', download_method=method)
            job = self.claim()
            identity = job['payload']['download_session_id']
            self.store.statistics.update(identity, {'label': label, 'price': '2.5', 'unit': 'GB'})
            self.store.statistics.record_file(job['id'], self.final_file(f'{index}.mp4', index + 100))
            self.store.finish(job['id'], result={}, worker_id=self.store.owner)
        rows = self.methods(query='客户 A')
        self.assertEqual([rows[key]['file_bytes'] for key in ('yt_dlp', 'yt5s')], [100, 102])
        self.assertEqual(sum(row['attempted'] for row in self.methods(query='%').values()), 0)
        exported = self.client.get('/api/statistics/methods/export', query_string={'q': '客户 A'})
        self.assertEqual(exported.status_code, 200)
        self.assertTrue(exported.data.startswith(b'\xef\xbb\xbf'))
        csv_rows = list(csv.DictReader(StringIO(exported.data.decode('utf-8-sig'))))
        self.assertEqual([row['交付精确字节数(计费)'] for row in csv_rows], ['100', '102', '0'])
        size = 9007199254740993
        with self.store.connect() as db:
            db.execute(download_sessions.update().where(download_sessions.c.id == identity).values(file_bytes=size))
            db.execute(method_totals.update().where(method_totals.c.session_id == identity).values(file_bytes=size))
        response = self.client.get('/api/statistics/sessions/' + identity + '/export?kind=methods')
        self.assertIn(str(size), response.data.decode('utf-8-sig'))
        self.assertEqual(self.methods(identity)['yt5s']['file_bytes_exact'], str(size))

    def test_old_schema_creates_new_tables_without_reclassifying_historical_bytes(self):
        self.job()
        job = self.claim()
        identity = job['payload']['download_session_id']
        self.store.statistics.record_file(job['id'], self.final_file(size=999))
        self.store.statistics.record_file(job['id'], self.final_file('test.mp4', 777), test_mode=True)
        self.store.finish(job['id'], result={}, worker_id=self.store.owner)
        before = self.overview()['summary']
        method_tasks.drop(self.store.db.engine)
        method_totals.drop(self.store.db.engine)
        with self.store.connect() as db:
            db.execute(settings.delete().where(settings.c.key == 'download_method_statistics_since'))
        self.store.db.initialize()
        self.store.db.initialize()
        rows = self.methods(identity)
        self.assertEqual((rows['unknown']['file_bytes'], rows['unknown']['test_file_bytes'], rows['unknown']['succeeded']), (999, 777, 1))
        self.assertEqual(sum(rows[method]['attempted'] for method in ('yt_dlp', 'yt5s')), 0)
        self.assertEqual(self.overview()['summary'], before)
        self.assertTrue(self.overview()['method_tracking_since'])

    def test_overview_and_method_export_read_only_small_aggregates(self):
        self.job(download_method='yt5s')
        self.claim()
        statements = []

        def sql(db, cursor, statement, parameters, context, many):
            statements.append(statement.lower())

        event.listen(self.store.db.engine, 'before_cursor_execute', sql)
        try:
            self.methods()
            self.client.get('/api/statistics/methods/export').get_data()
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', sql)
        for statement in statements:
            self.assertFalse(any(table in statement for table in ('from tasks', 'from download_deliveries',
                'from download_test_files', 'from download_method_tasks', 'from download_session_tasks')), statement)


class SQLiteMethodStatisticsTests(MethodStatisticsContract, unittest.TestCase):
    setUp = ScaleTests.setUp


class MySQLMethodStatisticsTests(MySQLFixture, MethodStatisticsContract, unittest.TestCase):
    pass

