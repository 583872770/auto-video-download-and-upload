"""Configured reservations, hot sizing and explicit deployment ceilings."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp.db_pool_calculator import connection_budget, target_workers
from webapp.throughput import MIB, ThroughputController, runtime_limits
from webapp.concurrency_policy import fragment_limit, pool_process_limits


class DatabaseBudgetTests(unittest.TestCase):
    def test_fragments_stay_independent_of_task_count_and_reserve_buffer_memory(self):
        config = {'download_task_limit': 0, 'youtube_cookie_pool': list(map(str,range(31))),
                  'fragment_concurrency': 8}
        self.assertEqual(fragment_limit(config), 8)
        capacity = pool_process_limits(config,31,16*1024**3,connection_budget(config))
        self.assertEqual(capacity['workers'],31)
        self.assertEqual(capacity['memory_per_worker_mb'],240)
        larger = pool_process_limits({**config,'fragment_concurrency':16},31,4*1024**3,connection_budget(config))
        smaller = pool_process_limits({**config,'fragment_concurrency':1},31,4*1024**3,connection_budget(config))
        self.assertLess(larger['workers'],smaller['workers'])

    def setUp(self):
        clean = patch.dict(os.environ, {}, clear=True)
        clean.start()
        self.addCleanup(clean.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = dict(youtube_cookie_pool=[str(i) for i in range(21)], download_task_limit=21)
        self.sample = dict(cpu_cores=4, memory_total=16384*MIB, memory_available=10000*MIB,
                           cpu_percent=30, io_pressure=0, rx=0)

    def test_auto_reserves_for_target_and_processing_workers(self):
        budget = connection_budget(self.config)
        self.assertEqual((budget['target_workers'], budget['required_total'], budget['total'],
                          budget['workers'], budget['target_supported']), (21, 57, 57, 21, True))
        self.assertEqual(connection_budget()['workers'], 16)

    def test_manual_and_deployment_limits_are_independent_and_visible(self):
        with patch.dict(os.environ, {'VIDEO_DB_CONNECTION_BUDGET': '48'}):
            budget = connection_budget(self.config)
            self.assertEqual((budget['requested_total'], budget['total'], budget['deployment_cap'],
                              budget['workers']), (57, 48, 48, 16))
            self.assertFalse(budget['target_supported'])
        with patch.dict(os.environ, {'VIDEO_WORKER_DB_CONNECTION_BUDGET': '32'}):
            budget = connection_budget(self.config)
            self.assertEqual((budget['deployment_worker_cap'], budget['workers']), (32, 16))
        with patch.dict(os.environ, {'VIDEO_DB_CONNECTION_BUDGET': '8'}), self.assertRaises(ValueError):
            connection_budget(self.config)

    def test_unlimited_pool_reserves_selected_session_demand_and_keeps_deployment_cap(self):
        config = dict(self.config, download_task_limit=0,
                      youtube_cookie_session_limit=2)
        self.assertEqual(target_workers(config), 42)
        budget = connection_budget(config)
        self.assertEqual((budget['target_workers'], budget['total'], budget['workers']), (42, 99, 42))
        with patch.dict(os.environ, {'VIDEO_WORKER_DB_CONNECTION_BUDGET': '48'}):
            limited = connection_budget(config)
            self.assertEqual(limited['workers'], 24)
            self.assertFalse(limited['target_supported'])
        self.assertEqual(target_workers(dict(config, youtube_cookie_session_limit=64)), 64)

    def test_zero_worker_limit_survives_live_sizing_and_hot_shrink(self):
        config = dict(self.config, download_task_limit=0,
                      youtube_cookie_pool=['fixture'] * 31)
        controller = ThroughputController(self.root)
        capacity = dict(known=True, capacity=31, reason='ready', profiles=[])
        with patch.object(controller, 'sample', return_value=self.sample):
            unlimited = controller.update(config, cookie_capacity=capacity, force=True)
            self.assertEqual(unlimited['workers'], 31)
            self.assertEqual(unlimited['worker_control']['configured_limit'], 0)
            self.assertIsNone(unlimited['worker_control']['bounds']['configured'])
            self.assertNotIn('configured', unlimited['worker_control']['limit_reasons'])
            self.assertEqual(runtime_limits(config, self.root)['workers'], 31)
            shrunk = controller.update(dict(config, download_task_limit=7),
                                       cookie_capacity=capacity, force=True)
            self.assertEqual(shrunk['workers'], 7)

    def test_custom_pools_change_the_formula_without_resizing_pools(self):
        with patch.dict(os.environ, {'VIDEO_DB_POOL_SIZE': '3', 'VIDEO_DB_MAX_OVERFLOW': '1',
                                    'VIDEO_WORKER_DB_POOL_SIZE': '3', 'VIDEO_WORKER_DB_MAX_OVERFLOW': '1'}):
            budget = connection_budget(self.config)
            self.assertEqual((budget['main'], budget['auxiliary'], budget['per_worker'],
                              budget['total'], budget['workers']), ((3, 1), 8, 4, 104, 21))


if __name__ == '__main__':
    unittest.main()
