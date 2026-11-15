"""Validate the new time migration and candidate scan against disposable MySQL."""
import json
import unittest

from sqlalchemy import inspect, select

from tests.test_mysql import MySQLFixture
from webapp.database import deliveries, test_files, tasks


class SmoothnessMySQLTests(MySQLFixture, unittest.TestCase):
    def test_ready_index_skips_completed_history_without_changing_order(self):
        with self.store.connect() as db:
            db.execute(tasks.insert(), [dict(id=f'{i:032x}', seq=i+1, batch_id='scan-fixture',
                action='download', status='succeeded' if i < 5900 else 'queued',
                created_at='2026-11-06T00:00:00+00:00', payload='{}') for i in range(6000)])
            scans, results = {}, {}
            for index in ('ix_tasks_batch_seq', 'ix_tasks_batch_ready'):
                before = int(db.exec_driver_sql("SHOW SESSION STATUS LIKE 'Handler_read_next'").one()[1])
                results[index] = db.exec_driver_sql(f"SELECT id FROM tasks FORCE INDEX ({index}) WHERE batch_id='scan-fixture' AND status='queued' ORDER BY seq LIMIT 96").scalars().all()
                after = int(db.exec_driver_sql("SHOW SESSION STATUS LIKE 'Handler_read_next'").one()[1])
                scans[index] = after-before
            self.assertEqual(results['ix_tasks_batch_seq'], results['ix_tasks_batch_ready'])
            self.assertEqual(len(results['ix_tasks_batch_ready']), 96)
            self.assertGreater(scans['ix_tasks_batch_seq'], 5800)
            self.assertLessEqual(scans['ix_tasks_batch_ready'], 100)
            print('QUEUE_SCAN_FIXTURE', json.dumps(scans), flush=True)
