"""Recent output rates follow completion instants and executor session boundaries."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from webapp.database import deliveries, download_sessions, test_files
from webapp.store import TaskStore


class OutputHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = TaskStore(Path(temp.name))
        self.end = datetime(2026, 11, 4, 10, 30, 30, tzinfo=timezone.utc).timestamp()
        self.identity = self.session('alpha', 1800)

    def session(self, instance, elapsed):
        with self.store.connect() as db:
            identity = self.store.statistics.start_session(db, instance)
            db.execute(download_sessions.update().where(download_sessions.c.id == identity).values(
                started_at=datetime.fromtimestamp(self.end - elapsed, timezone.utc).isoformat()))
        return identity

    def add(self, offset, size, *, identity=None, testing=False, zone=timezone.utc):
        ledger = test_files if testing else deliveries
        created = datetime.fromtimestamp(self.end + offset, zone).isoformat()
        with self.store.connect() as db:
            db.execute(ledger.insert().values(id=uuid.uuid4().hex,
                seq=self.store.db.sequence(db, 'test_fixture_' + ledger.name), session_id=identity or self.identity,
                task_id=uuid.uuid4().hex, title='fixture', path='/already-deleted.mp4', size=size, created_at=created))

    def current(self, instance='alpha'):
        with patch('webapp.fleet.database_time', return_value=self.end), self.store.connect() as db:
            return self.store.statistics.current_session(db, instance)

    def test_rolling_window_excludes_old_and_future_files_and_uses_partial_minute(self):
        for offset, size in [(-601, 12_000_000), (-600, 3_000_000), (-599, 6_000_000),
                             (-60, 9_000_000), (-15, 15_000_000), (1, 100_000_000)]:
            self.add(offset, size)
        value = self.current()
        self.assertEqual(value['output_recent_seconds'], 600)
        self.assertEqual(value['output_recent_file_bytes_exact'], '30000000')
        self.assertEqual(value['output_recent_mbps'], .4)
        points = value['output_history']['points']
        self.assertEqual(len(points), 30)
        self.assertEqual(points[-2]['output_mbps'], 1.2)
        self.assertEqual(points[-1]['elapsed_seconds'], 30)
        self.assertEqual(points[-1]['output_mbps'], 4)
        self.assertTrue(points[-1]['partial'])
        self.assertEqual(points[-1]['file_count'], 1)
        self.assertEqual(points[-3]['output_mbps'], 0)

    def test_short_session_uses_actual_time_and_marks_before_start_as_unavailable(self):
        identity = self.session('beta', 20)
        self.add(-21, 100_000_000, identity=identity)
        self.add(-20, 10_000_000, identity=identity)
        value = self.current('beta')
        self.assertEqual(value['output_recent_seconds'], 20)
        self.assertEqual(value['output_recent_mbps'], 4)
        points = value['output_history']['points']
        self.assertTrue(all(point['output_mbps'] is None for point in points[:-1]))
        self.assertEqual(points[-1]['output_mbps'], 4)

    def test_offsets_and_deleted_test_ledgers_survive_refresh_and_do_not_mix_instances(self):
        self.add(-100, 12_000_000, zone=timezone(timedelta(hours=-11)))
        self.add(-40, 18_000_000, testing=True, zone=timezone(timedelta(hours=14)))
        other = self.session('beta', 1800)
        self.add(-10, 600_000_000, identity=other)
        first = self.current()
        self.assertEqual(first['output_recent_mbps'], .4)
        self.assertEqual(sum(point['file_bytes'] for point in first['output_history']['points']), 30_000_000)
        self.assertEqual(first['output_history'], self.current()['output_history'])
        self.assertEqual(self.current('beta')['output_recent_mbps'], 8)

    def test_idle_window_is_zero_and_new_session_does_not_inherit_history(self):
        self.add(-1000, 50_000_000)
        self.assertEqual(self.current()['output_recent_mbps'], 0)
        with self.store.connect() as db:
            self.store.statistics.stop_session(db, 'alpha')
        self.assertIsNone(self.current())
        self.session('alpha', 100)
        value = self.current()
        self.assertEqual(value['output_recent_mbps'], 0)
        self.assertTrue(all(point['file_bytes'] == 0 for point in value['output_history']['points']))

    def test_zero_elapsed_has_no_rate(self):
        self.session('beta', 0)
        value = self.current('beta')
        self.assertIsNone(value['output_recent_mbps'])
        self.assertTrue(all(point['output_mbps'] is None for point in value['output_history']['points']))


if __name__ == '__main__':
    unittest.main()
