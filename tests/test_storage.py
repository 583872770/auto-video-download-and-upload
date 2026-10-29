"""Disposable downloads must never delete or archive permanent media."""
from concurrent.futures import ThreadPoolExecutor
import functools
import http.server
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from Utils.download_storage import DownloadWorkspace
from tests import test_scale


class StorageContract:
    def set_mode(self, mode):
        return self.client.put('/api/config', json={'storage_mode': mode}, headers=self.headers)

    def test_modes_roundtrip_and_cloud_is_not_implemented(self):
        self.assertEqual(self.client.get('/api/config').json['config']['storage_mode'], 'local')
        self.assertEqual(self.set_mode('test').status_code, 200)
        self.assertEqual(self.client.get('/api/status').json['storage_mode'], 'test')
        self.assertEqual(self.store.runtime()['storage_mode'], 'test')
        before = self.config.read_bytes()
        for mode in ('cloud', 'invalid', None):
            self.assertEqual(self.set_mode(mode).status_code, 400)
            self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.set_mode('local').status_code, 200)

    def test_storage_switch_preserves_in_flight_snapshot_and_queued_tasks(self):
        task = self.store.create('download', {'config': {}, 'base': str(self.root),
                                             'urls': ['https://example.test/fixture.mp4']})
        claimed = self.store.claim('simulated-owner')
        self.assertEqual(claimed['payload']['config']['storage_mode'], 'local')
        self.assertEqual(self.set_mode('test').status_code, 200)
        self.assertEqual(self.store.get(task['id'], private=True)['payload']['config']['storage_mode'], 'local')
        self.assertTrue(self.store.heartbeat(task['id'], 'simulated-owner'))
        self.store.set_downloads_paused(True)
        self.store.stop_running_downloads(lambda owner: True)
        self.store.set_downloads_paused(False)
        retried = self.store.claim('next-owner')
        self.assertEqual(retried['id'], task['id'])
        self.assertEqual(retried['payload']['config']['storage_mode'], 'test')

    def test_old_task_runs_in_test_mode_without_touching_existing_video_or_archive(self):
        import yt_dlp
        from web_worker import execute
        old = {'config': {'path': 'videos', 'proxy_mode': 'direct'}, 'base': str(self.root),
               'urls': ['https://www.youtube.com/watch?v=npNnbj46_ik']}
        task = self.store.create('download', old)
        existing = self.root / 'videos/Original [npNnbj46_ik].mp4'
        existing.parent.mkdir()
        existing.write_bytes(b'permanent video must survive')
        self.store.archive_add('youtube npNnbj46_ik', str(existing))
        self.catalog.index_file(existing, existing.parent)
        self.store.set_downloads_paused(True)
        self.assertEqual(self.set_mode('test').status_code, 200)
        self.store.set_downloads_paused(False)
        self.assertEqual(self.store.claim(self.store.owner)['id'], task['id'])
        outputs = []

        def download(downloader, urls):
            self.assertFalse(downloader.in_download_archive({'id': 'npNnbj46_ik', 'extractor_key': 'Youtube'}))
            target = Path(downloader.prepare_filename({'title': 'Original', 'id': 'npNnbj46_ik', 'ext': 'mp4'}))
            target.parent.mkdir(parents=True, exist_ok=True)
            self.assertTrue(target.is_relative_to(self.store.directory / 'test-downloads' / task['id']))
            outputs.append(target)
            for suffix in ('.mp4', '.info.json', '.jpg', '.vtt', '.part'):
                target.with_suffix(suffix).write_bytes(b'disposable fixture')
            downloader.record_download_archive({'id': 'NEW_FIXTURE', 'extractor_key': 'Youtube'})
            for hook in downloader.params.get('post_hooks') or []:
                hook(str(target))
            return 0

        progress = []
        with patch('webapp.youtube_auth.YouTubeAuth.require'), patch.object(yt_dlp.YoutubeDL, 'download', download), \
             patch('webapp.downloader.require_download_tools'):
            result = execute('download', old, progress.append, store=self.store, task_id=task['id'])
        self.assertTrue(result['test_mode'])
        self.assertTrue(result['files_deleted'])
        self.assertFalse(result['skipped'])
        self.assertTrue(outputs)
        self.assertFalse((self.store.directory / 'test-downloads' / task['id']).exists())
        self.assertEqual(existing.read_bytes(), b'permanent video must survive')
        self.assertTrue(self.store.archive_has('youtube npNnbj46_ik'))
        self.assertFalse(self.store.archive_has('youtube NEW_FIXTURE'))
        self.assertEqual(self.catalog.root_status(existing.parent)['count'], 1)
        self.assertEqual(progress[-1]['stage'], 'cleaning')
        self.store.finish(task['id'], result=result)
        self.assertEqual(self.client.get('/api/tasks?view=history').json['items'][0]['result']['test_mode'], True)
        totals = self.client.get('/api/statistics').json['summary']
        self.assertEqual((totals['test_file_count'], totals['test_file_bytes']), (1, len(b'disposable fixture')))
        self.assertEqual(totals['file_bytes'], 0)

    def test_failed_download_cleans_only_its_workspace_and_keeps_failure(self):
        import yt_dlp
        from web_worker import execute
        self.set_mode('test')
        payload = {'config': {'path': 'videos', 'proxy_mode': 'direct'}, 'base': str(self.root),
                   'urls': ['https://example.test/failure.mp4']}
        task = self.store.create('download', payload)
        with self.store.connect() as db:
            from webapp.database import tasks
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status='running', worker_id=self.store.owner, lease_until=time.time() + 60))
        root = self.store.directory / 'test-downloads' / task['id']

        def download(downloader, urls):
            target = Path(downloader.prepare_filename({'title': 'partial', 'id': 'failure', 'ext': 'mp4'}))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_suffix('.part').write_bytes(b'failed partial')
            raise yt_dlp.utils.DownloadError('HTTP Error 403: fixture denied')

        with patch.object(yt_dlp.YoutubeDL, 'download', download):
            with self.assertRaisesRegex(RuntimeError, '403'):
                execute('download', payload, store=self.store, task_id=task['id'])
        self.assertFalse(root.exists())

    def test_repeated_test_import_can_run_after_success_beside_local_reservation(self):
        local = {'config': {}, 'base': str(self.root), 'urls': ['https://youtu.be/npNnbj46_ik']}
        original = self.store.create_batch([local], deduplicate=True)[0]
        disposable = {**local, 'config': {'storage_mode': 'test'}}
        first = self.store.create_batch([disposable], deduplicate=True)
        self.assertEqual(len(first), 1)
        self.assertEqual(self.store.create_batch([disposable], deduplicate=True), [])
        self.store.finish(first[0]['id'], result={'test_mode': True, 'files_deleted': True})
        self.assertEqual(len(self.store.create_batch([disposable], deduplicate=True)), 1)
        self.assertEqual(self.store.get(original['id'])['status'], 'queued')

    def test_real_parallel_downloads_are_deleted_with_progress_and_history_retained(self):
        fixture = self.root / 'http'
        fixture.mkdir()
        content = b'local disposable download fixture\n' * 4000
        for name in ('one', 'two'):
            (fixture / (name + '.mp4')).write_bytes(content)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(fixture)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(self.set_mode('test').status_code, 200)
        self.store.set_runtime({'storage_mode': 'test', 'worker_count': 2, 'min_free_gb': 0})
        urls = [f'http://127.0.0.1:{server.server_port}/{name}.mp4' for name in ('one', 'two')]
        response = self.client.post('/api/downloads', json={'urls': urls}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        tasks = response.json['items']

        def run(slot):
            return subprocess.run([sys.executable, 'web_worker.py', '--state', str(self.store.directory),
                                   '--slot', str(slot), '--role', 'download'], capture_output=True, timeout=45)

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, (0, 1)))
        self.assertTrue(all(result.returncode == 0 for result in results))
        for task in tasks:
            saved = self.store.get(task['id'])
            self.assertEqual(saved['status'], 'succeeded', saved.get('error'))
            self.assertTrue(saved['result']['files_deleted'])
            self.assertTrue(saved['result']['test_mode'])
            self.assertEqual(saved['progress']['downloaded_bytes'], len(content))
            self.assertFalse((self.store.directory / 'test-downloads' / task['id']).exists())
            self.assertTrue(self.store.log_path(task['id']).is_file())
        self.assertEqual(self.catalog.root_status(self.root / 'videos')['count'], 0)
        self.assertFalse((self.root / 'videos').exists())
        self.assertFalse((self.root / 'videos.txt').exists())
        self.assertEqual(len(self.client.get('/api/tasks?view=history').json['items']), 2)
        totals = self.client.get('/api/statistics').json['summary']
        self.assertEqual((totals['test_file_count'], totals['test_file_bytes']), (2, len(content) * 2))
        self.assertEqual(totals['file_bytes'], 0)


