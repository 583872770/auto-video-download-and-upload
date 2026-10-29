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
                               policy_key, resource_limits, runtime_limits, transfer_slot)


class RouteHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.now = 1000
        self.health = ProxyHealth(self.directory, clock=lambda:self.now)

    def test_cross_site_connection_errors_cool_only_the_bad_node(self):
        error = RuntimeError('Page.goto: net::ERR_CONNECTION_CLOSED')
        for provider in ['bergung', 'yt5s', 'yt1d']:
            self.health.record('bad', provider, error=error)
        self.assertEqual(self.health.select(['bad','good']), 'good')
        with self.assertRaises(ProxyCoolingDown):
            self.health.select(['bad'])
        self.now += 121
        self.assertEqual(self.health.select(['bad']), 'bad')
        self.health.record('bad','yt5s', transferred_bytes=1000,elapsed=1)
        self.assertTrue(self.health.eligible('bad','yt5s'))

    def test_video_and_challenge_errors_do_not_punish_routes(self):
        for message in ['Video unavailable', 'HTTP Error 404', '网站验证超时', '磁盘空间不足', 'database is locked']:
            error = RuntimeError(message)
            self.assertFalse(connection_failure(error))
            self.health.record('good','yt5s',error=error)
        self.assertEqual(self.health.read(),{})

    def test_one_provider_failure_does_not_disable_another_provider(self):
        for _ in range(3):
            self.health.record('node','yt1d',error=RuntimeError('SSLEOFError'))
        self.assertFalse(self.health.eligible('node','yt1d'))
        self.assertTrue(self.health.eligible('node','yt5s'))
        self.assertEqual(self.health.select(['node']),'node')

    def test_parallel_process_updates_are_not_lost(self):
        code = ('from webapp.proxy_health import ProxyHealth; import sys; '
                'h=ProxyHealth(sys.argv[1]); '
                "[h.record('node','yt5s',error=RuntimeError('SSLEOFError')) for _ in range(5)]")
        processes = [subprocess.Popen([sys.executable,'-c',code,str(self.directory)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(3)]
        for process in processes:
            _, stderr = process.communicate(timeout=25)
            self.assertEqual(process.returncode,0,stderr.decode(errors='replace'))
        self.assertEqual(self.health.read()['node']['providers']['yt5s']['failures'],15)


class TransferAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = {'throughput_mode':'auto','worker_count':1,'yt5s_browser_count':1}
        _atomic(self.directory/'throughput.json',{'policy':policy_key(self.config),'time':time.time(),
                  'transfers':1,'workers':3,'browsers':1})

    def test_waiter_cannot_transfer_until_the_owner_releases(self):
        waiting, entered = threading.Event(), threading.Event()
        def second():
            with transfer_slot(self.directory,self.config,on_wait=waiting.set,timeout=5):
                entered.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transfer_slot(self.directory,self.config):
                future = pool.submit(second)
                self.assertTrue(waiting.wait(3))
                self.assertFalse(entered.is_set())
            future.result(timeout=4)
        self.assertTrue(entered.is_set())

    def test_backpressure_does_not_oversubscribe(self):
        with transfer_slot(self.directory,self.config):
            with self.assertRaises(PipelineBusy):
                with transfer_slot(self.directory,self.config,timeout=0):
                    self.fail('occupied transfer slot admitted another owner')

    def test_killed_worker_releases_os_admission(self):
        code = ('from webapp.throughput import transfer_slot; import json,sys,time; '
                'ctx=transfer_slot(sys.argv[1],json.loads(sys.argv[2])); '
                'ctx.__enter__(); print("ready",flush=True); time.sleep(20)')
        process = subprocess.Popen([sys.executable,'-c',code,str(self.directory),json.dumps(self.config)],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'ready')
            with self.assertRaises(PipelineBusy):
                with transfer_slot(self.directory,self.config,timeout=0):
                    self.fail('live child lost its slot')
        finally:
            process.kill()
            process.communicate(timeout=5)
        with transfer_slot(self.directory,self.config,timeout=0):
            pass


class ResourceFeedbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

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

    def test_controller_grows_only_busy_transfers_and_retreats_on_pressure(self):
        config={'throughput_mode':'auto','worker_count':16,'yt5s_browser_count':2,'download_methods':['yt5s']}
        clock=[100]
        controller=ThroughputController(self.directory,clock=lambda:clock[0])
        sample={'cpu_cores':4,'memory_total':8192*MIB,'memory_available':4096*MIB,'cpu_percent':30,'io_pressure':0,'rx':0}
        with patch.object(controller,'sample',side_effect=lambda:dict(sample)):
            initial=controller.update(config)
            self.assertEqual(initial['transfers'],8)
            self.assertEqual(initial['workers'],6)  # one unverified upstream + bounded preparation
            clock[0]+=61; sample['rx']+=610_000_000
            idle=controller.update(config,['waiting_browser']*12)
            self.assertEqual(idle['transfers'],8)
            clock[0]+=61; sample['rx']+=610_000_000
            grown=controller.update(config,['downloading']*8)
            self.assertEqual(grown['transfers'],9)
            clock[0]+=61; sample['rx']+=610_000_000; sample['cpu_percent']=97
            reduced=controller.update(config,['downloading']*9)
            self.assertLess(reduced['transfers'],9)
            self.assertEqual(runtime_limits(config,self.directory)['transfers'],reduced['transfers'])

    def test_stale_or_changed_policy_never_reuses_large_old_pool(self):
        config={'throughput_mode':'auto','worker_count':64,'yt5s_browser_count':2}
        _atomic(self.directory/'throughput.json',{'time':time.time()-21,'policy':policy_key(config),
                    'transfers':64,'workers':80,'browsers':2})
        self.assertEqual(runtime_limits(config,self.directory)['workers'],2)
        self.assertEqual(runtime_limits({'worker_count':64},self.directory)['workers'],64)

    def test_upstream_probe_reverts_when_more_connections_produce_no_gain(self):
        config = {'throughput_mode':'auto','worker_count':16,'yt5s_browser_count':2,'download_methods':['bergung']}
        clock = [100]
        controller = ThroughputController(self.directory, clock=lambda:clock[0])
        sample = {'cpu_cores':4,'memory_total':8192*MIB,'memory_available':4096*MIB,
                  'cpu_percent':30,'io_pressure':0,'rx':0}
        counters = {'worker':{'received_bytes':0,'verified_bytes':0,
                             'groups':{'unverified-media':{'received_bytes':0}}}}
        def advance():
            clock[0] += 61
            counters['worker']['received_bytes'] += 61_000_000
            counters['worker']['groups']['unverified-media']['received_bytes'] += 61_000_000
            # Return a fresh object, as the filesystem reader does.
            return json.loads(json.dumps(counters))
        with patch.object(controller,'sample',side_effect=lambda:dict(sample)), \
             patch('webapp.download_metrics.read_counters',side_effect=lambda *_:json.loads(json.dumps(counters))):
            initial = controller.update(config)
            self.assertEqual(initial['groups']['unverified-media']['limit'],2)
            advance()
            grown = controller.update(config,[{'stage':'downloading','upstream_group':'unverified-media'}]*2)
            self.assertEqual(grown['groups']['unverified-media']['limit'],3)
            advance()
            reverted = controller.update(config,[{'stage':'downloading','upstream_group':'unverified-media'}]*3)
            self.assertEqual(reverted['groups']['unverified-media']['limit'],2)
            self.assertEqual(reverted['media_receive_mbps'],8)
            self.assertEqual(reverted['receive_mbps'],0)

    def test_browser_probe_reverts_and_worker_budget_counts_per_process_pools(self):
        config={'throughput_mode':'auto','worker_count':16,'yt5s_browser_count':4,'download_methods':['bergung']}
        clock=[100]
        controller=ThroughputController(self.directory,clock=lambda:clock[0])
        sample={'cpu_cores':4,'memory_total':8192*MIB,'memory_available':4096*MIB,'cpu_percent':30,'io_pressure':0,'rx':0}
        with patch.object(controller,'sample',side_effect=lambda:dict(sample)), \
             patch.dict(os.environ,{'VIDEO_WORKER_DB_CONNECTION_BUDGET':'12','VIDEO_WORKER_DB_POOL_SIZE':'2','VIDEO_WORKER_DB_MAX_OVERFLOW':'0'}):
            initial=controller.update(config)
            self.assertEqual(initial['browsers'],2)
            self.assertLessEqual(initial['workers'],6)
            clock[0]+=61
            increased=controller.update(config,['waiting_browser']*4)
            self.assertEqual(increased['browsers'],3)
            clock[0]+=121
            reverted=controller.update(config,['waiting_browser']*4)
            self.assertEqual(reverted['browsers'],2)
            self.assertEqual(runtime_limits(config,self.directory)['workers'],reverted['workers'])

    def test_video_failures_do_not_reduce_shared_bandwidth_capacity(self):
        config={'throughput_mode':'auto','worker_count':16,'yt5s_browser_count':2,'download_methods':['bergung']}
        clock=[100]
        controller=ThroughputController(self.directory,clock=lambda:clock[0])
        sample={'cpu_cores':4,'memory_total':8192*MIB,'memory_available':4096*MIB,'cpu_percent':30,'io_pressure':0,'rx':0}
        counters={'worker':{'groups':{'unverified-media':{'failed':0,'upstream_failed':0}}}}
        with patch.object(controller,'sample',side_effect=lambda:dict(sample)), \
             patch('webapp.download_metrics.read_counters',side_effect=lambda *_:json.loads(json.dumps(counters))):
            controller.update(config)
            clock[0]+=61;counters['worker']['groups']['unverified-media']['failed']=10
            content=controller.update(config)
            self.assertEqual(content['groups']['unverified-media']['limit'],2)
            clock[0]+=61;counters['worker']['groups']['unverified-media']['upstream_failed']=3
            throttled=controller.update(config)
            self.assertEqual(throttled['groups']['unverified-media']['limit'],1)


if __name__=='__main__':
    unittest.main()
