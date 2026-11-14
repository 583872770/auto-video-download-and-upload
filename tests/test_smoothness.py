"""Regression contracts for bounded local work and disposable read caches."""
from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, select

from webapp.disk_budget import file_lock
from webapp.local_processing import local_processing
from webapp.migrations import _completed_file_times
from webapp.progress_cache import ProgressCache
from webapp.read_cache import ReadCache


def contend(directory, active, maximum):
    progress = SimpleNamespace(state={}, save=lambda **kw: None)
    with local_processing(directory, {'postprocess_limit': 2}, progress, 'fixture'):
        with active.get_lock():
            active.value += 1
            maximum.value = max(maximum.value, active.value)
        time.sleep(.3)
        with active.get_lock():
            active.value -= 1


class SmoothnessTests(unittest.TestCase):
    def test_warning_counts_survive_log_redaction_and_keep_session_rejection(self):
        from webapp.downloads import DownloadFailure
        from webapp.diagnostics import safe_value
        failure = DownloadFailure()
        failure.warning('The account cookies are no longer valid')
        failure.warning('DRM protected formats have been skipped')
        self.assertTrue(failure.cookie_rejected)
        self.assertEqual(safe_value(dict(failure.warnings)), {'invalid_session': 1, 'drm_formats_skipped': 1})

    def test_upload_chunks_reuse_reservation_but_check_before_crossing_allowance(self):
        from webapp.disk_budget import DiskBudget, DiskPressure, MIB
        from webapp.disk_uploads import UploadStream
        with tempfile.TemporaryDirectory() as directory:
            budget = DiskBudget(directory, {'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
            stream = UploadStream(budget, 32*MIB)
            try:
                stream.lease.last_check = time.monotonic()
                with patch.object(budget, '_reserve', wraps=budget._reserve) as reserve:
                    for _ in range(100):
                        stream.write(b'x'*100)
                    self.assertEqual(reserve.call_count, 0)
                before = stream.length
                with patch.object(budget, '_reserve', side_effect=DiskPressure('fixture full')):
                    with self.assertRaises(DiskPressure):
                        stream.write(b'x'*(stream.lease.reserved+1))
                self.assertEqual(stream.length, before, 'pressure must be checked before writing the oversized block')
            finally:
                if stream.lease.error:
                    with self.assertRaises(DiskPressure):
                        stream.close()
                else:
                    stream.close()

    def test_local_processing_bounds_processes_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context('spawn')
            active, maximum = context.Value('i', 0), context.Value('i', 0)
            processes = [context.Process(target=contend, args=(directory, active, maximum)) for _ in range(4)]
            try:
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(25)
                    self.assertEqual(process.exitcode, 0)
                self.assertGreater(maximum.value, 0)
                self.assertLessEqual(maximum.value, 2)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
            progress = SimpleNamespace(state={}, save=lambda **kw: None)
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with local_processing(directory, {'postprocess_limit': 1}, progress, 'fixture'):
                    raise RuntimeError('fixture')
            with file_lock(Path(directory) / 'local-processing/0.lock') as held:
                self.assertTrue(held)

    def test_waiting_checks_cancellation_and_releases_slot_when_reporting_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            def cancelled(**kw):
                raise RuntimeError('lease revoked')
            progress = SimpleNamespace(state={}, save=cancelled)
            with file_lock(Path(directory) / 'local-processing/0.lock') as held:
                self.assertTrue(held)
                with self.assertRaisesRegex(RuntimeError, 'lease revoked'):
                    with local_processing(directory, {'postprocess_limit': 1}, progress, 'fixture'):
                        self.fail('must not enter while occupied')
                self.assertEqual(progress.state['stage'], 'waiting_processing')
            with self.assertRaisesRegex(RuntimeError, 'lease revoked'):
                with local_processing(directory, {'postprocess_limit': 1}, progress, 'fixture'):
                    self.fail('report failure must unwind the lock')
            with file_lock(Path(directory) / 'local-processing/0.lock') as held:
                self.assertTrue(held)

    def test_short_read_cache_copies_values_expires_and_does_not_cache_failure(self):
        cache, calls = ReadCache(ttl=3, maximum=2), []
        def load():
            calls.append(1)
            return {'items': [1]}
        with patch('webapp.read_cache.time.monotonic', return_value=100):
            cache.read('a', load)['items'].append(2)
            self.assertEqual(cache.read('a', load), {'items': [1]})
        with patch('webapp.read_cache.time.monotonic', return_value=104):
            cache.read('a', load)
        self.assertEqual(len(calls), 2)
        with self.assertRaises(ValueError):
            cache.read('b', lambda: (_ for _ in ()).throw(ValueError()))
        self.assertNotIn('b', cache.values)

    def test_batch_cache_validates_each_attempt_lease_and_sql_checkpoint(self):
        now = time.time()
        rows = [dict(id=str(i), status='running', worker_id='worker', started_at='start', attempts=1,
                     lease_until=now+60, progress=json.dumps({'_reported_at': now-3})) for i in range(5)]
        rows[3]['lease_until'] = now-1
        values = {}
        class Redis:
            calls = 0
            failed = False
            def mget(self, keys):
                self.calls += 1
                if self.failed:
                    raise OSError('fixture outage')
                return [values.get(key) for key in keys]
        redis = Redis()
        cache = ProgressCache('sqlite:///fixture', client=redis)
        for row in rows:
            values[cache.prefix+row['id']] = json.dumps(dict(attempt=cache.attempt(row), updated_at=now,
                progress={'_reported_at': now-2, 'downloaded_bytes': 20}))
        bad = json.loads(values[cache.prefix+'1'])
        bad['attempt'][2] = 2
        values[cache.prefix+'1'] = json.dumps(bad)
        rows[2]['progress'] = json.dumps({'_reported_at': now})
        values[cache.prefix+'4'] = 'broken'
        self.assertEqual(list(cache.read_many(rows)), ['0'])
        self.assertEqual(redis.calls, 1)
        health = cache.health()['counters']
        self.assertEqual([health.get(k) for k in ('accepted', 'reject_attempt', 'reject_older_than_sql', 'reject_lease', 'reject_malformed')], [1]*5)
        redis.failed = True
        self.assertEqual(cache.read_many(rows), {})
        self.assertEqual(cache.read_many(rows), {})
        self.assertEqual(redis.calls, 2)
        self.assertEqual(cache.health()['counters']['error_mget'], 1)
        self.assertEqual(cache.health()['counters']['backoff_mget'], 1)

    def test_completed_time_schema_is_idempotent_without_backfilling_test_data(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        from webapp.database import deliveries, test_files
        with engine.begin() as db:
            for table in (deliveries, test_files):
                db.exec_driver_sql(f'CREATE TABLE {table.name} (id TEXT PRIMARY KEY, seq INTEGER, session_id TEXT, created_at TEXT)')
                db.exec_driver_sql(f'INSERT INTO {table.name} VALUES (?, ?, ?, ?)', ('a', 1, 's', '2026-11-06T08:00:00+08:00'))
                db.exec_driver_sql(f'INSERT INTO {table.name} VALUES (?, ?, ?, ?)', ('b', 2, 's', 'invalid'))
        _completed_file_times(engine)
        _completed_file_times(engine)
        for table in (deliveries, test_files):
            with engine.connect() as db:
                result = dict(db.execute(select(table.c.id, table.c.created_epoch)).all())
            self.assertEqual(result, {'a': None, 'b': None})
            self.assertTrue(any(index['name'].endswith('_time') for index in inspect(engine).get_indexes(table.name)))

    def test_media_time_schema_does_not_invent_completion_times_for_existing_rows(self):
        from webapp.database import media
        from webapp.migrations import _media_download_times
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        with engine.begin() as db:
            db.exec_driver_sql(f'CREATE TABLE {media.name} (id TEXT PRIMARY KEY, modified DOUBLE)')
            db.exec_driver_sql(f"INSERT INTO {media.name} VALUES ('fixture', 1234.5)")
        _media_download_times(engine)
        _media_download_times(engine)
        with engine.connect() as db:
            self.assertEqual(db.exec_driver_sql(f'SELECT modified, downloaded_at FROM {media.name}').one(), (1234.5, 0))

    def test_current_runtime_log_filter_keeps_multiline_and_excludes_old_errors(self):
        from webapp.diagnostics import read_runtime_logs
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'logs/instance'
            root.mkdir(parents=True)
            (root/'supervisor.log').write_text('2026-11-05T00:00:00+00:00 old failure\nold traceback\n'
                '2026-11-06T00:00:00+00:00 new entry\nnew traceback\n', encoding='utf-8')
            since = datetime(2026, 11, 6, tzinfo=timezone.utc).timestamp()
            value = read_runtime_logs(directory, since=since)
            self.assertNotIn('old', value)
            self.assertIn('new traceback', value)
