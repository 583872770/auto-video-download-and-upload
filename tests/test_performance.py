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

from Download import build_ydl_options
from tests import test_scale
from tests.test_mysql import MySQLFixture
from webapp.browser_pool import browser_slot
from webapp.clash import ClashManager, parse_subscription
from webapp.downloads import DownloadProgress
from webapp.yt5s_browser import BrowserSession, NativeBrowser


class FakeBrowser:
    def __init__(self):
        self.process = SimpleNamespace(poll=lambda: None)
        self.created = time.monotonic()
        self.uses = 0
        self.closed = threading.Event()

    def close(self):
        self.process = None
        self.closed.set()


class BrowserReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.session = BrowserSession(idle_seconds=0.2)
        self.addCleanup(self.session.close)
        factory = patch('webapp.yt5s_browser.NativeBrowser', FakeBrowser)
        factory.start()
        self.addCleanup(factory.stop)

    def use(self, signature='node-a', capacity=1):
        return self.session.use(self.root, capacity, None, 0, signature)

    def test_reuse_retains_capacity_until_idle_expiry(self):
        with self.use() as first:
            pass
        with self.use() as second:
            self.assertIs(first, second)
            with self.assertRaisesRegex(RuntimeError, '名额超时'):
                with browser_slot(self.root, 1, timeout=0):
                    self.fail('a resident browser must consume capacity')
        self.assertTrue(first.closed.wait(3))
        with browser_slot(self.root, 1, timeout=0):
            pass
        with self.use() as third:
            self.assertIsNot(first, third)

    def test_node_proxy_limit_crash_failure_and_periodic_recycle(self):
        with self.use() as first:
            pass
        with self.use('node-b') as second:
            self.assertIsNot(first, second)
            self.assertTrue(first.closed.is_set())
        with self.use('node-b', 2) as third:
            self.assertIsNot(second, third)
            third.uses = 20
        with self.use('node-b', 2) as fourth:
            self.assertIsNot(third, fourth)
            fourth.created -= 301
        with self.use('node-b', 2) as fifth:
            self.assertIsNot(fourth, fifth)
            fifth.process = SimpleNamespace(poll=lambda: 1)
        with self.assertRaisesRegex(RuntimeError, 'site failure'):
            with self.use('node-b', 2) as sixth:
                self.assertIsNot(fifth, sixth)
                raise RuntimeError('site failure')
        self.assertTrue(sixth.closed.is_set())
        with browser_slot(self.root, 1, timeout=0):
            pass

    def test_expired_timer_cannot_close_a_newly_borrowed_browser(self):
        with patch('webapp.yt5s_browser.threading.Timer') as timer:
            with self.use() as first:
                pass
            stale_expiry = timer.call_args.args[1]
            with self.use() as second:
                stale_expiry()
                self.assertIs(first, second)
                self.assertFalse(second.closed.is_set())


class NativeBrowserTests(unittest.TestCase):
    def test_real_chrome_reconnect_preserves_process_and_closes_task_pages(self):
        from playwright.async_api import async_playwright
        from webapp.yt5s import browser_executable
        try:
            executable = browser_executable()
        except RuntimeError:
            self.skipTest('Chrome/Chromium not installed')
        owner = NativeBrowser()
        self.addCleanup(owner.close)
        native_popen = subprocess.Popen

        def launch(command, **options):
            if '--remote-debugging-address=127.0.0.1' in command:
                command = [*command[:-1], '--headless=new', command[-1]]
            return native_popen(command, **options)

        async def visit():
            address = await owner.start(SimpleNamespace(browser_executable=executable, proxy=''))
            async with async_playwright() as driver:
                browser = await driver.chromium.connect_over_cdp(address)
                context = browser.contexts[0]
                self.assertEqual(len(context.pages), 1)  # Only the initial blank page remains.
                page = await context.new_page()
                await page.set_content('<title>Local reuse fixture</title>')
                self.assertEqual(await page.title(), 'Local reuse fixture')
                await page.close()
            return owner.process.pid

        with patch('webapp.yt5s_browser.subprocess.Popen', side_effect=launch):
            first = asyncio.run(visit())
            self.assertEqual(first, asyncio.run(visit()))
        process = owner.process
        owner.close()
        self.assertIsNotNone(process.poll())


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
        self.assertIn('name="fragment_concurrency"', self.client.get('/').text)


class SQLiteDispatchTests(DispatchContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


class MySQLPerformanceTests(MySQLFixture, DispatchContract, unittest.TestCase):
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