class SQLiteStorageTests(StorageContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


class WorkspaceTests(unittest.TestCase):
    def test_invalid_task_id_and_symlink_cannot_select_a_deletion_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for identity in ('..', '../videos', 'videos', 'a' * 31):
                with self.assertRaises(ValueError):
                    DownloadWorkspace({'storage_mode': 'test'}, root, identity)
            target = root / 'permanent'
            target.mkdir()
            sentinel = target / 'keep.mp4'
            sentinel.write_bytes(b'keep')
            workspace = DownloadWorkspace({'storage_mode': 'test'}, root, 'a' * 32)
            workspace.root.mkdir()
            try:
                workspace.directory.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest('Creating symlinks requires OS privileges')
            with self.assertRaises(ValueError):
                workspace.cleanup()
            self.assertEqual(sentinel.read_bytes(), b'keep')

    def test_cleanup_failure_reports_pending_cleanup_without_failing_download(self):
        import yt_dlp
        from web_worker import execute
        with tempfile.TemporaryDirectory() as directory:
            payload = {'config': {'storage_mode': 'test', 'proxy_mode': 'direct'}, 'base': directory,
                       'urls': ['https://example.test/fixture.mp4']}
            with patch.object(yt_dlp.YoutubeDL, 'download', return_value=0), \
                 patch('webapp.disk_budget.shutil.rmtree', side_effect=PermissionError('fixture file in use')):
                result = execute('download', payload)
                self.assertTrue(result['test_mode'])
                self.assertFalse(result['files_deleted'])
                self.assertIn('清理待重试', result['message'])


