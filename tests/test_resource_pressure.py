"""Scoped PSI must not shrink workers because an unrelated cgroup is busy."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp.concurrency_diagnostics import diagnose
from webapp.resource_pressure import sample_io_pressure
from webapp.throughput import MIB, ThroughputController


class ScopedPressureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.group, self.proc = self.root / 'cgroup', self.root / 'proc'
        self.group.mkdir()
        (self.proc / 'self').mkdir(parents=True)
        (self.proc / 'pressure').mkdir()
        (self.proc / 'self/cgroup').write_text('0::/\n')
        self.write_pressure(self.proc / 'pressure/io', 50)
        self.write_pressure(self.group / 'io.pressure', 9.5)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @staticmethod
    def write_pressure(path, value):
        path.write_text(f'some avg10={value} avg60=0 avg300=0 total=100\n'
                        'full avg10=1 avg60=0 avg300=0 total=20\n')

    def sample(self):
        return sample_io_pressure(self.group, self.proc)

    def test_nested_current_group_wins_over_busy_host_and_parent(self):
        (self.group / 'services/downloader').mkdir(parents=True)
        (self.proc / 'self/cgroup').write_text('0::/services/downloader\n')
        self.write_pressure(self.group / 'io.pressure', 75)
        self.write_pressure(self.group / 'services/downloader/io.pressure', 0)
        self.assertEqual(self.sample(), {'io_pressure': 0, 'io_pressure_source': 'cgroup',
                                         'io_pressure_host': 50})

    def test_namespace_mount_can_identify_current_group_by_process_membership(self):
        (self.proc / 'self/cgroup').write_text('0::/../../docker/not-visible\n')
        (self.group / 'cgroup.procs').write_text(str(os.getpid()) + '\n')
        self.assertEqual(self.sample()['io_pressure'], 9.5)
        self.assertEqual(self.sample()['io_pressure_source'], 'cgroup')

    def test_missing_or_invalid_scoped_sample_explicitly_falls_back_without_parent(self):
        (self.group / 'services/downloader').mkdir(parents=True)
        (self.proc / 'self/cgroup').write_text('0::/services/downloader\n')
        for content in ('', 'some avg10=nan', 'some avg10=101', 'full avg10=4'):
            with self.subTest(content=content):
                (self.group / 'services/downloader/io.pressure').write_text(content)
                sample = self.sample()
                self.assertEqual(sample['io_pressure'], 50)
                self.assertEqual(sample['io_pressure_source'], 'host')
        (self.proc / 'pressure/io').unlink()
        self.assertIsNone(self.sample()['io_pressure'])
        self.assertEqual(self.sample()['io_pressure_source'], 'unavailable')

    def controller(self):
        config = {'youtube_cookie_pool': [str(i) for i in range(18)],
                  'builtin_transfer_limit': 12, 'download_connection_limit': 48,
                  'download_methods': ['yt_dlp'], 'fragment_concurrency': 8}
        tick = [100]
        controller = ThroughputController(self.root / 'state', clock=lambda: tick[0])
        resources = {'cpu_cores': 4, 'memory_total': 16384 * MIB,
                     'memory_available': 11000 * MIB, 'cpu_percent': 30, 'rx': 0}
        sampling = patch.object(controller, 'sample', side_effect=lambda: {**resources, **self.sample()})
        sampling.start()
        self.addCleanup(sampling.stop)
        def update(seconds=0):
            tick[0] += seconds
            return controller.update(config, cookie_capacity={'known': True, 'capacity': 18,
                'reason': 'ready', 'profiles': [{'id': str(i), 'state': 'ready', 'active': 0, 'limit': 1}
                                               for i in range(18)]})
        return controller, resources, config, update

    def test_host_pressure_does_not_shrink_local_workers_and_real_pressure_recovers_slowly(self):
        controller, resources, config, update = self.controller()
        initial = update()
        self.assertEqual(initial['workers'], 16)
        for _ in range(4):
            healthy = update(61)
            self.assertEqual(healthy['workers'], 16)
            self.assertFalse(healthy['worker_control']['pressure']['active'])
        self.write_pressure(self.group / 'io.pressure', 22.9)
        self.assertEqual(update(5)['workers'], 16)
        self.assertEqual(update(5)['workers'], 16)
        reduced = update(51)
        self.assertEqual(reduced['workers'], 12)
        control = reduced['worker_control']
        self.assertEqual(control['last_reduction']['from'], 16)
        self.assertEqual(control['last_reduction']['to'], 12)
        self.assertEqual(control['last_reduction']['reasons'],
            [{'code': 'io', 'value': 22.9, 'threshold': 15, 'source': 'cgroup'}])
        self.assertIn('io', control['recovery']['blocked_by'])
        self.assertAlmostEqual(control['recovery']['eligible_at'] - reduced['time'], 60, places=2)
        self.write_pressure(self.group / 'io.pressure', 9.5)
        held = update(5)
        self.assertEqual(held['workers'], 12)
        self.assertEqual(held['worker_control']['recovery']['blocked_by'], [])
        self.assertEqual(held['worker_control']['last_reduction'], control['last_reduction'])
        recovered = update(55)
        self.assertEqual(recovered['workers'], 13)
        saved = json.loads((controller.directory / 'throughput.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['worker_control'], recovered['worker_control'])

    def test_critical_cpu_and_low_memory_protection_and_recovery_conditions_remain(self):
        _, resources, _, update = self.controller()
        update()
        resources['cpu_percent'] = 97
        reduced = update(61)
        self.assertEqual(reduced['worker_control']['last_reduction']['reasons'][0]['code'], 'cpu')
        resources['cpu_percent'] = 80
        self.assertEqual(update(61)['workers'], reduced['workers'])
        resources['cpu_percent'] = 30
        resources['memory_available'] = 1500 * MIB  # Above emergency waterline, below recovery waterline.
        held = update(61)
        self.assertEqual(held['workers'], reduced['workers'])
        self.assertIn('memory', held['worker_control']['recovery']['blocked_by'])
        resources['memory_available'] = 100 * MIB
        emergency = update(61)
        self.assertLess(emergency['workers'], reduced['workers'])
        self.assertIn('memory', [r['code'] for r in emergency['worker_control']['last_reduction']['reasons']])

    def test_scope_change_does_not_combine_unrelated_high_samples(self):
        _, _, _, update = self.controller()
        self.write_pressure(self.group / 'io.pressure', 22.9)
        update()
        update(5)
        (self.group / 'io.pressure').unlink()
        fallback = update(61)
        self.assertEqual(fallback['workers'], 16)
        self.assertEqual(fallback['worker_control']['pressure']['high_samples'], 1)

    def test_missing_io_is_unknown_and_cpu_protection_still_works(self):
        (self.group / 'io.pressure').unlink()
        (self.proc / 'pressure/io').unlink()
        _, resources, _, update = self.controller()
        initial = update()
        self.assertIsNone(initial['worker_control']['measurements']['io_pressure'])
        self.assertEqual(initial['worker_control']['measurements']['io_pressure_source'], 'unavailable')
        resources['cpu_percent'] = 97
        reduced = update(61)
        self.assertLess(reduced['workers'], initial['workers'])
        self.assertEqual(reduced['worker_control']['last_reduction']['reasons'][0]['code'], 'cpu')

    def test_diagnostic_reports_real_occupancy_and_drops_stale_or_offline_protection(self):
        _, resources, config, update = self.controller()
        update()
        resources['cpu_percent'] = 97
        rate = update(61)
        progress = [{'stage': stage} for stage in ['downloading'] * 5 + ['waiting_request'] * 4 + ['processing'] * 3]
        context = ({}, {}, {}, {})
        def view(stamp, online=True):
            return diagnose(config, 'node', progress, rate, context, stamp, online=online)
        current = view(rate['time'])
        self.assertEqual(current['worker_control']['used'], 12)
        self.assertEqual(current['worker_control']['stages'], {'transferring': 5, 'preparing': 4, 'processing': 3})
        reason = current['cookie_allocation']['accounts'][0]['reasons'][0]
        self.assertEqual(reason['code'], 'worker_limit')
        self.assertIn('12 / 12', reason['label'])
        self.assertIn('资源保护', reason['label'])
        self.assertIn('资源保护', current['bottleneck'])
        self.assertNotIn('设置中调整', current['bottleneck'])
        busy = diagnose(config, 'node', progress, rate, context, rate['time'],
            current_tasks=[{'id': 'active', 'progress': {'stage': 'downloading', 'cookie_profile_id': '0'}}])
        self.assertFalse(any(r['code'] == 'worker_limit'
                             for r in busy['cookie_allocation']['accounts'][0]['reasons']))
        for stale in (view(rate['time'] + 21), view(rate['time'], online=False)):
            self.assertIsNone(stale['worker_control'])
            self.assertFalse(any(r['code'] == 'worker_limit' for a in stale['cookie_allocation']['accounts']
                                 for r in a['reasons']))


if __name__ == '__main__':
    unittest.main()
