"""Dispatch remains responsive while retention is slow; admission still applies."""
import io
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from webapp.diagnostics import runtime_log
from webapp.store import TaskStore
from webapp.tasks import TaskRunner


class DispatchLatencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = TaskStore(Path(temporary.name) / 'state')
        self.addCleanup(self.store.db.engine.dispose)
        self.runner = TaskRunner(self.store, lambda: {
            'worker_count': 1, 'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        self.log = runtime_log(self.store.directory)

    def download(self):
        return self.store.create('download', {'config': {}, 'urls': ['https://youtu.be/npNnbj46_ik']})

    @staticmethod
    def process():
        return SimpleNamespace(pid=123456, stdout=io.BytesIO(), poll=lambda: None)

    def tick(self):
        with patch.object(self.runner._wake, 'wait', side_effect=lambda _: self.runner.stop.set()):
            self.runner._dispatch(self.log)

    def test_slow_disk_cleanup_does_not_delay_download_dispatch(self):
        self.assert_dispatch_during_maintenance('webapp.disk_budget.DiskBudget.cleanup')

    def test_slow_log_scan_does_not_delay_download_dispatch(self):
        self.assert_dispatch_during_maintenance('webapp.maintenance.prune_disk_logs')

    def assert_dispatch_during_maintenance(self, target):
        entered, release, dispatched = threading.Event(), threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(10)
        def launch(*args, **kwargs):
            dispatched.set()
            return SimpleNamespace(pid=123456, stdout=io.BytesIO(), poll=lambda: None)
        thread = threading.Thread(target=self.runner._watch, daemon=True)
        with patch(target, side_effect=slow), patch('webapp.tasks.subprocess.Popen', side_effect=launch):
            thread.start()
            try:
                self.assertTrue(entered.wait(5), 'maintenance never started')
                self.download()
                self.runner.wake()
                self.assertTrue(dispatched.wait(0.8), 'download waited for blocked maintenance/poll interval')
                self.assertFalse(release.is_set())
            finally:
                release.set()
                self.runner.stop.set()
                self.runner.wake()
                thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_successful_exit_reuses_slot_without_five_second_cooldown(self):
        self.download()
        self.runner.processes[0] = SimpleNamespace(pid=42, poll=lambda: 0)
        self.runner.last_started[0] = time.monotonic()
        with patch('webapp.tasks.subprocess.Popen', return_value=self.process()) as launch, \
                patch('webapp.diagnostics.capture_output'):
            self.tick()
        self.assertEqual(launch.call_count, 1)

    def test_failed_exit_keeps_restart_backoff(self):
        self.download()
        self.runner.processes[0] = SimpleNamespace(pid=42, poll=lambda: 1)
        self.runner.last_started[0] = time.monotonic()
        with patch('webapp.tasks.subprocess.Popen') as launch:
            self.tick()
        launch.assert_not_called()

    def test_disk_block_prevents_download_but_allows_indexing(self):
        self.download()
        self.store.create('index', {'config': {}})
        with patch('webapp.disk_budget.resource_blocked', return_value=True), \
                patch('webapp.tasks.subprocess.Popen', return_value=self.process()) as launch, \
                patch('webapp.diagnostics.capture_output'):
            self.tick()
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.args[0][-1], 'maintenance')

    def test_low_free_space_prevents_download_dispatch(self):
        self.download()
        self.runner.settings = lambda: {'disk_min_free_gb': 1}
        with patch('webapp.tasks.shutil.disk_usage', return_value=SimpleNamespace(free=0)), \
                patch('webapp.tasks.subprocess.Popen') as launch:
            self.tick()
        launch.assert_not_called()

    def test_idle_dispatcher_wakes_immediately_after_queue_change(self):
        idle, dispatched = threading.Event(), threading.Event()
        native_wait = self.runner._wake.wait
        def wait(timeout):
            idle.set()
            return native_wait(timeout)
        def launch(*args, **kwargs):
            dispatched.set()
            return SimpleNamespace(pid=123456, stdout=io.BytesIO(), poll=lambda: None)
        thread = threading.Thread(target=self.runner._dispatch, args=(self.log,), daemon=True)
        with patch.object(self.runner._wake, 'wait', side_effect=wait), \
                patch('webapp.tasks.subprocess.Popen', side_effect=launch):
            thread.start()
            try:
                self.assertTrue(idle.wait(5))
                self.download()
                self.runner.wake()
                self.assertTrue(dispatched.wait(0.8), 'wake failed to interrupt the one-second poll')
            finally:
                self.runner.stop.set()
                self.runner.wake()
                thread.join(5)
            self.assertFalse(thread.is_alive())


class StartWakeTests(unittest.TestCase):
    from tests.test_jobs import JobTests
    setUp = JobTests.setUp
    create = JobTests.create

    def test_start_wakes_supervisor_only_after_committing_selected_job(self):
        identity = self.create()
        def wake():
            self.assertEqual(self.store.selected_download_job(), identity)
            self.assertFalse(self.store.downloads_paused())
        runner = SimpleNamespace(wake=Mock(side_effect=wake))
        self.app.extensions['task_runner'] = runner
        with patch('app.require_downmp3_environment'):
            response = self.client.post(f'/api/jobs/{identity}/control', json={
                'action': 'start', 'resume_system': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        runner.wake.assert_called_once()
        runner.wake.reset_mock()
        self.client.get('/api/jobs/' + identity)
        self.client.post(f'/api/jobs/{identity}/control', json={'action': 'invalid'}, headers=self.headers)
        runner.wake.assert_not_called()


if __name__ == '__main__':
    unittest.main()
