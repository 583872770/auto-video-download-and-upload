"""Live progress is disposable and must never replace SQL ownership fences."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from sqlalchemy import select

from webapp.database import tasks
from webapp.progress_cache import ProgressCache
from webapp.store import TaskStore


class MemoryRedis:
    def __init__(self):
        self.data = {}
        self.failed = False

    def get(self, key):
        if self.failed:
            raise OSError('cache unavailable')
        return self.data.get(key)

    def set(self, key, value, **kwargs):
        if self.failed:
            raise OSError('cache unavailable')
        self.data[key] = value
        return True

    def delete(self, *keys):
        if self.failed:
            raise OSError('cache unavailable')
        return sum(self.data.pop(key, None) is not None for key in keys)


class ProgressCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.dict('os.environ', {'REDIS_URL': ''}):
            self.store = TaskStore(Path(temp.name))
        self.addCleanup(self.store.db.engine.dispose)
        self.redis = MemoryRedis()
        self.store.progress_cache = ProgressCache(self.store.db.url, client=self.redis)
        task = self.store.create('check', {})
        self.task = self.store.claim(self.store.owner, actions=['check'])
        self.assertEqual(task['id'], self.task['id'])

    def report(self, number, stage='downloading', owner=None):
        return self.store.update_progress(self.task['id'], dict(stage=stage,
            downloaded_bytes=number, percent=number, speed=123), owner or self.store.owner)

    def durable(self):
        with self.store.connect() as db:
            return json.loads(db.execute(select(tasks.c.progress).where(tasks.c.id == self.task['id'])).scalar_one())

    def test_material_reset_discards_only_the_requested_progress_keys_in_batches(self):
        cache = self.store.progress_cache
        identities = [f'{index:032x}' for index in range(205)]
        for identity in identities:
            self.redis.set(cache.prefix + identity, 'old progress')
        self.redis.set(cache.prefix + 'unrelated', 'keep')
        cache.discard(identities)
        self.assertEqual(self.redis.data, {cache.prefix + 'unrelated': 'keep'})

    def test_live_updates_skip_redundant_writes_but_checkpoint_and_stage_change_persist(self):
        self.assertTrue(self.report(10))
        self.assertTrue(self.report(20))
        self.assertEqual(self.durable()['downloaded_bytes'], 10)
        self.assertEqual(self.store.get(self.task['id'])['progress']['downloaded_bytes'], 20)
        self.store._progress_checkpoints[self.task['id']] = (
            self.store._progress_checkpoints[self.task['id']][0], time.monotonic()-6)
        self.assertTrue(self.report(30))
        self.assertEqual(self.durable()['downloaded_bytes'], 30)
        self.assertTrue(self.report(40, 'processing'))
        self.assertEqual(self.durable()['stage'], 'processing')

    def test_cache_outage_falls_back_and_old_cache_cannot_mask_newer_sql_after_recovery(self):
        self.report(10)
        self.report(20)
        self.redis.failed = True
        self.assertTrue(self.report(30))
        self.assertEqual(self.durable()['downloaded_bytes'], 30)
        self.assertEqual(self.store.get(self.task['id'])['progress']['downloaded_bytes'], 30)
        self.redis.failed = False
        self.store.progress_cache.retry_at = 0
        self.assertEqual(self.store.get(self.task['id'])['progress']['downloaded_bytes'], 30)

    def test_revoked_or_reassigned_owner_cannot_publish_or_overlay_new_attempt(self):
        self.report(10)
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == self.task['id']).values(worker_id='new-owner', attempts=2))
        self.assertFalse(self.report(99))
        self.assertEqual(self.store.get(self.task['id'])['progress']['downloaded_bytes'], 10)
        self.assertTrue(self.report(5, owner='new-owner'))
        self.assertEqual(self.store.get(self.task['id'])['progress']['downloaded_bytes'], 5)

    def test_expired_fleet_lease_rejects_progress_even_when_cache_is_healthy(self):
        self.report(10)
        self.store.fleet = SimpleNamespace(boot_id='boot', row=lambda db: {'boot_id':'boot'}, clock=lambda db: time.time())
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == self.task['id']).values(lease_until=time.time()-1))
        self.assertFalse(self.report(99))
        self.assertEqual(self.store.get(self.task['id'])['progress']['downloaded_bytes'], 10)

    def test_terminal_record_includes_latest_cached_progress_and_never_resurrects(self):
        self.report(10)
        self.report(20)
        self.assertTrue(self.store.finish(self.task['id'], result={'message':'done'}, worker_id=self.store.owner))
        row = self.store.get(self.task['id'])
        self.assertEqual(row['status'], 'succeeded')
        self.assertEqual(row['progress']['downloaded_bytes'], 20)
        self.assertEqual(row['progress']['percent'], 100)
        self.assertIsNone(row['progress']['speed'])
        self.assertFalse(self.report(99))

    def test_databases_have_distinct_namespaces_and_bad_cache_values_are_ignored(self):
        self.report(10)
        other = ProgressCache('sqlite:///another.db', client=self.redis)
        with self.store.connect() as db:
            row = db.execute(select(tasks).where(tasks.c.id == self.task['id'])).mappings().one()
        self.assertIsNone(other.read(row))
        key = self.store.progress_cache.prefix + self.task['id']
        self.redis.data[key] = 'broken JSON'
        self.assertEqual(self.store.get(self.task['id'])['progress']['downloaded_bytes'], 10)
