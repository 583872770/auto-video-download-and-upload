"""Real Redis checks use an explicit disposable test endpoint, never REDIS_URL alone."""
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests.test_mysql import StandaloneMySQLFixture
from webapp.database import tasks
from webapp.progress_cache import ProgressCache
from webapp.store import TaskStore


class RealRedisContract:
    def live_task(self):
        import redis
        endpoint = os.environ['VIDEO_TEST_REDIS_URL']
        client = redis.Redis.from_url(endpoint, decode_responses=True, socket_timeout=1,
                                     socket_connect_timeout=1)
        self.addCleanup(client.close)
        client.ping()
        cache = ProgressCache(self.store.db.url, client=client)
        self.store.progress_cache = cache
        self.store.create('check', {})
        task = self.store.claim(self.store.owner, actions=('check',))
        self.assertIsNotNone(task)
        # Every store uses a newly created DB. Remove only this task's key.
        self.addCleanup(client.delete, cache.prefix + task['id'])
        return task, client

    def report(self, task, count):
        return self.store.update_progress(task['id'],
            {'stage': 'downloading', 'downloaded_bytes': count, 'percent': count}, self.store.owner)

    def durable(self, task):
        with self.store.connect() as db:
            return json.loads(db.execute(select(tasks.c.progress).where(tasks.c.id == task['id'])).scalar_one())

    def test_independent_reader_sees_live_progress_and_terminal_state_stays_durable(self):
        task, client = self.live_task()
        self.assertTrue(self.report(task, 10))
        self.assertTrue(self.report(task, 20))
        self.assertEqual(self.durable(task)['downloaded_bytes'], 10)
        with patch.dict(os.environ, {'REDIS_URL': os.environ['VIDEO_TEST_REDIS_URL']}):
            reader = TaskStore(self.root / 'reader', self.store.db.url)
        self.addCleanup(reader.db.engine.dispose)
        self.assertEqual(reader.get(task['id'])['progress']['downloaded_bytes'], 20)
        self.assertTrue(self.store.finish(task['id'], result={}, worker_id=self.store.owner))
        self.assertEqual(reader.get(task['id'])['status'], 'succeeded')
        self.assertEqual(reader.get(task['id'])['progress']['downloaded_bytes'], 20)
        self.assertFalse(self.report(task, 99))

    def test_real_cache_connection_failure_falls_back_to_sql_and_rejects_stale_cache(self):
        import redis
        task, client = self.live_task()
        self.report(task, 10)
        self.report(task, 20)
        with socket.socket() as unused:
            unused.bind(('127.0.0.1', 0))
            offline = redis.Redis(host='127.0.0.1', port=unused.getsockname()[1],
                                  socket_connect_timeout=.1, socket_timeout=.1)
            self.addCleanup(offline.close)
            self.store.progress_cache.client = offline
            self.assertTrue(self.report(task, 30))
        self.assertEqual(self.durable(task)['downloaded_bytes'], 30)
        self.store.progress_cache.client = client
        self.store.progress_cache.retry_at = 0
        self.assertEqual(self.store.get(task['id'])['progress']['downloaded_bytes'], 30)
        self.assertGreater(self.store.progress_cache.health()['counters'].get('error_set', 0), 0)

    def test_previous_owner_cached_progress_cannot_override_a_reclaimed_task(self):
        task, client = self.live_task()
        self.report(task, 10)
        self.report(task, 20)
        self.store.release(task['id'], self.store.owner, 'fixture retry', delay=0)
        self.assertEqual(self.store.get(task['id'])['status'], 'queued')
        current = self.store.claim('next-owner', actions=('check',))
        self.assertEqual(current['id'], task['id'])
        # Backpressure releases refund the attempt. Ownership must still fence
        # the previous worker and its cache even when attempts stay unchanged.
        self.assertEqual(current['attempts'], task['attempts'])
        self.assertEqual(current['worker_id'], 'next-owner')
        self.assertNotEqual(current['worker_id'], task['worker_id'])
        cached = json.loads(client.get(self.store.progress_cache.prefix + task['id']))
        self.assertEqual(cached['attempt'][0], task['worker_id'])
        row = self.store.get(task['id'])
        self.assertEqual(row['progress']['downloaded_bytes'], 10)
        self.assertGreater(self.store.progress_cache.health()['counters'].get('reject_attempt', 0), 0)
        self.assertFalse(self.report(task, 99))
        self.assertFalse(self.store.finish(task['id'], result={}, worker_id=self.store.owner))
        self.assertTrue(self.store.update_progress(task['id'],
            {'stage': 'downloading', 'downloaded_bytes': 30}, 'next-owner'))
        self.assertEqual(self.store.get(task['id'])['progress']['downloaded_bytes'], 30)
        self.assertTrue(self.store.finish(task['id'], result={}, worker_id='next-owner'))


@unittest.skipUnless(os.environ.get('VIDEO_TEST_REDIS_URL'), 'Set VIDEO_TEST_REDIS_URL to an isolated Redis')
class SQLiteRedisProgressTests(RealRedisContract, unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        with patch.dict(os.environ, {'REDIS_URL': os.environ['VIDEO_TEST_REDIS_URL']}):
            self.store = TaskStore(self.root / 'state')
        self.addCleanup(self.store.db.engine.dispose)


@unittest.skipUnless(os.environ.get('VIDEO_TEST_REDIS_URL'), 'Set VIDEO_TEST_REDIS_URL to an isolated Redis')
class MySQLRedisProgressTests(StandaloneMySQLFixture, RealRedisContract, unittest.TestCase):
    pass
