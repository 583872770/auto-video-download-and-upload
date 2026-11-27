"""Capacity contention, crash recovery, streaming limits and preserved ledgers."""
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
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
import time
import traceback
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from webapp.disk_budget import DiskBudget, DiskPressure, DiskCoordinationBusy, GIB, MIB, file_lock, process_identity, safe_path, size_of
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
    except BaseException:
        results.put(('error', traceback.format_exc()))


class DiskScanTests(unittest.TestCase):
    def test_empty_test_parent_cleanup_preserves_files_recent_directories_and_owned_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            budget = DiskBudget(temporary, {'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
            root = budget.directory / 'test-downloads'
            root.mkdir(exist_ok=True)
            old, recent, nonempty, unknown, owned = [root / (c * 32) for c in 'abcde']
            for path in (old, recent, nonempty, unknown, owned):
                path.mkdir()
            (nonempty / 'keep').write_bytes(b'not a disposable orphan')
            unknown.rename(root / 'unknown-directory')
            for path in (old, nonempty, owned, root / 'unknown-directory'):
                os.utime(path, (time.time()-600, time.time()-600))
            lease = budget.lease('test', path=owned / 'new-attempt')
            with lease:
                # The owner may reserve a path before its first file is written.
                lease.path.rmdir()
                os.utime(owned, (time.time()-600, time.time()-600))
                count, _ = budget._clean_empty_test_directories(300)
                self.assertEqual(count, 1)
                self.assertFalse(old.exists())
                self.assertTrue(owned.exists())
                self.assertTrue(recent.exists())
                self.assertTrue((nonempty / 'keep').is_file())
                self.assertTrue((root / 'unknown-directory').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows extended-path spelling')
    def test_disappearing_directory_extended_prefix_is_not_a_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'changing' / 'child'
            original = Path.resolve
            def prefixed(path, *args, **kwargs):
                resolved = original(path, *args, **kwargs)
                return Path('\\\\?\\' + str(resolved)) if path == target else resolved
            with patch.object(Path, 'resolve', new=prefixed):
                self.assertEqual(safe_path(target, root), target)

    def test_resolved_redirect_and_parent_escape_remain_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'changing' / 'child'
            original = Path.resolve
            def redirected(path, *args, **kwargs):
                return root.parent / 'outside' if path == target else original(path, *args, **kwargs)
            with patch.object(Path, 'resolve', new=redirected):
                with self.assertRaisesRegex(ValueError, '包含链接'):
                    safe_path(target, root)
            with self.assertRaises(ValueError):
                safe_path(root / '..' / 'outside', root)

    @unittest.skipUnless(os.name == 'nt', 'NTFS concurrent deletion')
    def test_delete_pending_native_name_is_rechecked_before_accepting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / 'changing' / 'child'
            deleted = Path(root.drive + '/') / '$Extend' / '$Deleted' / 'transient' / 'child'
            original = Path.resolve
            observations = []
            def changing(path, *args, **kwargs):
                if path == target:
                    observations.append(path)
                    if len(observations) == 1:
                        return deleted
                return original(path, *args, **kwargs)
            with patch.object(Path, 'resolve', new=changing):
                self.assertEqual(safe_path(target, root), target)
            self.assertEqual(len(observations), 2)

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

    @unittest.skipUnless(os.name == 'nt', 'Windows delete-pending access denial')
    def test_delete_pending_access_denial_is_rechecked_then_discarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            measured = {root / 'lease': 4096}
            error = PermissionError('delete pending')
            error.winerror = 5
            with patch('webapp.disk_budget.os.scandir', side_effect=[error, FileNotFoundError()]) as scan:
                self.assertEqual(size_of(root, measured=measured), 0)
            self.assertEqual(scan.call_count, 2)
            self.assertEqual(measured[root / 'lease'], 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows persistent ACL denial')
    def test_persistent_windows_permission_error_is_never_counted_as_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            error = PermissionError('ACL denied')
            error.winerror = 5
            with patch('webapp.disk_budget.os.scandir', side_effect=error) as scan:
                with self.assertRaises(PermissionError):
                    size_of(temporary)
            self.assertEqual(scan.call_count, 5)

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

    def test_stale_maintenance_cache_is_reclaimed_after_worker_exits(self):
        cache = self.budget.runtime / 'cache' / 'worker-maintenance'
        cache.mkdir(parents=True, exist_ok=True)
        (cache / 'leftover.tmp').write_bytes(b'partial maintenance output')
        old = time.time() - 7200
        os.utime(cache, (old, old))
        with file_lock(self.root / 'worker-maintenance.lock') as owned:
            self.assertTrue(owned)
            self.budget._clean_diagnostics(grace=3600)
            self.assertTrue(cache.exists(), 'live maintenance worker owns its cache')
        self.budget._clean_diagnostics(grace=3600)
        self.assertFalse(cache.exists())

    def test_recent_maintenance_cache_is_preserved(self):
        cache = self.budget.runtime / 'cache' / 'worker-maintenance'
        cache.mkdir(parents=True, exist_ok=True)
        leftover = cache / 'leftover.tmp'
        leftover.write_bytes(b'recent maintenance output')
        self.budget._clean_diagnostics(grace=3600)
        self.assertTrue(leftover.exists())

    def test_download_workspace_uses_global_budget_instead_of_cache_budget(self):
        budget = DiskBudget(self.root, {'disk_cache_budget_mb': 1, 'disk_budget_gb': 1,
                                        'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        with budget.lease('downloads', reserve=512 * MIB):
            self.assertEqual(budget.snapshot()['categories']['downloads']['budget_bytes'], GIB)
        with self.assertRaisesRegex(DiskPressure, '分类预算不足'):
            with budget.lease('cache', reserve=2 * MIB):
                pass

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
            self.assertEqual(outcomes, [('ok', 8)] * 16,
                             '\n'.join(str(detail) for status, detail in outcomes if status != 'ok'))
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

    def test_existing_promise_does_not_scan_but_growth_and_low_watermark_still_check(self):
        with self.assertRaises(DiskPressure):
            with self.budget.lease('imports') as lease:
                lease.check(force=True)
                with patch.object(self.budget, '_refresh_usage', side_effect=AssertionError('unneeded scan')):
                    lease.check(force=True)
                with patch.object(self.budget, '_refresh_usage', wraps=self.budget._refresh_usage) as refresh:
                    lease.check(lease.reserved + MIB, force=True)
                    self.assertTrue(refresh.called)
                self.budget.config['disk_min_free_gb'] = 1
                with patch.object(self.budget, '_free', return_value=0):
                    lease.check(force=True)

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
            row = db.execute('SELECT children,reserved FROM leases WHERE id=?', (lease.id,)).fetchone()
            self.assertEqual(json.loads(row[0]), lease.children)
            self.assertGreater(row['reserved'], 0, 'unconfirmed writers must keep their space promise')
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

    def _measured_usage(self, budget, lease, amount, **other):
        # Model one coherent filesystem scan without writing hundreds of MiB.
        usage = {key: other.get(key, 0) for key in budget.limits}
        usage[lease.category] = amount
        with budget.connect() as db:
            db.execute('UPDATE leases SET measured=? WHERE id=?', (amount, lease.id))
            budget._set(db, 'usage', {'time': time.time(), 'bytes': usage})

    def test_growth_only_promises_unwritten_bytes_above_the_low_watermark(self):
        budget = DiskBudget(self.root, {'disk_min_free_gb': 10, 'disk_resume_free_gb': 10})
        with patch.object(budget, '_free', return_value=12 * GIB):
            with budget.lease('downloads', reserve=16 * MIB) as lease:
                self._measured_usage(budget, lease, 288 * MIB)
                with patch.object(budget, '_free', return_value=10 * GIB + 32 * MIB), \
                        patch.object(budget, '_refresh_usage'):
                    budget._reserve(lease, 304 * MIB)
                    self.assertEqual(lease.reserved, 304 * MIB)
                    # The remaining 16 MiB promise still protects future writes.
                    with self.assertRaisesRegex(DiskPressure, '预留空间不足'):
                        budget._reserve(lease, 336 * MIB)
                    self.assertEqual(lease.reserved, 304 * MIB)

    def test_growth_does_not_charge_measured_bytes_twice_against_budgets(self):
        for scope in ('category', 'total'):
            with self.subTest(scope=scope):
                budget = DiskBudget(self.root / scope, {'disk_budget_gb': 1,
                    'disk_import_budget_mb': 320, 'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
                with budget.lease('imports', reserve=16 * MIB) as lease:
                    self._measured_usage(budget, lease, 288 * MIB,
                                         downloads=720 * MIB if scope == 'total' else 0)
                    with patch.object(budget, '_refresh_usage'):
                        budget._reserve(lease, 304 * MIB)
                        self.assertEqual(lease.reserved, 304 * MIB)
                        message = '分类预算不足' if scope == 'category' else '总预算不足'
                        with self.assertRaisesRegex(DiskPressure, message):
                            budget._reserve(lease, (336 if scope == 'category' else 316) * MIB)

    def test_growth_with_unspent_reservation_still_accounts_for_all_new_bytes(self):
        budget = DiskBudget(self.root, {'disk_min_free_gb': 10, 'disk_resume_free_gb': 10})
        with patch.object(budget, '_free', return_value=12 * GIB):
            with budget.lease('downloads', reserve=32 * MIB) as lease:
                self._measured_usage(budget, lease, 16 * MIB)
                with patch.object(budget, '_free', return_value=10 * GIB + 40 * MIB), \
                        patch.object(budget, '_refresh_usage'):
                    with self.assertRaisesRegex(DiskPressure, '预留空间不足'):
                        budget._reserve(lease, 64 * MIB)
                    budget._reserve(lease, 48 * MIB)

    def test_rejection_refreshes_usage_before_interrupting_an_active_download(self):
        budget = DiskBudget(self.root, {'disk_min_free_gb': 10, 'disk_resume_free_gb': 10})
        with patch.object(budget, '_free', return_value=12 * GIB):
            with budget.lease('imports', reserve=MIB) as lease:
                # The cached scan predates the write; disk free already reflects it.
                (lease.path / 'data').write_bytes(b'x' * (2 * MIB))
                with patch.object(budget, '_free', return_value=10 * GIB + 2 * MIB):
                    budget._reserve(lease, 3 * MIB)
                    self.assertEqual(lease.reserved, 3 * MIB)

    def test_budget_rejection_refreshes_files_removed_since_the_cached_scan(self):
        budget = DiskBudget(self.root, {'disk_import_budget_mb': 2,
            'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        with budget.lease('imports', reserve=MIB // 2) as lease:
            stale = lease.path.parent / 'finished-import'
            stale.write_bytes(b'x' * (MIB + MIB // 4))
            budget.snapshot(force=True)
            stale.unlink()
            budget._reserve(lease, MIB)
            self.assertEqual(lease.reserved, MIB)

    def test_actual_low_space_and_resume_watermark_remain_distinct(self):
        budget = DiskBudget(self.root, {'disk_min_free_gb': 10, 'disk_resume_free_gb': 15})
        with patch.object(budget, '_free', return_value=16 * GIB):
            with budget.lease('imports', reserve=MIB) as lease:
                with patch.object(budget, '_free', return_value=9 * GIB):
                    with self.assertRaisesRegex(DiskPressure, '实际剩余空间不足'):
                        budget._reserve(lease, MIB)
                with patch.object(budget, '_free', return_value=12 * GIB):
                    budget._reserve(lease, MIB)
                    with self.assertRaisesRegex(DiskPressure, '恢复水位'):
                        with budget.lease('imports', reserve=MIB):
                            pass

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

    def test_retained_partial_files_release_promise_before_retry(self):
        budget = DiskBudget(self.root, {'disk_import_budget_mb': 4,
            'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        with budget.lease('imports', task='partial', reserve=3*MIB) as previous:
            output = previous.path / 'partial.mp4'
            output.write_bytes(b'x'*MIB)
            previous.preserve = True
        snapshot = budget.snapshot()
        category = snapshot['categories']['imports']
        self.assertTrue(output.is_file())
        self.assertGreaterEqual(category['used_bytes'], MIB)
        self.assertEqual(category['committed_bytes'], category['used_bytes'])
        self.assertNotIn('imports', snapshot['active'])
        with budget.lease('imports', task='partial', reserve=2*MIB):
            self.assertTrue(output.is_file())

    def test_crashed_receipt_releases_unused_promise_without_deleting_result(self):
        with self.budget.lease('test', task='crashed', reserve=GIB) as lease:
            output = lease.path / 'caption.srt'
            output.write_text('a verified partial result')
            (lease.path / '.completed-files.json').write_text('[{"path":"caption.srt","size":25}]')
            lease.preserve = True
        # Simulate the old owner dying before normal retirement ran.
        with self.budget.connect() as db:
            db.execute('UPDATE leases SET reserved=?, measured=0 WHERE id=?', (GIB, lease.id))
        snapshot = self.budget.cleanup(grace=0)
        self.assertTrue(output.is_file())
        self.assertEqual(snapshot['last_cleanup']['pending_results'], 1)
        self.assertEqual(snapshot['committed_bytes'], snapshot['used_bytes'])
        with self.budget.connect(write=False) as db:
            self.assertEqual(db.execute('SELECT reserved FROM leases WHERE id=?', (lease.id,)).fetchone()[0], 0)

    def test_retained_live_child_keeps_promise_until_it_stops(self):
        with self.budget.lease('test', reserve=GIB) as lease:
            lease.preserve = True
            lease.children = [process_identity()]
        with self.budget.connect(write=False) as db:
            self.assertEqual(db.execute('SELECT reserved FROM leases WHERE id=?', (lease.id,)).fetchone()[0], GIB)
        self.assertTrue(lease.path.exists())

    def test_retirement_measures_recent_writes_before_releasing_reservation(self):
        with self.budget.lease('test', reserve=GIB) as lease:
            before = self.budget.snapshot(force=True)['used_bytes']
            (lease.path / 'new.mp4').write_bytes(b'x'*MIB)
            lease.preserve = True
        after = self.budget.snapshot()
        self.assertGreaterEqual(after['used_bytes'], before+MIB)
        self.assertEqual(after['used_bytes'], after['committed_bytes'])

    def test_log_writes_stay_bounded_before_task_finishes(self):
        path = self.root / ('a' * 32 + '.log')
        writer = BoundedLog(path, 65536)
        for i in range(100):
            writer.write((str(i) + ':' + 'x' * 10000 + '\n').encode())
            self.assertLessEqual(path.stat().st_size, 65536)
        self.assertIn(b'99:', path.read_bytes())
        self.assertGreater(int(path.with_suffix('.offset').read_text()), 0)


class DiskWebTests(unittest.TestCase):
    def setUp(self):
        from tests.test_jobs import JobTests
        JobTests.setUp(self)

    def _create_test_download_task(self):
        """Create a test-mode job using the current task-owned storage scope."""
        from webapp.jobs import Jobs
        response = self.client.put('/api/config', json={'disk_min_free_gb': 0,
            'disk_resume_free_gb': 0}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        response = self.client.post('/api/jobs', json={'customer': 'Disk test', 'name': 'Recovery',
            'urls': 'npNnbj46_ik', 'delivery': {'storage': {'storage_mode': 'test'}}}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        identity = response.json['id']
        Jobs(self.store).control(identity, 'start', self.store.runtime(), {})
        task = self.client.get(f'/api/jobs/{identity}/videos').json['items'][0]
        return self.store.get(task['id'], private=True)

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
        # Explicitly verify the source-template fallback; built routes use Vue.

    def test_upload_is_bounded_during_multipart_parsing(self):
        response = self.client.put('/api/config', json={'disk_upload_limit_mb':1}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        identity = self.client.post('/api/jobs', json={'name': 'Upload limit', 'customer': 'Fixture'}, headers=self.headers).json['id']
        response = self.client.post('/api/jobs/' + identity + '/imports', data={'file':(BytesIO(b'x' * (MIB + 1)), 'large.txt')}, headers=self.headers)
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

    def test_known_disk_wait_is_not_claimed_or_dispatched_until_capacity_fits(self):
        response=self.client.put('/api/config',json={'disk_budget_gb':2,'disk_test_budget_gb':1,
            'disk_min_free_gb':0,'disk_resume_free_gb':0},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        task=self._create_test_download_task()
        task=self.store.claim(self.store.owner)
        config={**self.store.runtime(),'storage_mode':'test'}
        budget=DiskBudget(self.store.directory,config)
        with self.assertRaises(DiskPressure) as rejected:
            with budget.lease('test',task=task['id'],reserve=2*GIB):
                self.fail('cannot fit')
        self.store.release(task['id'],self.store.owner,str(rejected.exception),delay=0,
            resource=True,resource_detail=rejected.exception.detail)
        for _ in range(3):
            self.assertIsNone(self.store.claim(self.store.owner,actions=['download']))
            self.assertNotIn('download',self.store.runnable_counts())
        self.assertEqual(self.store.get(task['id'])['attempts'],0)
        check=self.store.create('check',{'config':{},'base':str(self.root)})
        claimed=self.store.claim(self.store.owner)
        self.assertEqual(claimed['id'],check['id'],'disk waits must not block auxiliary work')
        self.store.finish(check['id'],result={},worker_id=self.store.owner)
        response=self.client.put('/api/config',json={'disk_test_budget_gb':2,'disk_budget_gb':3},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.store.claim(self.store.owner,actions=['download'])['id'],task['id'])

    def test_budget_release_wakes_waiting_task_without_physical_free_change(self):
        from webapp.database import tasks
        task=self._create_test_download_task();task=self.store.claim(self.store.owner)
        config={**self.store.runtime(),'storage_mode':'test'}
        budget=DiskBudget(self.store.directory,config)
        with budget.lease('test',reserve=budget.limits['test']-GIB):
            with budget.connect() as db:
                db.execute('INSERT OR REPLACE INTO requirements VALUES (?,?,?,?,?)',
                    (task['id'],budget._version(),2*GIB,4,budget._free()))
            self.store.release(task['id'],self.store.owner,'waiting for budget',delay=0,resource=True)
            self.assertIsNone(self.store.claim(self.store.owner,actions=['download']))
        with patch.object(budget,'_free',return_value=budget._free()):
            self.assertNotIn(task['id'],budget.blocked_tasks())
            claimed=self.store.claim(self.store.owner,actions=['download'])
            self.assertEqual(claimed['id'],task['id'])
            with budget.lease('test',task=task['id'],reserve=GIB) as lease:
                self.assertGreaterEqual(lease.reserved,2*GIB,'prior interruptions must not strand a task once budget fits')

    def test_worker_handles_pressure_without_provider_failure(self):
        from tests.test_progress_cache import MemoryRedis
        from webapp.progress_cache import ProgressCache
        from webapp.workbench_live import live_snapshot, publish_queue
        from webapp.worker.queue import drain_queue
        self.store.progress_cache = ProgressCache(self.store.db.url, client=MemoryRedis())
        task = self._create_test_download_task()
        config = {**self.store.runtime(), 'storage_mode': 'test', 'disk_budget_gb': 4, 'disk_test_budget_gb': 2}
        budget = DiskBudget(self.store.directory, config)
        competing = ExitStack()
        self.addCleanup(competing.close)
        competing.enter_context(budget.lease('test', reserve=GIB))
        executions = []

        def execute(action, payload, report, **context):
            current = self.store.get(task['id'])
            executions.append((context['task_id'], current['worker_id'], current['attempts']))
            self.assertEqual(current['status'], 'running')
            if len(executions) == 2:
                # 另一个进程不能领走正在等空间的视频，顶部和工作台的实时等待只有一个。
                self.assertIsNone(self.store.claim('another-worker', actions=['download']))
                publish_queue(self.store)
                snapshot = live_snapshot(self.store.progress_cache, 'local')
                self.assertEqual(snapshot['disk_waiting'], dict(download=1, processing=0))
                self.assertEqual(current['progress']['stage'], 'disk_wait')
                competing.close()
            self.assertLessEqual(len(executions), 2)
            with budget.lease('test', task=task['id'], reserve=3 * GIB // 2):
                report(dict(stage='preparing_workspace'))
                return dict(file_count=0)

        with patch('webapp.worker.disk_wait.DISK_RECHECK_SECONDS', 0):
            drain_queue(self.store.directory, role='download', execute_task=execute, store_factory=lambda *args: self.store)
        saved = self.store.get(task['id'])
        self.assertEqual(len(executions), 2)
        self.assertEqual(executions[0], executions[1])
        self.assertEqual((executions[0][0], executions[0][2]), (task['id'], 1))
        self.assertEqual(saved['status'], 'succeeded')
        self.assertEqual(saved['attempts'], 1)
        self.assertEqual(self.client.get('/api/statistics').json['summary']['failed'], 0)
        self.assertEqual(live_snapshot(self.store.progress_cache, 'local')['disk_waiting'], dict(download=0, processing=0))

    def test_disk_wait_checks_space_without_reopening_source(self):
        from unittest.mock import Mock
        from webapp.worker.disk_wait import run_with_disk_wait

        heartbeat = SimpleNamespace(stop=Mock(), wait_state=None, wait=None)
        heartbeat.stop.is_set.return_value = False
        def set_wait(state=None, wait=None):
            heartbeat.wait_state, heartbeat.wait = state, wait
        heartbeat.set_wait = set_wait
        space_ready = Mock(side_effect=[False, False, True])
        attempts = []
        def execute(report):
            attempts.append(heartbeat.wait_state)
            report({'stage': 'extracting'})
            if len(attempts) == 1:
                error = DiskPressure('空间不足')
                error.download_space_ready = space_ready
                raise error
            self.assertEqual(heartbeat.wait_state, 'disk_wait')
            report({'stage': 'extracting', 'space_plan': {'peak_bytes': 20 * MIB}})
            return 'ready'

        self.assertEqual(run_with_disk_wait(execute, heartbeat, Mock()), 'ready')
        self.assertEqual(attempts, [None, 'disk_wait'])
        self.assertEqual(space_ready.call_count, 3)
        self.assertIsNone(heartbeat.wait_state)

    def test_real_coordination_lock_is_requeued_without_failure_statistics(self):
        from web_worker import drain
        task = self._create_test_download_task()
        budget = DiskBudget(self.store.directory, self.store.runtime())
        budget.snapshot()
        def plan_download(action, payload, report, *args):
            # 解析完成后才检查空间账本，账本繁忙时不能进入文件下载。
            report({'stage': 'extracting', 'space_plan': {'peak_bytes': 20 * MIB}})
            self.fail('账本繁忙时不能开始下载文件')
        with closing(sqlite3.connect(budget.database)) as locker:
            locker.execute('BEGIN IMMEDIATE')
            with patch('webapp.disk_budget.COORDINATION_TIMEOUT', 0.03), \
                    patch('web_worker._execute', side_effect=plan_download):
                drain(self.store.directory, role='download')
        saved = self.store.get(task['id'])
        self.assertEqual(saved['status'], 'queued')
        self.assertEqual(saved['attempts'], 0)
        self.assertEqual(saved['progress']['stage'], 'scheduling_wait')
        self.assertIsNone(saved['progress']['disk_pressure'])
        self.assertLessEqual(saved['progress']['retry_at'] - time.time(), 5)
        self.assertIn('协调繁忙', saved['error'])
        self.assertEqual(self.client.get('/api/statistics').json['summary']['failed'], 0)

    def test_queued_partial_receipt_and_retry_both_release_crashed_reservation(self):
        task = self._create_test_download_task()
        budget = DiskBudget(self.store.directory, {'disk_min_free_gb':0, 'disk_resume_free_gb':0})
        with budget.lease('test', task=task['id'], reserve=GIB) as lease:
            output = lease.path / 'caption.srt'
            output.write_bytes(b'partial completed output')
            (lease.path / '.completed-files.json').write_text(json.dumps([{
                'path':str(output), 'size':output.stat().st_size, 'complete':True,
                'artifact':{'id':'partial'}}]))
            lease.preserve = True
        with budget.connect() as db:
            db.execute('UPDATE leases SET reserved=? WHERE id=?', (GIB, lease.id))
        snapshot = budget.cleanup(self.store, grace=0)
        self.assertEqual(snapshot['last_cleanup']['pending_results'], 1)
        self.assertEqual(snapshot['used_bytes'], snapshot['committed_bytes'])
        self.assertTrue(output.exists())
        # A retry must release the crashed promise even before the sweep runs.
        with budget.connect() as db:
            db.execute('UPDATE leases SET reserved=? WHERE id=?', (GIB, lease.id))
        self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.recover_test_outputs', return_value=None):
            self.assertIsNone(budget.recover_test_result(self.store, task['id']))
        self.assertTrue(output.exists())
        self.assertEqual(budget.snapshot()['used_bytes'], budget.snapshot()['committed_bytes'])




    def test_late_watchdog_pressure_does_not_requeue_completed_pipeline(self):
        from web_worker import execute
        running = []
        def monitor(lease, on_pressure):
            running.append(lease)
        def download(action, payload, report, *args):
            report({'stage': 'extracting', 'space_plan': {'peak_bytes': 20 * MIB}})
            running[0].error = DiskPressure('late observation during completion')
            return {'file_count':1, 'file_bytes':20}
        task = self._create_test_download_task()
        task = self.store.claim(self.store.owner)
        with patch('webapp.disk_budget.DiskLease.start_monitor', new=monitor):
            with patch('web_worker._execute', side_effect=download):
                result = execute('download', task['payload'], store=self.store, task_id=task['id'])
        self.assertEqual(result['file_count'], 1)
        # Test files retire after terminal settlement and sample handling.
        # A late watchdog observation must not cancel that valid handoff.
        self.assertFalse(result['files_deleted'])
        self.assertIn('_processing_workspace', result)
        self.assertIsNone(running[0].error)

    def test_pressure_wrapped_by_downloader_remains_resource_wait(self):
        from web_worker import execute
        from webapp.disk_budget import DiskLease
        task = self._create_test_download_task()
        task = self.store.claim(self.store.owner)
        reserve = DiskBudget._reserve
        reservation_sizes = []

        def record_reservation(budget, lease, needed):
            reservation_sizes.append(needed)
            return reserve(budget, lease, needed)

        def download(action, payload, report, *args):
            # 模拟解析进度，确认准备目录和分配账号阶段没有提前检查或预留空间。
            report({'stage': 'extracting'})
            self.assertEqual(reservation_sizes, [])
            check_space.assert_not_called()
            try:
                report({'stage': 'extracting', 'space_plan': {'peak_bytes': 20 * GIB}})
            except DiskPressure as error:
                raise RuntimeError('wrapped downloader error') from error
        with patch.object(DiskBudget, '_reserve', new=record_reservation), \
                patch.object(DiskLease, 'check_media_space', autospec=True) as check_space, \
                patch.object(DiskLease, 'start_monitor', autospec=True) as start_monitor, \
                patch('web_worker._execute', side_effect=download):
            with self.assertRaises(DiskPressure):
                execute('download', task['payload'], store=self.store, task_id=task['id'])
        self.assertEqual(reservation_sizes, [20 * GIB])
        check_space.assert_called_once()
        start_monitor.assert_not_called()
