"""Processing pool lifetime, transaction boundaries and instance controls."""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import event, select

from tests import test_scale, test_fleet
from webapp.database import workers, instances, settings
from webapp.worker.idle import ProcessingIdle, DownloadIdle, notify_processing


class ProcessingIdleTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_idle_retires_without_lease_or_open_database_connection(self):
        self.store.set_downloads_paused(False)
        clock = [0.0]
        checked_out = [0]
        def checkout(*args):checked_out[0] += 1
        def checkin(*args):checked_out[0] -= 1
        event.listen(self.store.db.engine, 'checkout', checkout)
        event.listen(self.store.db.engine, 'checkin', checkin)
        def sleep(seconds):
            self.assertEqual(checked_out[0], 0, 'idle sleep must release DB connection')
            clock[0] += seconds
        idle = ProcessingIdle(self.store, -2, 'local', idle_seconds=25,
                              clock=lambda:clock[0], sleep=sleep)
        with patch.object(self.store, 'worker_status', wraps=self.store.worker_status) as beat:
            while idle.wait():pass
            self.assertEqual(beat.call_count, 3)
        self.assertEqual(clock[0], 25)
        if self.store.progress_cache.enabled:
            row = next(row for row in self.store.progress_cache.read_workers() if row['id'] == self.store.owner)
            with self.store.connect() as db:
                self.assertIsNone(db.execute(select(workers).where(workers.c.id == self.store.owner)).first())
        else:
            with self.store.connect() as db:
                row = db.execute(select(workers).where(workers.c.id == self.store.owner)).mappings().one()
        self.assertEqual(row['state'], 'idle')
        self.assertIsNone(row['task_id'])

    def test_notification_wakes_early_and_missing_notification_polls(self):
        self.store.set_downloads_paused(False)
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
            if clock[0] == .25:notify_processing(self.store.directory)
        idle = ProcessingIdle(self.store, -2, 'local', clock=lambda:clock[0], sleep=sleep)
        self.assertTrue(idle.wait())
        self.assertEqual(clock[0], .25)
        # No new notification: delayed retries/missed signals still get a claim.
        self.assertTrue(idle.wait())
        self.assertEqual(clock[0], 2.25)
        self.store.set_downloads_paused(True)
        self.assertFalse(idle.wait())

    def test_spurious_notifications_do_not_extend_idle_lifetime(self):
        self.store.set_downloads_paused(False)
        clock = [0.0]
        def sleep(seconds):clock[0] += seconds
        idle = ProcessingIdle(self.store, -2, 'local', idle_seconds=5,
                              clock=lambda:clock[0], sleep=sleep)
        with patch.object(idle, '_token', side_effect=lambda:clock[0]):
            while idle.wait():clock[0] += .25
        self.assertGreaterEqual(clock[0], 5)
        self.assertLess(clock[0], 5.5)
        idle.busy()
        self.assertIsNone(idle.idle_since)

    def test_network_role_keeps_finite_drain_behavior(self):
        from webapp.worker.queue import drain_queue
        with patch.object(self.store,'claim',return_value=None), \
             patch('webapp.worker.idle.ProcessingIdle') as idle:
            drain_queue(self.store.directory, role='download', execute_task=None,
                        store_factory=lambda *args:self.store)
        idle.assert_not_called()

    def test_removed_processing_slot_exits_before_taking_another_task(self):
        from webapp.worker.queue import drain_queue
        self.store.set_downloads_paused(False)
        self.store.set_runtime({**self.store.runtime(), 'postprocess_limit':1})
        with patch.object(self.store, 'claim') as claim:
            drain_queue(self.store.directory, slot=-3, role='processing', execute_task=None,
                        store_factory=lambda *args:self.store)
        claim.assert_not_called()


class ProcessingFleetIdleTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet

    def test_network_reuse_respects_instance_slot_limit_pause_and_boot(self):
        primary = DownloadIdle(self.a, 0, 'first')
        self.assertTrue(primary.allowed())
        self.assertFalse(DownloadIdle(self.a, 2, 'removed').allowed())
        self.a.fleet.control('alpha', {'action': 'drain'})
        self.assertFalse(primary.allowed())
        self.assertTrue(DownloadIdle(self.b, 0, 'other').allowed())
        with self.a.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(paused=0, boot_id='replacement'))
        self.assertFalse(primary.allowed())

    def test_fleet_pause_stop_boot_fence_and_shrinking_slots_are_scoped(self):
        primary = ProcessingIdle(self.a, -2, 'first')
        excess = ProcessingIdle(self.a, -3, 'second')
        other = ProcessingIdle(self.b, -3, 'second')
        # Legacy global pause must not override the local fleet intent.
        with self.a.connect() as db:
            db.execute(settings.update().where(settings.c.key=='downloads_paused').values(value='true'))
            config = json.loads(self.a.fleet.row(db)['config'])
            config['config']['postprocess_limit'] = 1
            db.execute(instances.update().where(instances.c.id=='alpha')
                       .values(config=json.dumps(config)))
        self.assertTrue(primary.allowed())
        self.assertFalse(excess.allowed())
        self.assertTrue(other.allowed())
        for values in ({'paused':1}, {'paused':0,'stop_requested':1},
                       {'stop_requested':0,'boot_id':'replacement'}):
            with self.a.connect() as db:
                db.execute(instances.update().where(instances.c.id=='alpha').values(**values))
            self.assertFalse(primary.allowed())
            self.assertTrue(other.allowed())

    def test_attached_worker_uses_supervisor_boot_identity(self):
        from webapp.store import TaskStore
        attached = TaskStore(self.a.directory, self.a.db.url)
        self.addCleanup(attached.db.engine.dispose)
        self.assertEqual(attached.fleet.boot_id, self.a.fleet.boot_id)
        self.assertTrue(ProcessingIdle(attached, -2, 'first').allowed())


class ProcessingSupervisorTests(unittest.TestCase):
    from tests.test_dispatch_latency import DispatchLatencyTests as Fixture
    setUp = Fixture.setUp
    tick = Fixture.tick
    process = staticmethod(Fixture.process)

    def test_idle_processing_slots_are_reused_when_work_arrives(self):
        self.runner.processes.update({-2:self.process(), -3:self.process()})
        with patch.object(self.store, 'runnable_counts', return_value={'processing':2}), \
             patch('webapp.runtime.supervisor.subprocess.Popen') as launch:
            self.tick()
        launch.assert_not_called()

    def test_crashed_processing_slot_restarts_after_existing_backoff(self):
        import time
        from types import SimpleNamespace
        self.runner.settings=lambda:{'postprocess_limit':1,'disk_min_free_gb':0}
        self.runner.processes[-2]=SimpleNamespace(pid=42,poll=lambda:1)
        self.runner.last_started[-2]=time.monotonic()-6
        with patch.object(self.store, 'runnable_counts', return_value={'processing':1}), \
             patch('webapp.runtime.supervisor.subprocess.Popen',return_value=self.process()) as launch, \
             patch('webapp.diagnostics.capture_output'):
            self.tick()
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0][-1], 'processing')
