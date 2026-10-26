"""Capacity contention, crash recovery, streaming limits and preserved ledgers."""
from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from webapp.disk_budget import DiskBudget, DiskPressure, GIB, MIB, process_identity, size_of
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


class DiskBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.budget = DiskBudget(self.root, {'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})

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

    def test_completed_file_is_recovered_before_any_new_download(self):
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
                'size':video.stat().st_size,'complete':True,'method':'yt_dlp','video_id':'npNnbj46_ik','title':'Fixture'}]))
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
