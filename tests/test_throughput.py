"""Real admission locks, route failure isolation, and resource feedback behavior."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from webapp.proxy_health import ProxyCoolingDown, ProxyHealth, connection_failure
from webapp.throughput import (MIB, PipelineBusy, ThroughputController, _atomic,
                               policy_key, resource_limits, runtime_limits)


class RouteHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.now = 1000
        self.health = ProxyHealth(self.directory, clock=lambda:self.now)

    def test_cross_site_connection_errors_cool_only_the_bad_node(self):
        error = RuntimeError('Page.goto: net::ERR_CONNECTION_CLOSED')
        for _ in range(3):
            self.health.record('bad', error=error)
        self.assertEqual(self.health.select(['bad','good']), 'good')
        with self.assertRaises(ProxyCoolingDown):
            self.health.select(['bad'])
        self.now += 121
        self.assertEqual(self.health.select(['bad']), 'bad')
        self.health.record('bad', transferred_bytes=1000,elapsed=1)
        self.assertTrue(self.health.eligible('bad'))

    def test_video_and_challenge_errors_do_not_punish_routes(self):
        for message in ['Video unavailable', 'HTTP Error 404', '网站验证超时', '磁盘空间不足', 'database is locked']:
            error = RuntimeError(message)
            self.assertFalse(connection_failure(error))
            self.health.record('good',error=error)
        self.assertEqual(self.health.read(),{})

    def test_recovering_route_allows_one_probe_and_success_clears_failures(self):
        for _ in range(3):self.health.record('node',error=RuntimeError('SSLEOFError'))
        self.assertFalse(self.health.eligible('node'))
        self.now+=121
        self.assertEqual(self.health.select(['node']),'node')
        with self.assertRaises(ProxyCoolingDown):self.health.select(['node'])
        self.health.record('node',transferred_bytes=1000,elapsed=1)
        self.assertEqual(self.health.select(['node']),'node')

    def test_parallel_process_updates_are_not_lost(self):
        code = ('from webapp.proxy_health import ProxyHealth; import sys; '
                'h=ProxyHealth(sys.argv[1]); '
                "[h.record('node',error=RuntimeError('SSLEOFError')) for _ in range(5)]")
        processes = [subprocess.Popen([sys.executable,'-c',code,str(self.directory)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(3)]
        for process in processes:
            _, stderr = process.communicate(timeout=25)
            self.assertEqual(process.returncode,0,stderr.decode(errors='replace'))
        self.assertEqual(self.health.read()['node']['failures'],15)


class ResourceFeedbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_pool_snapshot_carries_local_claim_wait_to_instance_diagnostics(self):
        from webapp.concurrency_diagnostics import diagnose
        from webapp.download_start_gate import DownloadStartGate

        config = {'youtube_cookie_pool': ['a']}
        deadline = time.time() + 60
        _atomic(self.directory / 'download-start-gate.json', {
            'policy': policy_key(config), 'next_at': deadline,
            'token': None, 'owner': None, 'expires_at': 0})
        controller = ThroughputController(self.directory)
        sample = {'cpu_cores': 4, 'memory_total': 8192 * MIB,
                  'memory_available': 4096 * MIB, 'cpu_percent': 30, 'io_pressure': 0, 'rx': 0}
        with patch.object(controller, 'sample', return_value=sample):
            controller.update(config, cookie_capacity={
                'known': True, 'capacity': 1, 'reason': 'ready',
                'profiles': [{'id': 'a', 'state': 'ready', 'active': 0, 'limit': 1}]})
        rate = json.loads((self.directory / 'throughput.json').read_text(encoding='utf-8'))
        self.assertEqual(rate['download_start_gate'], {'next_at': deadline})
        value = diagnose(config, 'node', [], rate, ({}, {}, {}), time.time())
        self.assertIn('等待下一次任务启动', value['bottleneck'])
        self.assertEqual(value['cookie_allocation']['accounts'][0]['reasons'][0]['code'],
                         'dispatch_interval')
        with patch.object(controller, 'sample', return_value=sample), \
                patch.object(DownloadStartGate, 'status', return_value={}):
            cleared = controller.update(config, force=True)
        self.assertEqual(cleared['download_start_gate'], {})

    def test_container_quota_and_memory_override_host_totals(self):
        (self.directory/'cpu.max').write_text('50000 100000')
        (self.directory/'memory.max').write_text(str(768*MIB))
        (self.directory/'memory.current').write_text(str(512*MIB))
        with patch('webapp.throughput.psutil.cpu_count',return_value=32), \
             patch('webapp.throughput.psutil.Process',return_value=SimpleNamespace(cpu_affinity=lambda:list(range(16)))), \
             patch('webapp.throughput.psutil.virtual_memory',return_value=SimpleNamespace(total=16384*MIB,available=8192*MIB)):
            result = resource_limits(self.directory)
        self.assertEqual(result['cpu_cores'],.5)
        self.assertEqual(result['memory_total'],768*MIB)
        self.assertEqual(result['memory_available'],256*MIB)


if __name__=='__main__':
    unittest.main()
