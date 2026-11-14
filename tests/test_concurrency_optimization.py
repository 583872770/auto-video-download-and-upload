from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
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

    def test_explicit_channel_limits_and_delivery_owned_subtitles(self):
        from webapp.settings import validate_settings
        from webapp.downloader import build_ydl_options
        config = {'builtin_transfer_limit':16,'builtin_connection_limit':32,'builtin_resolver_limit':6}
        validate_settings(config)
        channel = registry(config)[0]
        self.assertEqual((channel.transfer_limit, channel.connection_limit, channel.resolver_limit), (16,None,6))
        self.assertNotIn('builtin_connection_limit', config)
        self.assertIsNone(registry({'scheduler_mode':'auto','builtin_transfer_limit':0})[0].transfer_limit)
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(build_ydl_options(config, root)['writesubtitles'])
            self.assertFalse(build_ydl_options(config, root)['writeautomaticsub'])
            self.assertNotIn('subtitleslangs', build_ydl_options(config, root))
        with self.assertRaises(ValueError):
            validate_settings({'builtin_transfer_limit': 65})
