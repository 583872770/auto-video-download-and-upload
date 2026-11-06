"""Task settlement must not look like stolen ownership to its heartbeat."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from webapp.lease_guard import LeaseGuard
from webapp.store import TaskStore


class LeaseGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = TaskStore(Path(temporary.name))
        self.addCleanup(self.store.db.engine.dispose)
        self.task = self.store.create('check', {})
        self.store.claim(self.store.owner, actions=['check'])
        self.stop = threading.Event()
        self.guard = LeaseGuard(self.stop)

    def test_renewal_during_successful_settlement(self):
        self.check_settlement_race('succeeded')

    def test_renewal_during_failed_settlement(self):
        self.check_settlement_race('failed')

    def test_renewal_during_release(self):
        self.check_settlement_race('queued')

    def check_settlement_race(self, outcome):
        committed, resume, renewal_started = [threading.Event() for _ in range(3)]
        def settle():
            if outcome == 'queued':
                result = self.store.release(self.task['id'], self.store.owner, 'wait', delay=60)
            else:
                result = self.store.finish(self.task['id'], worker_id=self.store.owner,
                    **({'error': 'fixture failure'} if outcome == 'failed' else {'result': {'ok': True}}))
            committed.set()
            if not resume.wait(5):
                raise AssertionError('settlement not released')
            return result
        def renew():
            renewal_started.set()
            return self.guard.renew(self.store.heartbeat, self.task['id'], self.store.owner)
        with ThreadPoolExecutor(2) as pool:
            settlement = pool.submit(self.guard.settle, settle)
            try:
                self.assertTrue(committed.wait(5))
                # The old unsynchronized heartbeat sees False here.
                self.assertFalse(self.store.heartbeat(self.task['id'], self.store.owner))
                renewal = pool.submit(renew)
                self.assertTrue(renewal_started.wait(5))
            finally:
                resume.set()
            settlement.result(timeout=5)
            self.assertIsNone(renewal.result(timeout=5))
        self.assertTrue(self.stop.is_set())
        self.assertEqual(self.store.get(self.task['id'])['status'], outcome)

    def test_real_ownership_loss_is_still_reported(self):
        self.store.release(self.task['id'], self.store.owner, 'reassigned', delay=0)
        self.store.claim('another-worker', actions=['check'])
        self.assertFalse(self.guard.renew(self.store.heartbeat, self.task['id'], self.store.owner))
        self.assertFalse(self.stop.is_set())

    def test_failed_settlement_keeps_renewal_active(self):
        with self.assertRaises(OSError):
            self.guard.settle(Mock(side_effect=OSError('database unavailable')))
        self.assertFalse(self.stop.is_set())
        self.assertTrue(self.guard.renew(self.store.heartbeat, self.task['id'], self.store.owner))
