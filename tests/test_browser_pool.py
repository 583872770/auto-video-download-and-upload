"""Real process contention, dynamic browser limits and isolated resolution."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_scale
from tests.test_mysql import MySQLFixture
from webapp.browser_pool import browser_slot
from webapp.yt5s import resolve


def occupy_browser(directory, capacity, messages, release, active, peak):
    messages.put(('ready', None))
    with browser_slot(directory, capacity, lambda: messages.put(('waiting', None)), timeout=20) as slot:
        with active.get_lock():
            active.value += 1
            peak.value = max(peak.value, active.value)
        messages.put(('entered', (os.getpid(), slot)))
        try:
            deadline = time.monotonic() + 20
            while not Path(release).is_file():
                if time.monotonic() >= deadline:
                    raise RuntimeError('fixture browser was not released')
                time.sleep(0.02)
        finally:
            with active.get_lock():
                active.value -= 1


class BrowserPoolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def processes(self, count, capacity):
        context = multiprocessing.get_context('spawn')
        messages, release = context.Queue(), self.root / 'release'
        active, peak = context.Value('i', 0), context.Value('i', 0)
        children = [context.Process(target=occupy_browser, args=(str(self.root), capacity, messages, release, active, peak))
                    for _ in range(count)]

        def cleanup():
            release.touch()
            for child in children:
                child.join(timeout=5)
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            messages.close()
            messages.join_thread()

        self.addCleanup(cleanup)
        for child in children:
            child.start()
        return children, messages, release, peak

    def test_ten_processes_use_two_browser_slots_and_all_eventually_finish(self):
        children, messages, release, peak = self.processes(10, 2)
        ready, entered, waiting = 0, [], 0
        while ready < 10 or len(entered) < 2 or waiting < 8:
            kind, slot = messages.get(timeout=25)
            ready += kind == 'ready'
            waiting += kind == 'waiting'
            if kind == 'entered':
                entered.append(slot[1])
                self.assertLessEqual(len(entered), 2)
        self.assertEqual(len(set(entered)), 2)
        with self.assertRaises(Empty):
            messages.get(timeout=0.2)
        release.touch()
        for child in children:
            child.join(timeout=20)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(peak.value, 2)

    def test_killed_worker_releases_capacity_for_waiting_process(self):
        children, messages, release, peak = self.processes(2, 1)
        waiting, owner = False, None
        while not (waiting and owner):
            kind, value = messages.get(timeout=25)
            waiting |= kind == 'waiting'
            if kind == 'entered':
                owner = value[0]
        holder = next(child for child in children if child.pid == owner)
        survivor = next(child for child in children if child.pid != owner)
        holder.terminate()
        holder.join(timeout=5)
        kind, value = messages.get(timeout=8)
        self.assertEqual((kind, value[0]), ('entered', survivor.pid))
        release.touch()
        survivor.join(timeout=10)
        self.assertEqual(survivor.exitcode, 0)

    def test_dynamic_limit_increases_and_lowering_waits_for_high_slots(self):
        limit = [1]
        waiting = threading.Event()
        acquired = threading.Event()
        release = threading.Event()

        def acquire():
            with browser_slot(self.root, lambda: limit[0], waiting.set, timeout=5):
                acquired.set()
                release.wait(5)

        with ThreadPoolExecutor(1) as pool:
            with browser_slot(self.root, 2, preferred=12):
                future = pool.submit(acquire)
                self.assertTrue(waiting.wait(3))
                self.assertFalse(acquired.is_set())
                limit[0] = 2
                self.assertTrue(acquired.wait(3))
                limit[0] = 1
                release.set()
                future.result(timeout=3)
                waiting.clear()
                acquired.clear()
                future = pool.submit(acquire)
                self.assertTrue(waiting.wait(3))
                self.assertFalse(acquired.is_set())
            self.assertTrue(acquired.wait(3))
            future.result(timeout=3)

    def test_exception_releases_slot_and_timeout_never_starts_browser(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with browser_slot(self.root, 1):
                raise RuntimeError('fixture')
        with browser_slot(self.root, 1):
            with self.assertRaisesRegex(RuntimeError, '名额超时'):
                with browser_slot(self.root, 1, timeout=0):
                    self.fail('capacity exceeded')

    def test_resolvers_run_concurrently_with_separate_profiles_proxies_and_diagnostics(self):
        arrived = threading.Barrier(2)
        seen = []

        async def probe(args, directory, report, **kwargs):
            seen.append((args.proxy, directory))
            await asyncio.to_thread(arrived.wait, 5)
            report['stage'] = 'opening_site'
            (directory / 'page.png').write_bytes(args.proxy.encode())
            raise RuntimeError('fixture website failure')

        def run(index):
            with self.assertRaisesRegex(RuntimeError, 'fixture website failure'):
                resolve('https://www.youtube.com/watch?v=npNnbj46_ik',
                    {'proxy_mode': 'manual', 'proxy': f'http://127.0.0.1:{18000 + index}', 'yt5s_browser_count': 2},
                    self.root, self.root, worker_slot=index)

        with patch('webapp.yt5s.require_browser_environment', return_value='/fixture/chromium'), \
                patch('webapp.yt5s.probe', side_effect=probe), patch('builtins.print'), ThreadPoolExecutor(2) as pool:
            list(pool.map(run, range(2)))
        self.assertEqual(len({path for _, path in seen}), 2)
        for index in range(2):
            directory = self.root / 'yt5s' / f'worker-{index}'
            self.assertEqual((directory / 'last-failure.png').read_text(), f'http://127.0.0.1:{18000 + index}')
            report = json.loads((directory / 'last-failure.json').read_text(encoding='utf-8'))
            self.assertEqual(report['worker_slot'], index)
            self.assertEqual(list(directory.glob('resolve-*')), [])


class BrowserSettingsTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_browser_limit_defaults_validation_and_live_queue_configuration(self):
        self.assertEqual(self.client.get('/api/config').json['config']['yt5s_browser_count'], 2)
        task = self.store.create('download', {'config': {'download_method': 'yt5s'},
            'base': str(self.root), 'urls': ['https://www.youtube.com/watch?v=npNnbj46_ik']})
        for value in (0, 17, True, 2.5, 'bad'):
            before = self.config.read_bytes()
            result = self.client.put('/api/config', json={'yt5s_browser_count': value}, headers=self.headers)
            self.assertEqual(result.status_code, 400, result.json)
            self.assertEqual(self.config.read_bytes(), before)
        result = self.client.put('/api/config', json={'yt5s_browser_count': '3', 'worker_count': '10'}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.store.runtime()['yt5s_browser_count'], 3)
        self.assertEqual(self.client.get('/api/status').json['yt5s_browser_count'], 3)
        self.assertEqual(self.client.get('/api/config').json['config']['yt5s_browser_count'], 3)
        self.assertNotIn('yt5s_browser_count', self.store.get(task['id'], private=True)['payload']['config'])


class MySQLBrowserSettingsTests(MySQLFixture, BrowserSettingsTests):
    pass
