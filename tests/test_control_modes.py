"""Independent control modes and session-aware resource feedback."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from webapp.cookie_pool import capacity_summary
from webapp.concurrency_diagnostics import diagnose
from webapp.settings import validate_settings
from webapp.throughput import MIB, ThroughputController, automatic, policy_key, runtime_limits


class ControlModeTests(unittest.TestCase):
    def test_routing_and_concurrency_can_be_chosen_independently(self):
        for routing in ('auto', 'balanced', 'ordered_fallback'):
            for mode in ('manual', 'auto'):
                config = {'scheduler_mode': routing, 'throughput_mode': mode}
                validate_settings(config)
                self.assertEqual(config['throughput_mode'], mode)
                self.assertEqual(automatic(config), mode == 'auto')

    def test_pool_aliases_cooling_and_halt_have_distinct_capacity(self):
        snapshot = {'configured_files': 4, 'next_at': 110, 'accounts': [
            {'id': 'a', 'limit': 2, 'active': 1, 'next_at': 115},
            {'id': 'b', 'shared_with': 'a', 'limit': 2, 'active': 1},
            {'id': 'c', 'limit': 1, 'cooldown_until': 200},
            {'id': 'd', 'unavailable': True}]}
        result = capacity_summary(snapshot, 100)
        self.assertEqual((result['effective_sessions'], result['capacity'], result['active']), (2, 2, 1))
        self.assertEqual(result['next_start_at'], 115)
        self.assertEqual(result['unavailable_files'], 1)
        self.assertFalse(result['identity_verified'])
        result = capacity_summary({**snapshot, 'halted': True}, 100)
        self.assertEqual(result['capacity'], 0)
        self.assertIsNone(result['next_start_at'])

    def test_auto_pool_does_not_spawn_preparation_workers_beyond_sessions(self):
        config = {'throughput_mode': 'auto', 'worker_count': 16, 'download_methods': ['yt_dlp'],
                  'youtube_cookie_pool': ['a' * 32]}
        sample = dict(cpu_cores=16, memory_total=8192*MIB, memory_available=4096*MIB,
                      cpu_percent=20, io_pressure=0, rx=0)
        with tempfile.TemporaryDirectory() as root:
            controller = ThroughputController(root)
            with patch.object(controller, 'sample', return_value=sample):
                for capacity in (1, 2, 0):
                    state = controller.update(config, force=True, cookie_capacity={
                        'known': True, 'capacity': capacity, 'reason': 'session cap'})
                    self.assertLessEqual(state['workers'], max(1, capacity))
                    self.assertLessEqual(state['transfers'], max(1, capacity))
                    self.assertEqual(state['cookie_pool']['capacity'], capacity)
            # The policy includes pool membership, so removing the pool cannot
            # reuse a controller snapshot with the old admission assumptions.
            self.assertNotEqual(policy_key(config), policy_key({**config, 'youtube_cookie_pool': []}))
            Path(root, 'throughput.json').unlink()
            self.assertEqual(runtime_limits(config, root)['workers'], 1)

    def test_custom_mode_keeps_fixed_workers_but_reports_fresh_session_limits(self):
        config = {'throughput_mode': 'manual', 'worker_count': 16, 'download_methods': ['yt_dlp'],
                  'youtube_cookie_pool': ['a' * 32]}
        sample = dict(cpu_cores=4, memory_total=8192*MIB, memory_available=4096*MIB,
                      cpu_percent=20, io_pressure=0, rx=0)
        with tempfile.TemporaryDirectory() as root:
            controller = ThroughputController(root)
            with patch.object(controller, 'sample', return_value=sample):
                controller.update(config, cookie_capacity={'known': True, 'capacity': 1, 'reason': 'session cap'})
            rate = runtime_limits(config, root)
            self.assertEqual(rate['workers'], 16)
            self.assertEqual(rate['cookie_pool']['capacity'], 1)
            value = diagnose(config, 'node', [], rate, ({}, {}, {}), time.time())
            self.assertEqual(value['effective_limit'], 1)

    def test_unrelated_nic_traffic_never_grows_media_concurrency(self):
        config = {'throughput_mode': 'auto', 'worker_count': 16, 'download_methods': ['yt_dlp']}
        tick = [100]
        sample = dict(cpu_cores=4, memory_total=8192*MIB, memory_available=4096*MIB,
                      cpu_percent=20, io_pressure=0, rx=0)
        with tempfile.TemporaryDirectory() as root:
            controller = ThroughputController(root, clock=lambda: tick[0])
            with patch.object(controller, 'sample', side_effect=lambda: dict(sample)), \
                 patch('webapp.download_metrics.read_counters', return_value={}):
                initial = controller.update(config)
                tick[0] += 61
                sample['rx'] += 6_100_000_000
                after = controller.update(config, ['downloading'] * initial['transfers'])
            self.assertEqual(after['transfers'], initial['transfers'])
            self.assertGreater(after['receive_mbps'], 0)
            self.assertEqual(after['media_receive_mbps'], 0)

    def test_diagnostics_respect_pool_capacity_and_expired_samples(self):
        config = {'throughput_mode': 'auto', 'worker_count': 16, 'download_methods': ['yt_dlp'],
                  'youtube_cookie_pool': ['a' * 32]}
        stamp = time.time()
        rate = {'policy': policy_key(config), 'time': stamp, 'transfers': 8,
                'cookie_pool': {'known': True, 'capacity': 1, 'effective_sessions': 1, 'reason': 'warming'}}
        value = diagnose(config, 'node', [], rate, ({}, {}, {}), stamp)
        self.assertEqual(value['effective_limit'], 1)
        self.assertEqual(value['bottleneck'], 'warming')
        value = diagnose(config, 'node', [], rate, ({}, {}, {}), stamp + 21)
        self.assertEqual(value['effective_limit'], 0)
        self.assertFalse(value['cookie_pool']['known'])


if __name__ == '__main__':
    unittest.main()
