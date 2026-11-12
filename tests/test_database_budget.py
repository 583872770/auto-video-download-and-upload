"""Configured reservations, hot sizing and explicit deployment ceilings."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp.db_pool_calculator import connection_budget, target_workers
from webapp.throughput import MIB, ThroughputController, runtime_limits


class DatabaseBudgetTests(unittest.TestCase):
    def setUp(self):
        clean = patch.dict(os.environ, {}, clear=True)
        clean.start()
        self.addCleanup(clean.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = dict(download_methods=['yt_dlp'], youtube_cookie_pool=['a'] * 21,
                           youtube_cookie_pool_worker_limit=21, builtin_transfer_limit=21,
                           download_connection_limit=32)
        self.sample = dict(cpu_cores=4, memory_total=16384*MIB, memory_available=10000*MIB,
                           cpu_percent=30, io_pressure=0, rx=0)

    def test_auto_reserves_for_target_and_legacy_callers_keep_their_contract(self):
        budget = connection_budget(self.config)
        self.assertEqual((budget['target_workers'], budget['required_total'], budget['total'],
                          budget['workers'], budget['target_supported']), (21, 53, 53, 21, True))
        self.assertEqual(connection_budget()['workers'], 18)

    def test_manual_and_deployment_limits_are_independent_and_visible(self):
        manual = dict(self.config, db_budget_mode='manual', db_connection_budget=48)
        self.assertEqual(connection_budget(manual)['workers'], 18)
        with patch.dict(os.environ, {'VIDEO_DB_CONNECTION_BUDGET': '48'}):
            budget = connection_budget(self.config)
            self.assertEqual((budget['requested_total'], budget['total'], budget['deployment_cap'],
                              budget['workers']), (53, 48, 48, 18))
            self.assertFalse(budget['target_supported'])
        with patch.dict(os.environ, {'VIDEO_WORKER_DB_CONNECTION_BUDGET': '32'}):
            budget = connection_budget(self.config)
            self.assertEqual((budget['deployment_worker_cap'], budget['workers']), (32, 16))
        with self.assertRaises(ValueError):
            connection_budget(dict(manual, db_connection_budget=8))

    def test_unlimited_pool_reserves_selected_session_demand_and_keeps_deployment_cap(self):
        config = dict(self.config, youtube_cookie_pool_worker_limit=0,
                      youtube_cookie_session_limit=2)
        self.assertEqual(target_workers(config), 42)
        budget = connection_budget(config)
        self.assertEqual((budget['target_workers'], budget['total'], budget['workers']), (42, 95, 42))
        with patch.dict(os.environ, {'VIDEO_WORKER_DB_CONNECTION_BUDGET': '48'}):
            limited = connection_budget(config)
            self.assertEqual(limited['workers'], 24)
            self.assertFalse(limited['target_supported'])
        self.assertEqual(target_workers(dict(config, youtube_cookie_session_limit=64)), 64)

    def test_zero_worker_limit_survives_live_sizing_and_hot_shrink(self):
        config = dict(self.config, youtube_cookie_pool_worker_limit=0,
                      youtube_cookie_pool=['fixture'] * 31, builtin_transfer_limit=0,
                      download_connection_limit=64)
        controller = ThroughputController(self.root)
        capacity = dict(known=True, capacity=31, reason='ready', profiles=[])
        with patch.object(controller, 'sample', return_value=self.sample):
            unlimited = controller.update(config, cookie_capacity=capacity, force=True)
            self.assertEqual(unlimited['workers'], 31)
            self.assertEqual(unlimited['worker_control']['configured_limit'], 0)
            self.assertIsNone(unlimited['worker_control']['bounds']['configured'])
            self.assertNotIn('configured', unlimited['worker_control']['limit_reasons'])
            self.assertEqual(runtime_limits(config, self.root)['workers'], 31)
            shrunk = controller.update(dict(config, youtube_cookie_pool_worker_limit=7),
                                       cookie_capacity=capacity, force=True)
            self.assertEqual(shrunk['workers'], 7)

    def test_custom_pools_change_the_formula_without_resizing_pools(self):
        with patch.dict(os.environ, {'VIDEO_DB_POOL_SIZE': '3', 'VIDEO_DB_MAX_OVERFLOW': '1',
                                    'VIDEO_WORKER_DB_POOL_SIZE': '3', 'VIDEO_WORKER_DB_MAX_OVERFLOW': '1'}):
            budget = connection_budget(self.config)
            self.assertEqual((budget['main'], budget['auxiliary'], budget['per_worker'],
                              budget['total'], budget['workers']), ((3, 1), 8, 4, 96, 21))

    def test_non_pool_auto_covers_bounded_preparation_and_manual_enforces_budget(self):
        self.assertEqual(target_workers(dict(worker_count=21, throughput_mode='auto')), 23)
        self.assertEqual(target_workers(dict(worker_count=21, throughput_mode='auto',
                                            download_methods=['bergung'], yt5s_browser_count=3)), 26)
        config = dict(worker_count=21, db_budget_mode='manual', db_connection_budget=48)
        self.assertEqual(runtime_limits(config, self.root)['workers'], 18)

    def test_live_changes_retire_and_restore_workers_without_restarting_pools(self):
        controller = ThroughputController(self.root)
        capacity = dict(known=True, capacity=21, reason='ready', profiles=[])
        manual = dict(self.config, db_budget_mode='manual', db_connection_budget=48)
        with patch.object(controller, 'sample', return_value=self.sample):
            limited = controller.update(manual, cookie_capacity=capacity, force=True)
            self.assertEqual(limited['workers'], 18)
            self.assertEqual(limited['worker_control']['limit_reasons'], ['database'])
            restored = controller.update(self.config, cookie_capacity=capacity, force=True)
            self.assertEqual(restored['workers'], 21)
            self.assertEqual(runtime_limits(self.config, self.root)['workers'], 21)
            limited_again = controller.update(manual, cookie_capacity=capacity, force=True)
            self.assertEqual(limited_again['workers'], 18)
            self.assertEqual(runtime_limits(manual, self.root)['workers'], 18)


if __name__ == '__main__':
    unittest.main()
