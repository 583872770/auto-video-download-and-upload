"""Batch renewals enforce the same ownership rules as individual heartbeats."""
import unittest

from tests import test_scale, test_hybrid
from webapp.database import settings
from webapp.store import TaskStore


class LeaseOwnershipContract:
    def test_default_recovery_preserves_a_live_lease(self):
        task = self.store.create('check', {})
        self.store.claim('live-owner', actions=['check'])
        self.assertEqual(self.store.recover(), 0)
        self.assertEqual(self.store.get(task['id'])['status'], 'running')

    def test_batch_cannot_renew_another_workers_task(self):
        task = self.store.create('check', {})
        self.store.claim('current-owner', actions=['check'])
        before = self.store.get(task['id'], private=True)['lease_until']
        self.assertEqual(self.store.batch_heartbeat([(task['id'], 'old-owner', 600)]), 0)
        self.assertEqual(self.store.get(task['id'], private=True)['lease_until'], before)
        self.assertEqual(self.store.batch_heartbeat([(task['id'], 'current-owner', 600)]), 1)
        self.assertGreater(self.store.get(task['id'], private=True)['lease_until'], before)

    def test_mixed_owners_and_duplicate_ids_keep_the_correct_lease(self):
        a, b = [self.store.create('check', {}) for _ in range(2)]
        self.store.claim('owner-a', actions=['check'])
        self.store.claim('owner-b', actions=['check'])
        before = self.store.get(b['id'], private=True)['lease_until']
        renewed = self.store.batch_heartbeat([(a['id'], 'owner-a', 120),
            (a['id'], 'stale-owner', 9000), (b['id'], 'owner-a', 9000)])
        self.assertEqual(renewed, 1)
        self.assertEqual(self.store.get(b['id'], private=True)['lease_until'], before)
        self.assertLess(self.store.get(a['id'], private=True)['lease_until'] - before, 130)


class LocalLeaseOwnershipTests(LeaseOwnershipContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_pause_permission_is_immediately_shared_between_store_objects(self):
        peer = TaskStore(self.store.directory)
        self.addCleanup(peer.db.engine.dispose)
        self.store.set_downloads_paused(True)
        self.assertTrue(self.store.downloads_paused())
        peer.set_downloads_paused(False)
        self.assertFalse(self.store.downloads_paused())
        # Execution services also write inside their own transaction.
        with self.store.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='true'))
            self.assertTrue(self.store.downloads_paused(db))
        self.assertTrue(peer.downloads_paused())


class FleetLeaseOwnershipTests(LeaseOwnershipContract, unittest.TestCase):
    serve = test_hybrid.HybridTests.serve

    def setUp(self):
        test_hybrid.HybridTests.setUp(self)
        self.store = self.a
