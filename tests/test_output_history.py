"""Recent output rates follow completion instants and executor session boundaries."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from webapp.database import deliveries, delivery_artifacts, download_sessions, settings, test_files
from webapp.statistics import SESSION_OWNER_PREFIX
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

    def add(self, offset, size, *, identity=None, testing=False, instance=None, zone=timezone.utc):
        ledger = test_files if testing else deliveries
        created = datetime.fromtimestamp(self.end + offset, zone).isoformat()
        file_id, task_id = uuid.uuid4().hex, uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute(ledger.insert().values(id=file_id,
                seq=self.store.db.sequence(db, 'test_fixture_' + ledger.name), session_id=identity or self.identity,
                task_id=task_id, title='fixture', path='/already-deleted.mp4', size=size, created_at=created, created_epoch=self.end+offset))
            if instance:
                db.execute(delivery_artifacts.insert().values(id=file_id,
                    seq=self.store.db.sequence(db, 'delivery_artifact'), job_id='fixture', task_id=task_id,
                    title='fixture', contract_version=1, kind='av', language='', variant='fixture',
                    path='/already-deleted.mp4', size=size, instance_id=instance, storage='local',
                    available=0, test_mode=int(testing), created_at=created))

    def current(self, instance='alpha'):
        with patch('webapp.fleet.database_time', return_value=self.end), self.store.connect() as db:
            return self.store.statistics.current_session(db, instance)

    def delivery_speed(self, instance='alpha'):
        with patch('webapp.fleet.database_time', return_value=self.end):
            return self.store.statistics.delivery_speed(instance)

    def test_recent_session_excludes_old_and_future_files(self):
        for offset, size in [(-601, 12_000_000), (-600, 3_000_000), (-599, 6_000_000),
                             (-60, 9_000_000), (-15, 15_000_000), (1, 100_000_000)]:
            self.add(offset, size)
        value = self.current()
        self.assertEqual(value['output_recent_seconds'], 600)
        self.assertEqual(value['output_recent_file_bytes_exact'], '30000000')
        self.assertEqual(value['output_recent_mbps'], .4)

    def test_short_session_uses_actual_time(self):
        identity = self.session('beta', 20)
        self.add(-21, 100_000_000, identity=identity)
        self.add(-20, 10_000_000, identity=identity)
        value = self.current('beta')
        self.assertEqual(value['output_recent_seconds'], 20)
        self.assertEqual(value['output_recent_mbps'], 4)

    def test_offsets_and_deleted_test_ledgers_survive_refresh_and_do_not_mix_instances(self):
        self.add(-100, 12_000_000, zone=timezone(timedelta(hours=-11)))
        self.add(-40, 18_000_000, testing=True, zone=timezone(timedelta(hours=14)))
        other = self.session('beta', 1800)
        self.add(-10, 600_000_000, identity=other)
        first = self.current()
        self.assertEqual(first['output_recent_mbps'], .4)
        self.assertEqual(first['output_recent_file_bytes_exact'], '30000000')
        self.assertEqual(first, self.current())
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
        self.assertEqual(value['output_recent_file_bytes_exact'], '0')

    def test_zero_elapsed_has_no_rate(self):
        self.session('beta', 0)
        value = self.current('beta')
        self.assertIsNone(value['output_recent_mbps'])

    def test_delivery_speed_covers_exactly_24_hours_and_clips_partial_buckets(self):
        window = 24 * 60 * 60
        for offset, size in [(-window - 1, 99_000_000), (-window, 57_000_000),
                             (-31, 75_000_000), (-30, 15_000_000), (-1, 15_000_000),
                             (0, 99_000_000), (1, 99_000_000)]:
            self.add(offset, size, instance='alpha')
        history = self.delivery_speed()
        points = history['points']
        self.assertEqual((history['start_at'], history['end_at']), (self.end - window, self.end))
        self.assertEqual(history['interval_seconds'], 600)
        self.assertEqual(len(points), 145)
        self.assertEqual((points[0]['mbps'], points[-2]['mbps'], points[-1]['mbps']), (.8, 1, 8))
        self.assertEqual((points[0]['partial'], points[-2]['partial'], points[-1]['partial']), (True, False, True))
        self.assertEqual(points[-1]['file_count'], 2)
        self.assertEqual(sum(int(point['file_bytes_exact']) for point in points), 162_000_000)
        self.assertTrue(all(point['mbps'] == 0 for point in points[1:-2]))

    def test_delivery_speed_counts_deleted_test_files_across_sessions_and_isolates_instances(self):
        self.add(-100, 15_000_000, instance='alpha', zone=timezone(timedelta(hours=-11)))
        with self.store.connect() as db:
            self.store.statistics.stop_session(db, 'alpha')
        recent = self.session('alpha', 60)
        self.add(-20, 15_000_000, identity=recent, instance='alpha')
        self.add(-20, 15_000_000, identity=recent, instance='alpha', testing=True)
        self.add(-86400, 57_000_000, instance='alpha', testing=True)
        self.add(-86401, 99_000_000, instance='alpha', testing=True)
        self.add(0, 99_000_000, identity=recent, instance='alpha', testing=True)
        other = self.session('beta', 60)
        self.add(-20, 30_000_000, identity=other, instance='beta', testing=True)
        with self.store.connect() as db:
            self.store.statistics.stop_session(db, 'alpha')
        self.assertIsNone(self.current('alpha'))
        points = self.delivery_speed()['points']
        self.assertEqual(sum(point['file_count'] for point in points), 4)
        self.assertEqual(points[0]['mbps'], .8)
        self.assertEqual(points[-1]['file_bytes_exact'], '30000000')
        self.assertEqual(points[-1]['mbps'], 8)
        self.assertEqual(self.delivery_speed('beta')['points'][-1]['mbps'], 8)

    def test_delivery_speed_at_aligned_boundary_has_144_complete_buckets(self):
        self.end -= 30
        points = self.delivery_speed()['points']
        self.assertEqual(len(points), 144)
        self.assertTrue(all(not point['partial'] and point['mbps'] == 0 for point in points))

    def test_delivery_speed_uses_receipts_for_old_sessions_without_saved_owner(self):
        self.add(-20, 30_000_000, instance='alpha', testing=True)
        with self.store.connect() as db:
            self.store.statistics.stop_session(db, 'alpha')
            db.execute(settings.delete().where(settings.c.key == SESSION_OWNER_PREFIX + self.identity))
        self.assertEqual(self.delivery_speed('alpha')['points'][-1]['mbps'], 8)
        self.assertEqual(self.delivery_speed('beta')['points'][-1]['file_count'], 0)


if __name__ == '__main__':
    unittest.main()
