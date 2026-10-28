"""Legacy standalone queues upgrade without losing ownership, media or billing."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select
from app import create_app
from Utils.config import load_config
from webapp.database import deliveries, instances, settings, tasks, workers
from webapp.deployment import write_profile
from webapp.modes import ModeTransitionError, mode_key, single_state
from webapp.store import TaskStore


class UnifiedUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(temporary.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\ndownload_method: downmp3\n'
                               'downmp3_quality: "480"\n', encoding='utf-8')
        self.url = 'sqlite:///' + (self.root / 'queue.sqlite3').as_posix()
        self.directory = self.root / 'state'
        self.engines = []
        self.addCleanup(lambda: [engine.dispose() for engine in self.engines])
        self.original = self.make_app()
        self.store = self.original.extensions['task_store']
        self.state = {'instance_id': 'original-node', 'owner': 'b' * 32, 'status': 'completed'}

    def make_app(self, role='hybrid'):
        app = create_app(self.config, self.directory, start_worker=False, role=role,
                         instance_id='original-node', database_url=self.url)
        self.engines.append(app.extensions['task_store'].db.engine)
        return app

    def legacy(self, *, completed=True):
        self.state['status'] = 'completed' if completed else 'requested'
        write_profile(self.directory / 'single-mode.json', self.state)
        with self.store.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'original-node').values(
                role='worker', heartbeat=0, paused=0, limits='{"downmp3_quality":"1080"}'))
            if completed:
                db.execute(settings.update().where(settings.c.key == 'fleet_enabled').values(value=mode_key(self.state)))
                (self.directory / 'fleet-runtime.json').replace(self.directory / 'fleet-runtime.cluster-backup.json')

    def mode(self):
        with self.store.connect() as db:
            return db.execute(select(settings.c.value).where(settings.c.key == 'fleet_enabled')).scalar_one()

    def test_upgrade_preserves_tasks_media_identity_local_settings_and_billing(self):
        task = self.store.create('download', {'urls': ['https://youtu.be/npNnbj46_ik']})
        root = self.root / 'videos'
        root.mkdir()
        video = root / 'fixture.mp4'
        video.write_bytes(b'fixture video content')
        self.original.extensions['media_catalog'].index_file(video, root)
        before = self.original.extensions['media_catalog'].page(root)
        identity = (self.directory / 'instance.json').read_bytes()
        delivery_id = hashlib.sha256((task['id'] + '\0delivery').encode()).hexdigest()
        with self.store.connect() as db:
            db.execute(deliveries.insert().values(id=delivery_id, seq=1, session_id='history', task_id=task['id'],
                title='previous delivery', path=str(video), size=123456, created_at='2026-10-26', video_id='fixture'))
        self.legacy()
        app = self.make_app('all')
        store, fleet = app.extensions['task_store'], app.extensions['fleet']
        self.assertTrue(fleet.can_manage and fleet.can_download)
        self.assertEqual((fleet.role, fleet.id), ('hybrid', 'original-node'))
        self.assertTrue(fleet.row()['paused'])
        self.assertTrue(store.downloads_paused())
        self.assertEqual(self.mode(), 'true')
        self.assertEqual((self.directory / 'instance.json').read_bytes(), identity)
        self.assertIsNone(single_state(self.directory))
        self.assertTrue((self.directory / 'single-mode.legacy.json').exists())
        self.assertEqual(store.get(task['id'])['status'], 'queued')
        after = app.extensions['media_catalog'].page(root)
        self.assertEqual(after['items'][0]['id'], before['items'][0]['id'])
        self.assertEqual(video.read_bytes(), b'fixture video content')
        self.assertEqual(fleet.local_settings()['config']['downmp3_quality'], '480')
        self.assertEqual(load_config(self.config)[0]['downmp3_quality'], '480')
        with store.connect() as db:
            self.assertEqual(db.execute(select(deliveries.c.size)).scalar_one(), 123456)
        fleet.control(fleet.id, {'action': 'resume'})
        store.activate_download_lists()
        self.assertEqual(store.claim()['id'], task['id'])
        self.assertIsNone(self.store.claim())
        self.assertFalse(self.store.fleet.beat())
        self.assertFalse(store.statistics.record_file(task['id'], video))
        child = TaskStore(self.directory, self.url)
        self.engines.append(child.db.engine)
        self.assertEqual(child.fleet.role, 'hybrid')
        self.assertIsNone(child.single)

    def test_pending_conversion_is_cancelled_with_other_instances_online(self):
        self.legacy(completed=False)
        task = self.store.create('import', {'path': 'local.txt'})
        with self.store.connect() as db:
            row = dict(db.execute(select(instances)).mappings().one())
            row.update(id='other-node', role='manager', heartbeat=self.store.fleet.clock(db))
            db.execute(instances.insert().values(**row))
        app = self.make_app('manager')
        store = app.extensions['task_store']
        self.assertEqual(store.claim(actions=('import',))['id'], task['id'])
        self.assertIsNone(single_state(self.directory))
        self.assertEqual(self.mode(), 'true')
        with store.connect() as db:
            self.assertEqual(db.execute(select(instances.c.role).where(instances.c.id == 'other-node')).scalar_one(), 'manager')

    def test_mismatched_owner_or_identity_cannot_unlock_queue(self):
        self.legacy()
        original = self.mode()
        for changes in ({'owner': 'c' * 32}, {'instance_id': 'another-node'}):
            write_profile(self.directory / 'single-mode.json', {**self.state, **changes})
            with self.assertRaisesRegex(ModeTransitionError, '身份不匹配'):
                self.make_app()
            self.assertEqual(self.mode(), original)

    def test_unrelated_state_directory_cannot_unlock_standalone_database(self):
        self.legacy()
        with self.assertRaisesRegex(ModeTransitionError, '原状态目录'):
            create_app(self.config, self.root / 'unrelated', start_worker=False, database_url=self.url)
        self.assertEqual(self.mode(), mode_key(self.state))

    def test_live_old_workers_and_leases_prevent_migration(self):
        self.legacy()
        with self.store.connect() as db:
            stamp = self.store.fleet.clock(db)
            db.execute(workers.insert().values(id='old-worker', slot='0', pid=123, heartbeat=stamp, state='running'))
        with self.assertRaisesRegex(ModeTransitionError, '下载进程仍在线'):
            self.make_app()
        with self.store.connect() as db:
            db.execute(workers.update().values(heartbeat=0))
        task = self.store.create('download', {'urls': ['https://youtu.be/npNnbj46_ik']})
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='running', lease_until=stamp + 300))
        with self.assertRaisesRegex(ModeTransitionError, '租约尚未到期'):
            self.make_app()
        self.assertEqual(self.mode(), mode_key(self.state))

    def test_incomplete_finalization_retries_without_losing_settings(self):
        self.legacy()
        original_replace = Path.replace
        def fail_archive(path, target):
            if path.name == 'single-mode.json':
                raise PermissionError('fixture archive failure')
            return original_replace(path, target)
        with patch.object(Path, 'replace', fail_archive), self.assertRaises(PermissionError):
            self.make_app()
        self.assertEqual(self.mode(), 'true')
        with self.store.connect() as db:
            db.execute(instances.update().values(heartbeat=0))
        restarted = self.make_app()
        self.assertEqual(restarted.extensions['fleet'].local_settings()['config']['downmp3_quality'], '480')
        self.assertIsNone(single_state(self.directory))

    def test_expired_standalone_worker_cannot_renew_or_finish_after_upgrade(self):
        self.legacy()
        old = TaskStore(self.directory, self.url)
        self.engines.append(old.db.engine)
        self.assertIsNone(old.fleet)
        maintenance = old.create('check', {'base': str(self.root)})
        task = old.create('download', {'urls': ['https://youtu.be/npNnbj46_ik']})
        with old.connect() as db:
            row = db.execute(select(tasks).where(tasks.c.id == task['id'])).mappings().one()
            old._transition(db, row, 'running')
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status='running', worker_id=old.owner, lease_until=1))
        app = create_app(self.config, self.directory, start_worker=False, database_url=self.url)
        self.engines.append(app.extensions['task_store'].db.engine)
        self.assertEqual(app.extensions['fleet'].id, 'original-node')
        self.assertFalse(old.heartbeat(task['id'], old.owner))
        self.assertFalse(old.finish(task['id'], result={}, worker_id=old.owner))
        current = app.extensions['task_store']
        self.assertEqual(current.recover(expired_only=True), 1)
        self.assertEqual(current.get(task['id'])['status'], 'queued')
        self.assertEqual(current.claim(actions=('check',))['id'], maintenance['id'])
