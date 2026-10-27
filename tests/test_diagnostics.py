"""Bounded disk usage and useful, secret-free diagnostics under failures."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from webapp.diagnostics import (SafeOutput, capture_output, diagnostic_context, error_details, active_task_log,
                                event, process_diagnostics, read_runtime_logs, runtime_log, safe_text)
from webapp.maintenance import BoundedLog, prune_disk_logs
from webapp.disk_budget import size_of


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_redacts_proxy_database_signed_url_and_header_credentials(self):
        text = ('mysql+pymysql://person:DB_PASSWORD@db:3306/video '
                'https://cdn.test/video?sig=SIGNED_SECRET#fragment '
                'socks5://person:PROXY_PASSWORD@proxy:1080\n'
                'Authorization: Bearer AUTH_SECRET\n'
                'Cookie: sid=COOKIE_SECRET\npassword="PASS_SECRET" token=TOKEN_SECRET\n'
                "{'Cookie': 'sid=DICT_SECRET', 'Authorization': 'Bearer DICT_AUTH'}")
        result = safe_text(text)
        for secret in ('DB_PASSWORD', 'SIGNED_SECRET', 'fragment', 'PROXY_PASSWORD',
                       'AUTH_SECRET', 'COOKIE_SECRET', 'PASS_SECRET', 'TOKEN_SECRET', 'DICT_SECRET', 'DICT_AUTH'):
            self.assertNotIn(secret, result)
        self.assertIn('db:3306/video', result)
        self.assertIn('cdn.test/video', result)

    def test_split_pipe_chunks_and_oversized_lines_never_bypass_redaction(self):
        chunks = []
        output = SafeOutput(chunks.append)
        for part in (b'prefix https://person:PROXY_', b'SECRET@cdn/a?sig=SIGNED_',
                     b'SECRET\nAuthorization: Bear', b'er HEADER_SECRET\n',
                     b'a' * 18000, b'Cookie: LONG_SECRET', b'\nnormal\n'):
            output.feed(part)
        output.finish()
        text = b''.join(chunks).decode()
        self.assertNotIn('SECRET', text)
        self.assertIn('oversized log line omitted', text)
        self.assertIn('normal', text)
        self.assertLess(len(text), 1000)

    def test_events_keep_execution_context_causes_and_valid_json(self):
        with process_diagnostics(self.root, 0), diagnostic_context(task_id='a' * 32, attempt=2):
            try:
                try:
                    raise OSError('TLS EOF https://cdn.test/a?sig=PRIVATE_SIGNATURE')
                except OSError as cause:
                    raise RuntimeError('browser failed') from cause
            except RuntimeError as error:
                event('attempt_end', console=False, **error_details(error))
        text = (self.root / 'logs/instance/0.log').read_text(encoding='utf-8')
        record = json.loads(text.split('[event] ', 1)[1])
        self.assertEqual(record['attempt'], 2)
        self.assertEqual(record['task_id'], 'a' * 32)
        self.assertEqual(record['causes'][1]['type'], 'OSError')
        self.assertNotIn('PRIVATE_SIGNATURE', text)

    def test_structured_header_error_remains_valid_json_after_pipe_capture(self):
        chunks = []
        output = SafeOutput(chunks.append, timestamp=True)
        record = {'event': 'failure', 'message': 'Cookie: sid=PRIVATE_COOKIE', 'access_token': 'PRIVATE_TOKEN'}
        output.feed(('[event] ' + json.dumps(record) + '\n').encode('utf-8'))
        output.finish()
        text = b''.join(chunks).decode('utf-8')
        cleaned = json.loads(text.split('[event] ', 1)[1])
        self.assertEqual(cleaned['event'], 'failure')
        self.assertNotIn('PRIVATE_COOKIE', text)
        self.assertNotIn('PRIVATE_TOKEN', text)

    def test_real_child_crash_is_drained_redacted_and_bounded(self):
        writer = runtime_log(self.root, config={'disk_log_budget_mb': 1})
        process = subprocess.Popen([sys.executable, '-u', '-c',
            "import sys; print(('x'*900+'\\n')*200); print('https://person:PROXY_SECRET@cdn/a?sig=SIGNED_SECRET'); raise RuntimeError('fixture boot failure')"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        thread = capture_output(process.stdout, writer)
        self.assertEqual(process.wait(timeout=20), 1)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertLessEqual(writer.path.stat().st_size, writer.maximum)
        text = read_runtime_logs(self.root)
        self.assertIn('fixture boot failure', text)
        self.assertNotIn('PROXY_SECRET', text)
        self.assertNotIn('SIGNED_SECRET', text)

    def test_write_failure_is_nonfatal_and_sets_one_small_warning_file(self):
        writer = BoundedLog(self.root / 'output.log', 4096, warning_path=self.root / 'log-write-warning.json')
        original = Path.open
        def fail(path, *args, **kwargs):
            if path == writer.path:
                raise OSError(28, 'No space left on device')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', new=fail):
            for _ in range(100):
                writer.write(b'cannot write')
        warning = json.loads(writer.warning_path.read_text(encoding='utf-8'))
        self.assertEqual(warning['errno'], 28)
        self.assertLess(writer.warning_path.stat().st_size, 1024)

    def test_small_budget_bounds_all_fixed_instance_files_even_after_many_attempts(self):
        for slot in ['supervisor', 'maintenance', *range(16)]:
            writer = runtime_log(self.root, slot, {'disk_log_budget_mb': 1})
            for _ in range(30):
                writer.write(b'event ' + b'x' * 3000 + b'\n')
            self.assertLessEqual(writer.path.stat().st_size, 8192)
        self.assertEqual(len(list((self.root / 'logs/instance').glob('*.log'))), 18)
        self.assertLess(size_of(self.root / 'logs'), 256 * 1024)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        @contextmanager
        def unavailable():
            raise RuntimeError('database temporarily unavailable')
            yield
        self.store = SimpleNamespace(directory=self.root, connect=unavailable)

    def log(self, index, size, age=300):
        path = self.root / 'logs' / f'{index:032x}.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * size)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def test_total_capacity_cleanup_continues_during_database_outage(self):
        for index in range(180):
            self.log(index, 8192)
        runtime_log(self.root, config={'disk_log_budget_mb': 1}).write(b'supervisor diagnostic')
        prune_disk_logs(self.store, {'disk_log_budget_mb': 1})
        self.assertLess(size_of(self.root / 'logs'), 1024 * 1024)
        self.assertTrue((self.root / 'logs/instance/supervisor.log').exists())

    def test_file_count_stops_many_tiny_logs_from_accumulating(self):
        for index in range(1050):
            self.log(index, 1)
        prune_disk_logs(self.store, {'disk_log_budget_mb': 64})
        self.assertLessEqual(len(list((self.root / 'logs').glob('*.log'))), 1024)

    def test_age_low_space_and_orphan_offsets_are_cleaned(self):
        old = self.log(1, 100, age=8 * 86400)
        completed = self.log(2, 100, age=300)
        recent = self.log(3, 100, age=10)
        orphan = self.root / 'logs' / ('f' * 32 + '.offset')
        orphan.write_bytes(b'100')
        os.utime(orphan, (time.time() - 300, time.time() - 300))
        prune_disk_logs(self.store, {'disk_log_budget_mb': 64}, low_space=True)
        self.assertFalse(old.exists())
        self.assertFalse(completed.exists())
        self.assertFalse(orphan.exists())
        self.assertTrue(recent.exists())

    def test_expired_instance_log_is_removed_without_touching_media(self):
        writer = runtime_log(self.root)
        writer.write(b'old startup')
        os.utime(writer.path, (time.time() - 8 * 86400, time.time() - 8 * 86400))
        media = self.root / 'videos/keep.mp4'
        media.parent.mkdir()
        media.write_bytes(b'keep')
        prune_disk_logs(self.store, {'disk_log_budget_mb': 1})
        self.assertFalse(writer.path.exists())
        self.assertEqual(media.read_bytes(), b'keep')

    def test_active_process_marker_protects_old_log_when_database_is_down(self):
        active = self.log(1, 100, age=8 * 86400)
        with active_task_log(self.root, 0, active.stem):
            prune_disk_logs(self.store, {'disk_log_budget_mb': 1}, low_space=True)
            self.assertTrue(active.exists())
        prune_disk_logs(self.store, {'disk_log_budget_mb': 1}, low_space=True)
        self.assertFalse(active.exists())


class DiagnosticWebTests(unittest.TestCase):
    from tests.test_scale import ScaleTests
    setUp = ScaleTests.setUp

    def test_instance_logs_endpoint_shows_safe_bounded_output(self):
        with process_diagnostics(self.store.directory, 0):
            event('fixture', console=False, message='https://person:PRIVATE_PROXY@cdn/a?sig=PRIVATE_SIG')
        response = self.client.get('/api/disk/logs')
        self.assertEqual(response.status_code, 200)
        self.assertIn('fixture', response.json['text'])
        self.assertNotIn('PRIVATE_PROXY', response.json['text'])
        self.assertNotIn('PRIVATE_SIG', response.json['text'])
        self.assertLess(len(response.json['text']), 65536)

    def test_failed_attempt_records_phase_retry_and_safe_database_error(self):
        from web_worker import drain
        from webapp.downmp3 import Downmp3Error
        task = self.store.create('download', {'config': {'download_method': 'downmp3', 'path': 'videos'},
            'base': str(self.root), 'urls': ['https://www.youtube.com/watch?v=npNnbj46_ik']})
        def fail(action, payload, report, **kwargs):
            report({'stage': 'requesting_api', 'stage_seconds': {'requesting_api': 3.5}})
            raise Downmp3Error('TLS EOF https://cdn/a?sig=PRIVATE_SIG', retryable=True)
        with process_diagnostics(self.store.directory, 0), patch('web_worker.execute', side_effect=fail), \
                patch('web_worker.os.dup2'), patch('builtins.print'), patch('web_worker.traceback.print_exc'):
            drain(self.store.directory)
        row = self.store.get(task['id'])
        self.assertEqual(row['status'], 'queued')
        self.assertNotIn('PRIVATE_SIG', row['error'])
        text = (self.store.directory / 'logs/instance/0.log').read_text(encoding='utf-8')
        records = [json.loads(line.split('[event] ', 1)[1]) for line in text.splitlines()]
        end = next(record for record in records if record['event'] == 'attempt_end')
        self.assertEqual(end['task_id'], task['id'])
        self.assertEqual(end['attempt'], 1)
        self.assertEqual(end['failed_stage'], 'requesting_api')
        self.assertTrue(end['retry_scheduled'])
        self.assertNotIn('PRIVATE_SIG', text)

    def test_log_write_warning_is_visible_in_disk_status(self):
        (self.store.directory / 'log-write-warning.json').write_text(json.dumps({
            'time': time.time(), 'message': 'fixture disk warning'}), encoding='utf-8')
        response = self.client.get('/api/disk')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['log_warning'], 'fixture disk warning')

    def test_reading_missing_or_pruned_logs_does_not_recreate_empty_shards(self):
        task = self.store.create('check', {'config': {}, 'base': str(self.root)})
        root = self.store.directory / 'logs'
        before = set(root.rglob('*'))
        response = self.client.get(f'/api/tasks/{task["id"]}/logs')
        self.assertEqual(response.json['state'], 'pending')
        self.assertEqual(set(root.rglob('*')), before)
        self.store.finish(task['id'], result={'message': 'ok'})
        path = self.store.log_path(task['id'])
        path.write_bytes(b'old log')
        os.utime(path, (time.time() - 48 * 3600,) * 2)
        prune_disk_logs(self.store, {'disk_log_budget_mb': 1})
        before = set(root.rglob('*'))
        response = self.client.get(f'/api/tasks/{task["id"]}/logs')
        self.assertEqual(response.json['state'], 'pruned')
        self.assertIn('自动清理', response.json['message'])
        self.assertEqual(set(root.rglob('*')), before)

    def test_real_supervisor_worker_writes_attempt_and_exit_logs(self):
        from webapp.tasks import TaskRunner
        from webapp.settings import DEFAULTS
        task = self.store.create('index', {'config': {'path': 'videos'}, 'base': str(self.root)})
        runner = TaskRunner(self.store, lambda: {**DEFAULTS, 'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        thread = threading.Thread(target=runner._watch, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.store.get(task['id'])['status'] == 'succeeded' and 'worker_exit' in read_runtime_logs(self.store.directory):
                    break
                time.sleep(0.1)
            self.assertEqual(self.store.get(task['id'])['status'], 'succeeded')
            self.assertIn('worker_exit', read_runtime_logs(self.store.directory))
            log = self.store.log_path(task['id'], create=False).read_text(encoding='utf-8')
            self.assertIn('attempt_start', log)
            self.assertIn('attempt_end', log)
            self.assertIn('execution_id', log)
        finally:
            runner.stop.set()
            thread.join(timeout=5)
            for process in list(runner.processes.values()):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
