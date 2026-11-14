"""Validate the new time migration and candidate scan against disposable MySQL."""
import json
import unittest

from sqlalchemy import inspect, select

from tests.test_mysql import MySQLFixture
from webapp.database import deliveries, test_files, tasks
from webapp.migrations import _completed_file_times


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

    def test_time_schema_and_index_creation_are_restartable_without_backfill(self):
        with self.store.connect() as db:
            for table in (deliveries, test_files):
                index_name = next(index.name for index in table.indexes if index.name.endswith('_time'))
                db.exec_driver_sql(f'ALTER TABLE {table.name} DROP INDEX {index_name}')
                db.exec_driver_sql(f'ALTER TABLE {table.name} DROP COLUMN created_epoch')
                db.exec_driver_sql(f"INSERT INTO {table.name} (id,seq,session_id,task_id,title,path,size,created_at) VALUES ('fixture',1,'session','task','','',100,'2026-11-06T08:00:00+08:00')")
        _completed_file_times(self.store.db.engine)
        _completed_file_times(self.store.db.engine)
        for table in (deliveries, test_files):
            with self.store.connect() as db:
                value = db.execute(select(table.c.created_epoch).where(table.c.id=='fixture')).scalar_one()
            self.assertIsNone(value)
            self.assertTrue(any(index['name'].endswith('_time') for index in inspect(self.store.db.engine).get_indexes(table.name)))
