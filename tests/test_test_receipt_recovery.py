"""Recover completed attempts using the ledger's durable task identity."""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests import test_scale
from tests.test_mysql import StandaloneMySQLFixture
from webapp.database import tasks, test_files
from webapp.disk_budget import DiskBudget, DiskPressure
from web_worker import execute


class ReceiptRecoveryContract:
    def retained_attempt(self, *, logical=True):
        response = self.client.put('/api/config', json={'storage_mode': 'test',
            'disk_min_free_gb': 0, 'disk_resume_free_gb': 0}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        task = self.store.create('download', {'config': {'storage_mode': 'test'},
            'base': str(self.root), 'urls': ['https://youtu.be/npNnbj46_ik']})
        if logical:
            # Reproduce a managed task whose file was counted in an older attempt.
            with self.store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                    source_key='job:receipt-fixture:youtube:npNnbj46_ik'))
        claimed = self.store.claim(self.store.owner)
        self.assertEqual(claimed['id'], task['id'])
        old = self.root / 'registered.mp4'
        content = b'complete video from a previous attempt'
        old.write_bytes(content)
        self.assertTrue(self.store.statistics.record_file(task['id'], old,
            'npNnbj46_ik', 'Fixture', test_mode=True))
        old.unlink()
        self.store.release(task['id'], self.store.owner, 'simulated restart', delay=0)
        budget = DiskBudget(self.store.directory, {'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        with budget.lease('test', task=task['id']) as lease:
            video = lease.path / 'retained.mp4'
            video.write_bytes(content)
            receipt = lease.path / '.completed-files.json'
            receipt.write_text(json.dumps([{'path': str(video.resolve()), 'size': len(content),
                'video_id': 'npNnbj46_ik', 'complete': True, 'method': 'yt_dlp', 'title': 'Fixture'}]))
            lease.preserve = True
        return task['id'], budget, video, receipt

    def ledger(self, task_id):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute(select(test_files).where(
                test_files.c.task_id == task_id)).mappings()]

    def test_new_attempt_path_recovers_without_redownload_or_duplicate_statistics(self):
        task_id, budget, video, receipt = self.retained_attempt()
        recorded = self.ledger(task_id)
        summary = self.store.statistics.overview()['summary']
        claimed = self.store.claim(self.store.owner)
        self.assertEqual(claimed['id'], task_id)
        with patch('web_worker._execute', side_effect=AssertionError('must not redownload')):
            result = execute('download', claimed['payload'], store=self.store, task_id=task_id)
        self.assertTrue(result['recovered'])
        self.assertTrue(result['files_deleted'])
        self.assertFalse(video.exists())
        self.assertTrue(self.store.finish(task_id, result=result, worker_id=self.store.owner))
        self.assertEqual(self.store.get(task_id)['status'], 'succeeded')
        self.assertEqual(self.ledger(task_id), recorded, 'keep the original ledger and session')
        after = self.store.statistics.overview()['summary']
        self.assertEqual(after['test_file_count'], summary['test_file_count'])
        self.assertEqual(after['test_file_bytes'], summary['test_file_bytes'])
        self.assertEqual(after['test_succeeded'], 1)
        self.assertIsNone(self.store.claim(self.store.owner))
        self.assertIsNone(budget.recover_test_result(self.store, task_id))

    def test_cleanup_preserves_queued_completion_until_worker_settles_it(self):
        task_id, budget, video, receipt = self.retained_attempt()
        self.assertTrue(budget.receipts_recorded(self.store, task_id, receipt))
        result = budget.cleanup(self.store, grace=0)
        self.assertTrue(video.exists())
        self.assertEqual(result['last_cleanup']['pending_results'], 1)
        self.store.claim(self.store.owner)
        result = budget.recover_test_result(self.store, task_id)
        self.assertTrue(result['recovered'])
        self.assertFalse(video.exists())

    def test_cleanup_removes_confirmed_duplicate_of_terminal_task(self):
        task_id, budget, video, receipt = self.retained_attempt()
        self.store.claim(self.store.owner)
        self.store.finish(task_id, error='old failure', worker_id=self.store.owner)
        recorded = self.ledger(task_id)
        result = budget.cleanup(self.store, grace=0)
        self.assertFalse(video.exists())
        self.assertEqual(result['last_cleanup']['pending_results'], 0)
        self.assertEqual(self.ledger(task_id), recorded)
        self.assertEqual(self.store.get(task_id)['status'], 'failed')

    def test_changed_video_or_size_is_not_accepted_as_a_duplicate(self):
        for change in ({'video_id': '2Gu7q-KUXCk'}, {'size': 1}, {'complete': False}):
            with self.subTest(change=change):
                task_id, budget, video, receipt = self.retained_attempt()
                items = json.loads(receipt.read_text())
                items[0].update(change)
                receipt.write_text(json.dumps(items))
                self.assertFalse(budget.receipts_recorded(self.store, task_id, receipt))
                self.store.claim(self.store.owner)
                result = None
                try:
                    result = budget.recover_test_result(self.store, task_id)
                except DiskPressure:
                    pass
                self.assertIsNone(result)
                self.assertTrue(video.exists())
                self.assertEqual(len(self.ledger(task_id)), 1)
                self.store.finish(task_id, error='fixture closed', worker_id=self.store.owner)

    def test_legacy_path_identity_does_not_confirm_a_different_attempt(self):
        task_id, budget, video, receipt = self.retained_attempt(logical=False)
        self.assertFalse(budget.receipts_recorded(self.store, task_id, receipt))
        self.store.claim(self.store.owner)
        with patch.object(self.store.statistics, 'record_file', return_value=False):
            with self.assertRaises(DiskPressure):
                budget.recover_test_result(self.store, task_id)
        self.assertTrue(video.exists())

    def test_missing_or_truncated_file_is_preserved_for_inspection(self):
        task_id, budget, video, receipt = self.retained_attempt()
        video.write_bytes(b'truncated')
        self.assertFalse(budget.receipts_recorded(self.store, task_id, receipt))
        video.unlink()
        self.assertFalse(budget.receipts_recorded(self.store, task_id, receipt))
        self.assertTrue(receipt.exists())

    def test_multiple_receipts_cannot_share_one_logical_video_record(self):
        task_id, budget, video, receipt = self.retained_attempt()
        items = json.loads(receipt.read_text())
        receipt.write_text(json.dumps(items * 2))
        self.assertFalse(budget.receipts_recorded(self.store, task_id, receipt))
        self.store.claim(self.store.owner)
        self.assertIsNone(budget.recover_test_result(self.store, task_id))
        self.assertTrue(video.exists())

    def test_empty_receipt_is_not_a_confirmed_result(self):
        task_id, budget, video, receipt = self.retained_attempt()
        receipt.write_text('[]')
        self.assertFalse(budget.receipts_recorded(self.store, task_id, receipt))


class SQLiteReceiptRecoveryTests(ReceiptRecoveryContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


class MySQLReceiptRecoveryTests(StandaloneMySQLFixture, ReceiptRecoveryContract, unittest.TestCase):
    pass
