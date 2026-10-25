"""Real queue/catalogue behavior across a restart from cluster to single mode."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from app import create_app
from Utils.config import load_config
from webapp.database import deliveries, instances, settings, tasks
from webapp.deployment import write_profile
from webapp.modes import ModeTransitionError, mode_key, single_state
from webapp.settings import save_settings
from webapp.store import TaskStore


class SingleModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(temporary.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\ndownload_method: downmp3\n', encoding='utf-8')
        self.url = 'sqlite:///' + (self.root / 'queue.sqlite3').as_posix()
        self.directory = self.root / 'state'
        self.engines = []
        self.addCleanup(lambda: [engine.dispose() for engine in self.engines])
        self.original = self.make_app('manager')
        self.store = self.original.extensions['task_store']

    def make_app(self, role):
        app = create_app(self.config, self.directory, start_worker=False, role=role,
                         instance_id='original-node', database_url=self.url)
        self.engines.append(app.extensions['task_store'].db.engine)
        return app

    def request_conversion(self):
        write_profile(self.directory / 'single-mode.json', {
            'instance_id': 'original-node', 'owner': 'b' * 32, 'status': 'requested'})

    def queue(self):
        return self.store.create('download', {'urls': ['https://youtu.be/npNnbj46_ik']})

    def test_conversion_preserves_queue_media_settings_and_billing_and_downloads_locally(self):
        task = self.queue()
        root = self.root / 'videos'
        root.mkdir()
        video = root / 'fixture.mp4'
        video.write_bytes(b'fixture video content')
        self.original.extensions['media_catalog'].index_file(video, root)
        before_media = self.original.extensions['media_catalog'].page(root)
        self.store.fleet.save_policy({'download_method': 'yt5s', 'download_methods': ['yt5s', 'downmp3'],
                                     'downmp3_quality': '1080'})
        delivery_id = hashlib.sha256((task['id'] + '\0delivery').encode()).hexdigest()
        with self.store.connect() as db:
            db.execute(deliveries.insert().values(id=delivery_id, seq=1, session_id='history', task_id=task['id'],
                title='previous delivery', path=str(video), size=123456, created_at='2026-10-26', video_id='fixture'))
        self.request_conversion()
        app = self.make_app('all')
        store = app.extensions['task_store']
        self.assertIsNone(store.fleet)
        self.assertTrue(store.downloads_paused())
        self.assertFalse((self.directory / 'fleet-runtime.json').exists())
        self.assertTrue((self.directory / 'fleet-runtime.cluster-backup.json').exists())
        self.assertEqual(single_state(self.directory)['status'], 'completed')
        self.assertEqual(store.get(task['id'])['status'], 'queued')
        after_media = app.extensions['media_catalog'].page(root)
        self.assertEqual(after_media['total'], before_media['total'])
        self.assertEqual(after_media['items'][0]['id'], before_media['items'][0]['id'])
        self.assertEqual(video.read_bytes(), b'fixture video content')
        config, _ = load_config(self.config)
        self.assertEqual(config['download_methods'], ['yt5s', 'downmp3'])
        with store.connect() as db:
            self.assertEqual(db.execute(select(deliveries.c.size)).scalar_one(), 123456)
            self.assertEqual(db.execute(select(settings.c.value).where(settings.c.key == 'fleet_enabled')).scalar_one(),
                             mode_key(store.single))
        store.activate_download_lists()
        claimed = store.claim()
        self.assertEqual(claimed['id'], task['id'])
        self.assertEqual(claimed['payload']['config']['download_methods'], ['yt5s', 'downmp3'])
        # The original cluster executor is fenced even if its old process wakes.
        self.assertIsNone(self.store.claim())
        self.assertFalse(self.store.fleet.beat())
        fresh_child = TaskStore(self.directory, self.url)
        self.engines.append(fresh_child.db.engine)
        self.assertIsNone(fresh_child.fleet)
        self.assertEqual(fresh_child.single['instance_id'], 'original-node')
        # The billing identity is unchanged after the mode change.
        self.assertFalse(store.statistics.record_file(task['id'], video))

    def test_restarting_single_mode_does_not_restore_old_policy_or_pause_active_queue(self):
        self.request_conversion()
        app = self.make_app('all')
        config, _ = load_config(self.config)
        config['download_methods'] = ['downmp3']
        config['downmp3_quality'] = '480'
        save_settings(self.config, config)
        app.extensions['task_store'].set_downloads_paused(False)
        restarted = self.make_app('all')
        self.assertEqual(load_config(self.config)[0]['downmp3_quality'], '480')
        self.assertFalse(restarted.extensions['task_store'].downloads_paused())

    def test_other_online_instances_block_conversion_without_changing_database_mode(self):
        with self.store.connect() as db:
            row = dict(db.execute(select(instances)).mappings().one())
            row.update(id='another-worker', role='worker')
            db.execute(instances.insert().values(**row))
        self.request_conversion()
        with self.assertRaisesRegex(ModeTransitionError, '其他在线实例'):
            self.make_app('all')
        self.assertTrue((self.directory / 'fleet-runtime.json').exists())
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(settings.c.value).where(settings.c.key == 'fleet_enabled')).scalar_one(), 'true')

    def test_running_jobs_block_conversion(self):
        task = self.queue()
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='running'))
        self.request_conversion()
        with self.assertRaisesRegex(ModeTransitionError, '正在执行'):
            self.make_app('all')

    def test_pending_conversion_freezes_local_maintenance_claims_until_restart(self):
        task = self.store.create('import', {'path': 'local.txt'})
        self.request_conversion()
        self.assertIsNone(self.store.claim())
        converted = self.make_app('all').extensions['task_store']
        self.assertEqual(converted.claim()['id'], task['id'])

    def test_other_instances_pending_local_maintenance_blocks_conversion(self):
        task = self.store.create('import', {'path': 'remote-only.txt'})
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(target_instance_id='remote-worker'))
        self.request_conversion()
        with self.assertRaisesRegex(ModeTransitionError, '维护任务'):
            self.make_app('all')

    def test_new_cluster_instances_and_unrelated_single_instances_cannot_claim_after_conversion(self):
        task = self.queue()
        self.request_conversion()
        app = self.make_app('all')
        app.extensions['task_store'].activate_download_lists()
        with self.assertRaisesRegex(ValueError, '已切为单机模式'):
            self.make_app('manager')
        unrelated = TaskStore(self.root / 'other-state', self.url)
        self.engines.append(unrelated.db.engine)
        self.assertIsNone(unrelated.claim())
        self.assertEqual(app.extensions['task_store'].claim()['id'], task['id'])

    def test_conversion_can_resume_after_local_finalize_failure(self):
        self.request_conversion()
        with patch('webapp.deployment.write_profile', side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):
                self.make_app('all')
        self.assertFalse((self.directory / 'fleet-runtime.json').exists())
        restarted = self.make_app('all')
        self.assertIsNone(restarted.extensions['task_store'].fleet)
        self.assertEqual(single_state(self.directory)['status'], 'completed')
