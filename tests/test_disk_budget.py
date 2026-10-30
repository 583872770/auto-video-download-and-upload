"""Capacity contention, crash recovery, streaming limits and preserved ledgers."""
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from webapp.disk_budget import DiskBudget, DiskPressure, DiskCoordinationBusy, GIB, MIB, file_lock, process_identity, size_of
from webapp.maintenance import BoundedLog
from tests import test_scale


def reserve_import(directory, ready, release):
    try:
        budget = DiskBudget(directory, {'disk_import_budget_mb': 1, 'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        with budget.lease('imports', reserve=400 * 1024):
            ready.put(True)
            release.wait(15)
    except DiskPressure:
        ready.put(False)


def exercise_disk_worker(directory, ready, start, results):
    """Use independent SQLite connections/processes, just like real workers."""
    ready.put(True)
    if not start.wait(30):
        results.put(('error', 'workers did not start'))
        return
    try:
        budget = DiskBudget(directory, {'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        for attempt in range(8):
            with budget.lease('imports', reserve=MIB) as lease:
                lease.start_monitor()
                for part in range(8):
                    (lease.path / str(part)).write_bytes(b'x' * 8192)
                    lease.check((part + 1) * 8192, force=True)
                snapshot = budget.snapshot()
                if snapshot['committed_bytes'] > snapshot['budget_bytes']:
                    raise AssertionError('total capacity overcommitted')
                for category in snapshot['categories'].values():
                    if category['committed_bytes'] > category['budget_bytes']:
                        raise AssertionError('category capacity overcommitted')
        results.put(('ok', 8))
    except BaseException as error:
        results.put(('error', repr(error)))


class DiskScanTests(unittest.TestCase):
    def test_special_files_are_counted_without_opening_or_traversing(self):
        for kind in (stat.S_IFSOCK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(kind=kind):
                metadata = SimpleNamespace(st_mode=kind | 0o600, st_size=0, st_blocks=0)
                with patch.object(Path, 'stat', return_value=metadata), \
                        patch.object(Path, 'lstat', return_value=metadata), \
                        patch.object(Path, 'is_junction', return_value=False), \
                        patch('webapp.disk_budget.os.scandir') as scan:
                    self.assertEqual(size_of('SingletonSocket'), 0)
                scan.assert_not_called()

    def test_directory_replaced_during_scan_does_not_abort_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / 'changing'
            child.mkdir()
            (root / 'keep').write_bytes(b'x' * 123)
            original = os.scandir
            def scan(path):
                if Path(path) == child:
                    raise NotADirectoryError('directory changed during traversal')
                return original(path)
            with patch('webapp.disk_budget.os.scandir', side_effect=scan):
                self.assertGreaterEqual(size_of(root), 123)

    def test_scan_permission_errors_remain_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('webapp.disk_budget.os.scandir', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    size_of(temporary)

    def test_disappearing_parent_discards_credit_for_previously_scanned_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / 'lease'
            child.mkdir()
            (child / 'data').write_bytes(b'x' * 4096)
            original = os.scandir
            def entries():
                yield SimpleNamespace(path=str(child))
                raise NotADirectoryError('parent replaced during enumeration')
            @contextmanager
            def changing():
                yield entries()
            def scan(path):
                return changing() if Path(path) == root else original(path)
            measured = {child: 0}
            with patch('webapp.disk_budget.os.scandir', side_effect=scan):
                self.assertEqual(size_of(root, measured=measured), 0)
            self.assertEqual(measured[child], 0)


class DiskBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.budget = DiskBudget(self.root, {'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})

    def test_rejected_lock_contender_does_not_inspect_or_initialize_locked_file(self):
        path = self.root / 'first-use.lock'
        original = Path.stat
        def metadata(target, *args, **kwargs):
            if target == path:
                raise AssertionError('contender must acquire ownership before initialization')
            return original(target, *args, **kwargs)
        with file_lock(path) as owned:
            self.assertTrue(owned)
            with patch.object(Path, 'stat', new=metadata):
                with file_lock(path) as competing:
                    self.assertFalse(competing)

    def test_sixteen_processes_write_check_and_refresh_without_coordination_failures(self):
        context = multiprocessing.get_context('spawn')
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=exercise_disk_worker,
                     args=(str(self.root), ready, start, results)) for _ in range(16)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                ready.get(timeout=45)
            start.set()
            # Exercise independent maintenance scans while workers admit/grow/exit.
            for _ in range(20):
                snapshot = self.budget.snapshot(force=True)
                self.assertLessEqual(snapshot['committed_bytes'], snapshot['budget_bytes'])
                time.sleep(0.02)
            outcomes = [results.get(timeout=60) for _ in processes]
            self.assertEqual(outcomes, [('ok', 8)] * 16)
        finally:
            start.set()
            for process in processes:
                if process.pid:
                    process.join(timeout=5)
                    if process.is_alive():
                        process.kill()
                        process.join()
            ready.close()
            results.close()
        snapshot = self.budget.snapshot(force=True)
        self.assertEqual(snapshot['active'], {})
        self.assertLess(snapshot['categories']['imports']['used_bytes'], 65536)

    def test_budget_writer_commits_while_a_snapshot_reader_is_open(self):
        done, failures = threading.Event(), []
        def write():
            try:
                with self.budget.connect() as db:
                    self.budget._set(db, 'writer-progress', True)
            except BaseException as error:
                failures.append(error)
            finally:
                done.set()
        with self.budget.connect(write=False) as db:
            db.execute('SELECT * FROM state').fetchall()
            thread = threading.Thread(target=write)
            thread.start()
            committed = done.wait(3)
        thread.join(6)
        self.assertTrue(committed, 'read-only checks must not block budget commits')
        self.assertEqual(failures, [])

    def test_unchanged_checks_and_children_do_not_write_database_pages(self):
        changes = []
        original = self.budget.connect
        @contextmanager
        def counted(**kwargs):
            with original(**kwargs) as db:
                before = db.total_changes
                yield db
                changes.append(db.total_changes - before)
        with self.budget.lease('imports') as lease:
            lease.check(force=True)
            self.budget.snapshot(force=True)
            with patch.object(self.budget, 'connect', new=counted), \
                    patch('webapp.disk_budget.psutil.Process', return_value=SimpleNamespace(children=lambda **kw: [])):
                for _ in range(5):
                    lease.track_children()
                    lease.check(force=True)
                    self.budget.snapshot()
        self.assertTrue(changes)
        self.assertEqual(sum(changes), 0)

    def test_slow_scan_does_not_hold_writer_lock_or_lose_new_reservations(self):
        scanned, resume = threading.Event(), threading.Event()
        original = size_of
        def slow(path, **kwargs):
            amount = original(path, **kwargs)
            if Path(path) == self.root / 'test-downloads':
                scanned.set()
                if not resume.wait(10):
                    raise AssertionError('scan not released')
            return amount
        with self.budget.lease('test', reserve=2 * MIB) as first:
            (first.path / 'data').write_bytes(b'x' * 1000)
            with ThreadPoolExecutor(max_workers=1) as pool:
                with patch('webapp.disk_budget.size_of', side_effect=slow):
                    pending = pool.submit(self.budget._refresh_usage, True)
                    try:
                        self.assertTrue(scanned.wait(5))
                        # Both a new constructor and a reservation must succeed
                        # before the filesystem scan is allowed to finish.
                        other = DiskBudget(self.root, self.budget.config)
                        with other.lease('test', reserve=3 * MIB) as second:
                            resume.set()
                            pending.result(timeout=5)
                            snapshot = self.budget.snapshot()
                            self.assertGreaterEqual(snapshot['categories']['test']['committed_bytes'], 5 * MIB)
                            with self.budget.connect(write=False) as db:
                                row = db.execute('SELECT reserved,measured FROM leases WHERE id=?', (second.id,)).fetchone()
                                self.assertEqual(tuple(row), (3 * MIB, 0))
                    finally:
                        resume.set()
                    pending.result(timeout=5)

    def test_measurement_credits_only_bytes_in_the_same_traversal(self):
        with self.budget.lease('imports', reserve=MIB) as lease:
            (lease.path / 'data').write_bytes(b'x' * 4096)
            original = size_of
            def growing(path, **kwargs):
                amount = original(path, **kwargs)
                if Path(path) == lease.path:
                    (lease.path / 'late-data').write_bytes(b'x' * (MIB // 2))
                return amount
            with patch('webapp.disk_budget.size_of', side_effect=growing):
                snapshot = self.budget.snapshot(force=True)
            self.assertGreaterEqual(snapshot['categories']['imports']['committed_bytes'], MIB)
            with self.budget.connect(write=False) as db:
                measured = db.execute('SELECT measured FROM leases WHERE id=?', (lease.id,)).fetchone()[0]
            self.assertLess(measured, MIB // 2)

    def test_busy_database_is_resource_pressure_and_rejected_admission_releases_file_lock(self):
        self.budget.snapshot()
        lease = self.budget.lease('imports')
        with self.budget.connect(), patch('webapp.disk_budget.COORDINATION_TIMEOUT', 0.03):
            with self.assertRaises(DiskCoordinationBusy):
                lease.__enter__()
        with file_lock(self.root / 'disk-locks' / (lease.id + '.lock')) as acquired:
            self.assertTrue(acquired)
        with self.budget.connect(write=False) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 0)

    def test_existing_reservation_can_be_checked_while_another_writer_is_active(self):
        with self.budget.lease('imports') as lease:
            lease.check(force=True)
            self.budget.snapshot(force=True)
            with self.budget.connect(), patch('webapp.disk_budget.COORDINATION_TIMEOUT', 0.03):
                lease.check(force=True)
                self.assertEqual(self.budget.snapshot()['active']['imports'], 1)

    def test_watchdog_reports_lock_pressure_and_cleanup_preserves_original_error(self):
        notified = threading.Event()
        lease = self.budget.lease('imports')
        lease.__enter__()
        lease.estimate = lease.reserved + MIB
        with self.budget.connect(), patch('webapp.disk_budget.COORDINATION_TIMEOUT', 0.03):
            lease.start_monitor(notified.set)
            self.assertTrue(notified.wait(5), 'watchdog must report rather than die silently')
            self.assertIsInstance(lease.error, DiskCoordinationBusy)
            with self.assertRaises(DiskCoordinationBusy):
                lease.__exit__(None, None, None)
        with file_lock(self.root / 'disk-locks' / (lease.id + '.lock')) as acquired:
            self.assertTrue(acquired, 'cleanup must always release ownership lock')

    def test_unexpected_monitor_exception_is_logged_and_reported_to_execution(self):
        notified = threading.Event()
        lease = self.budget.lease('imports')
        lease.__enter__()
        with patch.object(lease, 'track_children', side_effect=sqlite3.OperationalError('unexpected fixture')), \
                self.assertLogs('webapp.disk_budget', level='ERROR') as logs:
            lease.start_monitor(notified.set)
            self.assertTrue(notified.wait(5))
        self.assertIn('unexpected fixture', '\n'.join(logs.output))
        with self.assertRaises(DiskPressure):
            lease.check()
        with self.assertRaises(DiskPressure):
            lease.__exit__(None, None, None)

    def test_failed_child_tracking_during_exit_keeps_files_and_releases_lock(self):
        lease = self.budget.lease('imports')
        notified = threading.Event()
        with self.assertRaisesRegex(RuntimeError, 'original error'):
            with patch.object(lease, 'track_children', side_effect=DiskCoordinationBusy('busy')):
                with lease:
                    lease.on_pressure = notified.set
                    # Newly discovered descendants may not have reached SQLite
                    # when tracking fails; the retirement handoff must retain them.
                    lease.children = [{'pid': os.getpid(), 'created': 0}]
                    raise RuntimeError('original error')
        self.assertTrue(notified.is_set())
        self.assertTrue(lease.preserve)
        self.assertTrue(lease.path.exists())
        with self.budget.connect(write=False) as db:
            row = db.execute('SELECT children FROM leases WHERE id=?', (lease.id,)).fetchone()
            self.assertEqual(json.loads(row[0]), lease.children)
        with file_lock(self.root / 'disk-locks' / (lease.id + '.lock')) as acquired:
            self.assertTrue(acquired)

    def test_metadata_cleanup_lock_does_not_replace_a_completed_result_with_retry(self):
        lease = self.budget.lease('imports')
        lease.__enter__()
        with self.budget.connect(), patch('webapp.disk_budget.COORDINATION_TIMEOUT', 0.03):
            lease.__exit__(None, None, None)
        self.assertFalse(lease.path.exists())
        with file_lock(self.root / 'disk-locks' / (lease.id + '.lock')) as acquired:
            self.assertTrue(acquired)

    def test_real_processes_cannot_reserve_the_same_capacity(self):
        context = multiprocessing.get_context('spawn')
        ready, release = context.Queue(), context.Event()
        processes = [context.Process(target=reserve_import, args=(str(self.root), ready, release)) for _ in range(3)]
        try:
            for process in processes:
                process.start()
            results = [ready.get(timeout=20) for _ in processes]
            self.assertEqual(sorted(results), [False, True, True])
        finally:
            release.set()
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.kill()
                    process.join()

    def test_allocation_and_reservation_are_not_counted_twice(self):
        with self.budget.lease('imports', reserve=MIB) as lease:
            (lease.path / 'data').write_bytes(b'x' * (MIB // 2))
            snapshot = self.budget.snapshot(force=True)
            value = snapshot['categories']['imports']
            self.assertGreaterEqual(value['used_bytes'], MIB // 2)
            self.assertGreaterEqual(value['committed_bytes'], MIB)
            self.assertLess(value['committed_bytes'], MIB + 65536)

    def test_crashed_owner_is_swept_but_live_owner_is_not(self):
        script = "from webapp.disk_budget import DiskBudget; import os,sys; b=DiskBudget(sys.argv[1],{'disk_min_free_gb':0,'disk_resume_free_gb':0}); l=b.lease('imports'); l.__enter__(); (l.path/'partial').write_bytes(b'x'*100); os._exit(0)"
        subprocess.run([sys.executable, '-c', script, str(self.root)], check=True, timeout=20)
        with self.budget.lease('imports') as active:
            self.budget.cleanup(grace=0)
            self.assertTrue(active.path.is_dir())
            with self.budget.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0], 1)
        self.assertEqual(list((self.root / 'runtime/uploads').glob('*')), [])

    def test_low_watermark_has_resume_hysteresis(self):
        budget = DiskBudget(self.root, {'disk_min_free_gb': 10, 'disk_resume_free_gb': 15})
        with patch.object(budget, '_free', return_value=9 * GIB):
            self.assertTrue(budget.snapshot()['low_space'])
        with patch.object(budget, '_free', return_value=12 * GIB):
            self.assertTrue(budget.snapshot()['low_space'])
        with patch.object(budget, '_free', return_value=16 * GIB):
            self.assertFalse(budget.snapshot()['low_space'])

    def test_reservations_also_protect_real_remaining_space(self):
        budget = DiskBudget(self.root, {'disk_min_free_gb':10,'disk_resume_free_gb':15})
        with patch.object(budget, '_free', return_value=12 * GIB):
            with budget.lease('test', reserve=2 * GIB):
                with self.assertRaises(DiskPressure):
                    with budget.lease('test', reserve=GIB):
                        self.fail('cannot promise the same physical free bytes twice')

    def test_oversized_task_remembers_required_capacity(self):
        with self.assertRaises(DiskPressure):
            with self.budget.lease('test', task='a' * 32) as lease:
                lease.check(17 * GIB, force=True)
        with self.assertRaises(DiskPressure):
            with self.budget.lease('test', task='a' * 32):
                self.fail('must not start the same oversized transfer again')

    def test_symlink_is_not_a_cleanup_target(self):
        outside = self.root / 'protected'
        outside.mkdir()
        (outside / 'keep').write_text('keep')
        link = self.root / 'runtime/uploads/link'
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink permission unavailable')
        with self.assertRaises(ValueError):
            self.budget.lease('imports', path=link)
        self.assertTrue((outside / 'keep').exists())

    def test_unconfirmed_completed_file_is_preserved(self):
        lease = self.budget.lease('test', task='a' * 32)
        with lease:
            (lease.path / '.completed-files.json').write_text('[{"path":"unknown","size":1}]')
            lease.preserve = True
        result = self.budget.cleanup(grace=0)
        self.assertTrue(lease.path.exists())
        self.assertEqual(result['last_cleanup']['pending_results'], 1)

    def test_log_writes_stay_bounded_before_task_finishes(self):
        path = self.root / ('a' * 32 + '.log')
        writer = BoundedLog(path, 65536)
        for i in range(100):
            writer.write((str(i) + ':' + 'x' * 10000 + '\n').encode())
            self.assertLessEqual(path.stat().st_size, 65536)
        self.assertIn(b'99:', path.read_bytes())
        self.assertGreater(int(path.with_suffix('.offset').read_text()), 0)


class DiskWebTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    @unittest.skipIf(os.name == 'nt', 'requires POSIX filesystem sockets and FIFOs')
    def test_linux_chromium_socket_and_fifo_do_not_break_disk_api_or_admission(self):
        root = self.store.directory / 'runtime/cache/worker-2/chromium'
        root.mkdir(parents=True)
        (root / 'data').write_bytes(b'fixture')
        os.mkfifo(root / 'pipe')
        with socket.socket(socket.AF_UNIX) as connection:
            connection.bind(str(root / 'SingletonSocket'))
            self.assertTrue(stat.S_ISSOCK((root / 'SingletonSocket').lstat().st_mode))
            response = self.client.get('/api/disk')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertGreaterEqual(response.json['categories']['cache']['used_bytes'], 7)
            budget = DiskBudget(self.store.directory, {'disk_min_free_gb':0, 'disk_resume_free_gb':0})
            with budget.lease('test') as lease:
                lease.check(force=True)
            budget.cleanup(self.store, grace=300)

    def test_configuration_is_validated_and_visible(self):
        before = self.config.read_bytes()
        bad = self.client.put('/api/config', json={'disk_min_free_gb':20,'disk_resume_free_gb':10}, headers=self.headers)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.config.read_bytes(), before)
        response = self.client.put('/api/config', json={'disk_budget_gb':25}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.runtime()['disk_budget_gb'], 25)
        self.assertEqual(self.client.get('/api/disk').json['budget_bytes'], 25 * GIB)
        self.assertIn('磁盘容量管理', self.client.get('/').text)

    def test_upload_is_bounded_during_multipart_parsing(self):
        response = self.client.put('/api/config', json={'disk_upload_limit_mb':1}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        response = self.client.post('/api/downloads/import', data={'file':(BytesIO(b'x' * (MIB + 1)), 'large.txt')}, headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(any((self.store.directory / 'runtime/uploads').glob('*')))

    def test_log_cleanup_keeps_task_and_statistics(self):
        task = self.store.create('check', {'config':{}, 'base':str(self.root)})
        self.store.finish(task['id'], result={'message':'ok'})
        path = self.store.log_path(task['id'])
        path.write_bytes(b'old log')
        os.utime(path, (time.time() - 48 * 3600,) * 2)
        before = self.client.get('/api/statistics').json
        result = self.client.post('/api/disk/cleanup', headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.get(task['id'])['status'], 'succeeded')
        self.assertEqual(self.client.get('/api/statistics').json, before)

    def test_resource_wait_does_not_increment_failure_or_consume_retry(self):
        task = self.store.create('download', {'config':{}, 'base':str(self.root),'urls':['https://youtu.be/npNnbj46_ik']})
        task = self.store.claim(self.store.owner)
        self.store.release(task['id'], self.store.owner, '磁盘预算不足', resource=True)
        task = self.store.get(task['id'])
        self.assertEqual(task['attempts'], 0)
        self.assertEqual(task['progress']['stage'], 'disk_wait')
        totals = self.client.get('/api/statistics').json['summary']
        self.assertEqual(totals['failed'], 0)

    def test_worker_handles_pressure_without_provider_failure(self):
        from web_worker import drain
        task = self.store.create('download', {'config':{}, 'base':str(self.root),'urls':['https://youtu.be/npNnbj46_ik']})
        with patch('web_worker.execute', side_effect=DiskPressure('测试磁盘预算不足')):
            drain(self.store.directory, role='download')
        saved = self.store.get(task['id'])
        self.assertEqual(saved['status'], 'queued')
        self.assertEqual(saved['attempts'], 0)
        self.assertEqual(saved['progress']['stage'], 'disk_wait')
        self.assertEqual(self.client.get('/api/statistics').json['summary']['failed'], 0)
        self.assertEqual(self.store.workers()[0]['state'], 'disk_wait')

    def test_real_coordination_lock_is_requeued_without_failure_statistics(self):
        from web_worker import drain
        self.assertEqual(self.client.put('/api/config', json={'storage_mode': 'test', 'disk_min_free_gb': 0,
            'disk_resume_free_gb': 0}, headers=self.headers).status_code, 200)
        budget = DiskBudget(self.store.directory, self.store.runtime())
        budget.snapshot()
        task = self.store.create('download', {'config': {'storage_mode': 'test'}, 'base': str(self.root),
                                            'urls': ['https://youtu.be/npNnbj46_ik']})
        with closing(sqlite3.connect(budget.database)) as locker:
            locker.execute('BEGIN IMMEDIATE')
            with patch('webapp.disk_budget.COORDINATION_TIMEOUT', 0.03), \
                    patch('web_worker._execute', side_effect=AssertionError('must wait before downloading')):
                drain(self.store.directory, role='download')
        saved = self.store.get(task['id'])
        self.assertEqual(saved['status'], 'queued')
        self.assertEqual(saved['attempts'], 0)
        self.assertEqual(saved['progress']['stage'], 'disk_wait')
        self.assertIn('协调繁忙', saved['error'])
        self.assertEqual(self.client.get('/api/statistics').json['summary']['failed'], 0)

    def test_completed_file_is_recovered_before_any_new_download(self):
        self.check_completed_file_recovery()

    def test_automatic_completed_receipt_recovery_uses_actual_adapter(self):
        response = self.client.put('/api/config', json={'scheduler_mode': 'auto',
            'download_methods': ['yt_dlp', 'downmp3']}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.check_completed_file_recovery()

    def check_completed_file_recovery(self):
        from web_worker import execute
        self.assertEqual(self.client.put('/api/config', json={'storage_mode':'test','disk_min_free_gb':0,
            'disk_resume_free_gb':0}, headers=self.headers).status_code, 200)
        task = self.store.create('download', {'config':{'storage_mode':'test'},'base':str(self.root),
                                            'urls':['https://youtu.be/npNnbj46_ik']})
        self.store.claim(self.store.owner)
        budget = DiskBudget(self.store.directory, {'disk_min_free_gb':0,'disk_resume_free_gb':0})
        with budget.lease('test', task=task['id']) as lease:
            video = lease.path / 'complete.mp4'
            video.write_bytes(b'completed fixture')
            (lease.path / '.completed-files.json').write_text(json.dumps([{'path':str(video.resolve()),
                'size':video.stat().st_size,'complete':True,'method':'downmp3','video_id':'npNnbj46_ik','title':'Fixture'}]))
            lease.preserve = True
        self.store.release(task['id'], self.store.owner, 'simulated restart', delay=0)
        self.store.claim(self.store.owner)
        with patch.object(self.store.statistics, 'record_file', return_value=False):
            with self.assertRaisesRegex(DiskPressure, '尚未核对一致'):
                budget.recover_test_result(self.store, task['id'])
        self.assertTrue(video.exists(), 'an unconfirmed receipt must not be deleted')
        with patch('web_worker._execute', side_effect=AssertionError('must not redownload')):
            result = execute('download', self.store.get(task['id'], private=True)['payload'], store=self.store, task_id=task['id'])
        self.assertTrue(result['recovered'])
        self.assertFalse(video.exists())
        self.store.finish(task['id'], result=result, worker_id=self.store.owner)
        before = self.client.get('/api/statistics').json['summary']
        self.assertEqual(before['test_file_count'], 1)
        self.assertEqual(before['test_file_bytes'], len(b'completed fixture'))
        methods = {row['method']: row for row in self.client.get('/api/statistics').json['methods']}
        self.assertEqual(methods['downmp3']['test_file_bytes'], len(b'completed fixture'))
        self.assertEqual(methods['yt_dlp']['test_file_bytes'], 0)
        budget.cleanup(self.store, grace=0)
        self.assertEqual(self.client.get('/api/statistics').json['summary'], before)

    def test_late_watchdog_pressure_does_not_requeue_completed_pipeline(self):
        from web_worker import execute
        running = []
        def monitor(lease, on_pressure):
            running.append(lease)
        def download(*args):
            running[0].error = DiskPressure('late observation during completion')
            return {'file_count':1, 'file_bytes':20}
        with patch('webapp.disk_budget.DiskLease.start_monitor', new=monitor):
            with patch('web_worker._execute', side_effect=download):
                result = execute('download', {'config':{'storage_mode':'test','disk_min_free_gb':0,
                    'disk_resume_free_gb':0}, 'base':str(self.root)})
        self.assertEqual(result['file_count'], 1)
        self.assertTrue(result['files_deleted'])

    def test_pressure_wrapped_by_downloader_remains_resource_wait(self):
        from web_worker import execute
        self.assertEqual(self.client.put('/api/config', json={'storage_mode':'test','disk_min_free_gb':0,
            'disk_resume_free_gb':0}, headers=self.headers).status_code, 200)
        task = self.store.create('download', {'config':{'storage_mode':'test'}, 'base':str(self.root),
                                            'urls':['https://youtu.be/npNnbj46_ik']})
        task = self.store.claim(self.store.owner)
        def download(action, payload, report, *args):
            try:
                report({'total_bytes':20 * GIB,'download_method':'yt_dlp'})
            except DiskPressure as error:
                raise RuntimeError('wrapped downloader error') from error
        with patch('web_worker._execute', side_effect=download):
            with self.assertRaises(DiskPressure):
                execute('download', task['payload'], store=self.store, task_id=task['id'])
