"""Resource bounds, progress persistence and queue dispatch regressions."""
import asyncio
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import event, text

from webapp.downloader import build_ydl_options
from tests import test_scale
from tests.test_mysql import StandaloneMySQLFixture
from webapp.clash import ClashManager, parse_subscription
from webapp.downloads import DownloadProgress


class ProgressRateTests(unittest.TestCase):
    def test_progress_is_throttled_but_stage_transitions_and_finish_are_immediate(self):
        values = []
        clock = [10.0]
        with patch('webapp.downloads.time.monotonic', side_effect=lambda: clock[0]):
            tracker = DownloadProgress(values.append)
            tracker.hook({'status': 'downloading', 'downloaded_bytes': 1})
            self.assertEqual(len(values), 2)  # Extracting -> downloading is immediate.
            for count in range(2, 100):
                clock[0] += 0.01
                tracker.hook({'status': 'downloading', 'downloaded_bytes': count})
            self.assertEqual(len(values), 2)
            clock[0] += 1
            tracker.hook({'status': 'downloading', 'downloaded_bytes': 100})
            self.assertEqual(values[-1]['downloaded_bytes'], 100)
            tracker.hook({'status': 'finished', 'downloaded_bytes': 101})
            self.assertEqual(values[-1]['stage'], 'processing')
            self.assertEqual(values[-1]['downloaded_bytes'], 101)


class NodeCacheTests(unittest.TestCase):
    def test_cache_is_invalidated_by_replace_and_cannot_be_mutated_by_callers(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = ClashManager(temporary)
            path = Path(temporary) / 'nodes.yaml'
            content = b'proxies:\n- {name: one, type: http, server: localhost, port: 8080}\n'
            path.write_bytes(content)
            with patch('webapp.clash.parse_subscription', wraps=parse_subscription) as parse:
                manager.nodes()[0]['name'] = 'mutated'
                self.assertEqual(manager.nodes()[0]['name'], 'one')
                self.assertEqual(parse.call_count, 1)
                replacement = path.with_suffix('.tmp')
                replacement.write_bytes(content.replace(b'one', b'two'))
                replacement.replace(path)
                self.assertEqual(manager.nodes()[0]['name'], 'two')
                self.assertEqual(parse.call_count, 2)
                path.unlink()
                self.assertEqual(manager.nodes(), [])


class DispatchContract:
    def test_only_due_queues_dispatch_and_running_work_does_not_spawn_workers(self):
        task = self.store.create('download', {'config': {}, 'urls': ['https://example.test/1']})
        self.assertEqual(self.store.runnable_roles(), {'regular', 'download'})
        self.assertEqual(self.store.runnable_counts()['download'], 1)
        claimed = self.store.claim()
        self.assertEqual(claimed['id'], task['id'])
        self.assertEqual(self.store.runnable_roles(), set())
        self.store.release(task['id'], self.store.owner, 'fixture backoff', delay=60)
        self.assertEqual(self.store.runnable_roles(), set())
        self.store.create('index', {'config': {}})
        self.assertEqual(self.store.runnable_roles(), {'maintenance'})
        self.store.create('check', {'config': {}})
        self.assertEqual(self.store.runnable_roles(), {'maintenance', 'regular'})
        self.store.set_downloads_paused(True)
        self.store.create('import', {'config': {}})
        self.assertEqual(self.store.runnable_roles(), {'maintenance', 'regular'})

    def test_fragment_concurrency_validation_persistence_and_download_options(self):
        self.assertEqual(self.client.get('/api/config').json['config']['fragment_concurrency'], 4)
        for value in (0, 17, True, 2.5, 'bad'):
            result = self.client.put('/api/config', json={'fragment_concurrency': value}, headers=self.headers)
            self.assertEqual(result.status_code, 400, result.json)
        result = self.client.put('/api/config', json={'fragment_concurrency': '2'}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        config = self.client.get('/api/config').json['config']
        self.assertEqual(build_ydl_options(config, self.root)['concurrent_fragment_downloads'], 2)
        # Explicitly verify the source-template fallback; built routes use Vue.


class SQLiteDispatchTests(DispatchContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


class MySQLPerformanceTests(StandaloneMySQLFixture, DispatchContract, unittest.TestCase):
    def test_connections_are_reused_bounded_and_recover_after_server_disconnect(self):
        engine = self.store.db.engine
        engine.dispose()
        connects = []
        def connected(*args):
            connects.append(True)
        event.listen(engine, 'connect', connected)
        self.addCleanup(event.remove, engine, 'connect', connected)
        for _ in range(4):
            self.store.counts()
        self.assertEqual(len(connects), 1)
        self.assertEqual(engine.pool.size(), 2)
        with engine.connect() as db:
            identity = db.execute(text('SELECT CONNECTION_ID()')).scalar_one()
        with self.admin.connect() as db:
            db.exec_driver_sql(f'KILL {int(identity)}')
        self.store.counts()
        self.assertEqual(len(connects), 2)
        first, second = engine.connect(), engine.connect()
        self.assertEqual(engine.pool.checkedout(), 2)
        second.close()
        first.close()
        self.assertEqual(engine.pool.checkedout(), 0)
