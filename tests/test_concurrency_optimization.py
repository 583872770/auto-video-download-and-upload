from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from webapp.channels import registry
from webapp.database import download_permits, tasks
from webapp.download_metrics import DownloadMeter
from webapp.store import TaskStore
from webapp.throughput import _atomic


class MetricTests(unittest.TestCase):
    def test_parallel_progress_counts_and_publications_remain_consistent(self):
        with tempfile.TemporaryDirectory() as root:
            meter = DownloadMeter(root)
            meter.begin(registry({'download_method': 'yt_dlp'})[0])
            def update(stream):
                for size in range(1, 61):
                    meter.hook({'filename': str(stream), 'downloaded_bytes': size})
                    meter.publish(force=True)
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(update, range(8)))
            self.assertEqual(meter.data['received_bytes'], 480)
            self.assertEqual(json.loads(meter.path.read_text())['received_bytes'], 480)
            self.assertEqual(list(meter.path.parent.glob('*.tmp')), [])

    def test_atomic_writers_do_not_share_temporary_names(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'sample.json'
            barrier = threading.Barrier(6)
            seen, lock = set(), threading.Lock()
            replace = Path.replace
            def overlapping(temporary, destination):
                with lock:
                    first = temporary not in seen
                    seen.add(temporary)
                if first:
                    barrier.wait(timeout=10)
                return replace(temporary, destination)
            with patch.object(Path, 'replace', overlapping), ThreadPoolExecutor(6) as pool:
                list(pool.map(lambda index: _atomic(path, {'index': index}), range(6)))
            self.assertIn(json.loads(path.read_text())['index'], range(6))
            self.assertEqual(list(Path(root).glob('*.tmp')), [])

    def test_stat_write_failure_does_not_fail_download_and_recovers(self):
        with tempfile.TemporaryDirectory() as root:
            meter = DownloadMeter(root)
            with patch('webapp.download_metrics._atomic', side_effect=OSError('disk unavailable')), self.assertLogs('webapp.download_metrics') as logs:
                meter.verified(100)
                meter.finish(True)
            self.assertEqual(len(logs.output), 1)
            self.assertEqual(meter.publish_error, 'OSError')
            meter.publish(force=True)
            self.assertIsNone(meter.publish_error)
            self.assertEqual(json.loads(meter.path.read_text())['verified_bytes'], 100)


class TransactionTests(unittest.TestCase):
    def test_permit_deadlock_retries_rolled_back_db_work_not_transfer(self):
        from webapp.channel_scheduler import ChannelScheduler
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(root)
            try:
                job = store.create('download', {'base': root, 'urls': ['https://youtu.be/jNQXAC9IVRw']})
                with store.connect() as db:
                    db.execute(tasks.update().where(tasks.c.id == job['id']).values(status='running', worker_id=store.owner, lease_until=time.time()+60))
                scheduler = ChannelScheduler(store, {'download_method': 'yt_dlp'}, job['id'])
                admit = scheduler._admit
                calls = []
                def deadlock(db, stamp, requirements):
                    value = admit(db, stamp, requirements)
                    calls.append(value)
                    if len(calls) == 1:
                        raise OperationalError('fixture', {}, Exception(1213, 'Deadlock'))
                    return value
                transfers = []
                with patch.object(scheduler, '_admit', side_effect=deadlock):
                    with scheduler.permit(scheduler.channels[0], 'transfer'):
                        transfers.append('download')
                        with store.connect() as db:
                            tokens = set(db.execute(select(download_permits.c.token)).scalars())
                            self.assertEqual(len(tokens), 1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(transfers, ['download'])
                with store.connect() as db:
                    self.assertEqual(db.execute(select(download_permits)).all(), [])
            finally:
                store.db.engine.dispose()

    def test_lost_connection_is_not_retried_and_deadlocks_are_bounded(self):
        from webapp.db_retry import transaction_retry
        for code, attempts in ((2006, 1), (1213, 3)):
            with self.subTest(code=code):
                with patch('webapp.db_retry.time.sleep'), patch('builtins.print', side_effect=OperationalError('fixture', {}, Exception(code, 'error'))) as operation:
                    with self.assertRaises(OperationalError):
                        transaction_retry(operation)
                    self.assertEqual(operation.call_count, attempts)


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = TaskStore(self.root/'state')
        self.addCleanup(self.store.db.engine.dispose)
        self.store.set_downloads_paused(False)
        self.config = {'path': str(self.root/'videos'), 'proxy_mode': 'direct', 'download_method': 'yt_dlp',
                       'disk_min_free_gb': 0, 'disk_resume_free_gb': 0, 'download_attempts': 3}
        self.video = self.root/'videos'/'title [jNQXAC9IVRw].mp4'
        self.video.parent.mkdir()
        self.video.write_bytes(b'fixture video')

    def queue(self):
        job = self.store.create('download', {'base': str(self.root), 'config': self.config, 'urls':['https://youtu.be/jNQXAC9IVRw']})
        result = {'message': 'video complete', '_subtitle_jobs': [{'base': str(self.root), 'config': self.config,
                    'video_file': str(self.video), 'urls': ['https://youtu.be/jNQXAC9IVRw']}]}
        self.assertTrue(self.store.finish(job['id'], result=result))
        self.assertFalse(self.store.finish(job['id'], result=result))
        parent = self.store.get(job['id'])
        self.assertNotIn('_subtitle_jobs', parent['result'])
        self.assertEqual(len(parent['subtitle_tasks']), 1)
        self.assertIn('subtitles', self.store.runnable_counts())
        child = self.store.claim(self.store.owner, actions=('subtitles',))
        return parent, child

    def test_video_commit_enqueues_once_and_last_video_does_not_block_subtitles(self):
        parent, child = self.queue()
        self.assertEqual(parent['status'], 'succeeded')
        self.assertIsNotNone(child)
        self.assertEqual(self.store.counts(actions=('subtitles',))['total'], 1)
        self.assertEqual(self.store.counts(actions=('download',))['succeeded'], 1)

    def test_subtitle_429_cools_only_subtitles_and_preserves_video(self):
        import yt_dlp
        from web_worker import execute
        from webapp.channel_scheduler import ChannelScheduler
        from webapp.throughput import PipelineBusy
        parent, child = self.queue()
        with patch.object(yt_dlp.YoutubeDL, 'download', side_effect=RuntimeError('HTTP Error 429: Too Many Requests')):
            with self.assertRaisesRegex(RuntimeError, '429'):
                execute('subtitles', child['payload'], store=self.store, task_id=child['id'])
        scheduler = ChannelScheduler(self.store, self.config, child['id'])
        with self.store.connect() as db:
            state = scheduler._state(db, 'subtitles:youtube-origin')
            self.assertGreater(state['cooldown_until'], time.time())
            self.assertEqual(scheduler._state(db, 'upstream:youtube-origin'), {})
        with scheduler.permit(scheduler.channels[0], 'transfer', timeout=0):
            pass
        with self.assertRaises(PipelineBusy):
            execute('subtitles', child['payload'], store=self.store, task_id=child['id'])
        self.assertEqual(self.store.get(parent['id'])['status'], 'succeeded')
        self.assertEqual(self.video.read_bytes(), b'fixture video')

    def test_pause_blocks_subtitle_claim_and_success_never_downloads_media(self):
        import yt_dlp
        from web_worker import execute
        parent, child = self.queue()
        self.store.release(child['id'], self.store.owner, 'fixture', delay=0)
        self.store.set_downloads_paused(True)
        self.assertNotIn('subtitles', self.store.runnable_counts())
        self.assertNotIn('subtitles', self.store.runnable_roles())
        self.assertIsNone(self.store.claim(actions=('subtitles',)))
        self.store.set_downloads_paused(False)
        child = self.store.claim(actions=('subtitles',))
        params = []
        def download(downloader, addresses):
            params.append(dict(downloader.params))
            return 0
        with patch.object(yt_dlp.YoutubeDL, 'download', download):
            result = execute('subtitles', child['payload'], store=self.store, task_id=child['id'])
        self.store.finish(child['id'], result=result, worker_id=self.store.owner)
        self.assertEqual(self.store.get(parent['id'])['subtitle_tasks'][0]['status'], 'succeeded')
        self.assertTrue(params[0]['skip_download'])
        self.assertIsNone(params[0]['download_archive'])
        self.assertEqual(params[0]['subtitleslangs'], ['en','zh-Hans','zh-Hant'])

    def test_separate_subtitle_process_drains_after_last_video(self):
        parent, child = self.queue()
        self.store.release(child['id'], self.store.owner, 'fixture', delay=0)
        self.video.unlink()
        process = subprocess.run([sys.executable, 'web_worker.py', '--state', str(self.store.directory),
            '--slot', '-2', '--role', 'subtitles'], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'VIDEO_WORKER_DATABASE_URL': self.store.db.url, 'PYTHONUTF8': '1'},
            capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr.decode('utf-8', errors='replace'))
        finished = self.store.get(child['id'])
        self.assertEqual(finished['status'], 'succeeded')
        self.assertTrue(finished['result']['skipped'])
        self.assertEqual(self.store.get(parent['id'])['status'], 'succeeded')
        self.assertTrue((self.store.directory / 'worker-subtitles.lock').exists())


class BudgetAndPolicyTests(unittest.TestCase):
    def test_budget_estimate_accounts_for_peak_size_without_growing_budget(self):
        from webapp.disk_budget import DiskBudget, GIB
        with tempfile.TemporaryDirectory() as root:
            config = {'storage_mode': 'test', 'disk_budget_gb': 20, 'disk_test_budget_gb':16,
                      'disk_min_free_gb':0, 'disk_resume_free_gb':0, 'worker_count':16}
            budget = DiskBudget(root, config)
            for size in (GIB, GIB, 2*GIB):
                budget.record_video_size(size)
            snapshot = budget.snapshot()
            estimate = snapshot['capacity_estimate']
            self.assertEqual(estimate['peak_bytes'], 4*GIB)
            self.assertLessEqual(estimate['suggested_slots'], 4)
            self.assertEqual(estimate['budget_for_configured_bytes'], 64*GIB)
            self.assertEqual(snapshot['budget_bytes'], 20*GIB)

    def test_explicit_channel_limits_and_subtitle_policy_validation(self):
        from webapp.settings import validate_settings
        from webapp.downloader import build_ydl_options
        config = {'builtin_transfer_limit':16,'builtin_connection_limit':32,'builtin_resolver_limit':6}
        validate_settings(config)
        channel = registry(config)[0]
        self.assertEqual((channel.transfer_limit, channel.connection_limit, channel.resolver_limit), (16,32,6))
        self.assertEqual(registry({'scheduler_mode':'auto','builtin_transfer_limit':0})[0].transfer_limit, 32)
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(build_ydl_options(config, root)['writesubtitles'])
            self.assertTrue(build_ydl_options({**config,'subtitle_mode':'required'}, root)['writesubtitles'])
        for values in ({'subtitle_languages':'all'}, {'subtitle_languages':'.*'}, {'builtin_transfer_limit':65}):
            if values.get('subtitle_languages') == 'all':
                values['subtitle_languages']='en,'*9
            with self.assertRaises(ValueError):
                validate_settings(values)
