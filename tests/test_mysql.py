"""Run queue, login, media and storage contracts against an isolated MySQL 8 DB.

Set VIDEO_TEST_MYSQL_URL to a test administrator URL, never the application URL.
Every test creates and drops its own randomly named database.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError
from sqlalchemy.pool import NullPool

from app import create_app
from tests import test_controls, test_scale
from tests.test_storage import StorageContract
from tests.test_task_views import TaskViewContract
from tests.test_youtube_auth import AuthContract
from webapp.database import reservations, tasks


class MySQLFixture:
    @classmethod
    def setUpClass(cls):
        raw = os.environ.get('VIDEO_TEST_MYSQL_URL')
        if not raw:
            raise unittest.SkipTest('Set VIDEO_TEST_MYSQL_URL to run isolated MySQL integration tests')
        cls.mysql_url = make_url(raw)
        if cls.mysql_url.drivername != 'mysql+pymysql':
            raise ValueError('VIDEO_TEST_MYSQL_URL must use mysql+pymysql')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        database = 'test_video_' + uuid.uuid4().hex[:16]
        admin = create_engine(self.mysql_url.set(database='mysql'), poolclass=NullPool, hide_parameters=True)
        self.admin = admin
        self.addCleanup(admin.dispose)
        with admin.begin() as db:
            db.exec_driver_sql(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin')

        def cleanup():
            with admin.begin() as db:
                db.exec_driver_sql(f'DROP DATABASE `{database}`')
        self.addCleanup(cleanup)
        state = self.root / 'state'
        state.mkdir()
        url = self.mysql_url.set(database=database).render_as_string(hide_password=False)
        (state / 'database.json').write_text(json.dumps({'url': url}), encoding='utf-8')
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        self.app = create_app(self.config, state, start_worker=False)
        self.client = self.app.test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]
        self.headers = {'X-CSRF-Token': token}
        self.store = self.app.extensions['task_store']
        self.catalog = self.app.extensions['media_catalog']


class MySQLScaleTests(MySQLFixture, test_scale.ScaleTests):
    def test_case_sensitive_ids_emoji_long_snapshots_and_no_silent_truncation(self):
        payload = {'config': {'fixture': '中😀' * 40000}, 'base': str(self.root)}
        jobs = self.store.create_batch([{**payload, 'urls': [f'https://youtu.be/{value}']}
                                       for value in ('Abcdefghijk', 'abcdefghijk', 'Abcdefghijk')], deduplicate=True)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(self.store.get(jobs[0]['id'], private=True)['payload']['config'], payload['config'])
        self.assertEqual(len(self.store.page(query='Abcdefghijk')['items']), 1)
        self.assertEqual(len(self.store.page(query='abcdefghijk')['items']), 1)
        with self.assertRaises(DataError):
            self.store.archive_add('x' * 513)
        self.assertFalse(self.store.archive_has('x' * 512))

    def test_fractional_lease_and_media_timestamps_are_preserved(self):
        job = self.store.create('download', {'config': {}, 'base': str(self.root)})
        stamp = time.time() + 0.123456
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == job['id']).values(lease_until=stamp, available_at=stamp))
        saved = self.store.get(job['id'])
        self.assertAlmostEqual(saved['lease_until'], stamp, places=5)
        self.assertAlmostEqual(saved['available_at'], stamp, places=5)

    def test_bulk_duplicate_ownership_is_atomic_across_importers(self):
        payloads = [{'config': {}, 'base': str(self.root), 'urls': [f'https://youtu.be/{i:011d}']}
                    for i in range(25)]
        with ThreadPoolExecutor(4) as pool:
            created = list(pool.map(lambda _: self.store.create_batch(payloads, deduplicate=True), range(4)))
        self.assertEqual(sum(map(len, created)), 25)
        self.assertEqual(self.store.counts()['total'], 25)
        with self.store.connect() as db:
            owners = set(db.execute(select(reservations.c.task_id)).scalars())
        self.assertEqual(owners, {row['id'] for group in created for row in group})

    def test_skip_locked_claims_a_different_task_without_waiting(self):
        created = self.store.create_batch([{'config': {}, 'base': str(self.root),
                                           'urls': [f'https://example.test/{index}.mp4']} for index in range(2)])
        with self.store.connect() as db:
            db.execute(select(tasks).where(tasks.c.id == created[0]['id']).with_for_update()).all()
            with ThreadPoolExecutor(1) as pool:
                claimed = pool.submit(self.store.claim, 'parallel-owner').result(timeout=5)
            self.assertEqual(claimed['id'], created[1]['id'])


class MySQLControlTests(MySQLFixture, test_controls.ControlContract, unittest.TestCase):
    test_two_real_download_workers_stop_and_resume_partial_files = (
        test_controls.PostgreSQLControlTests.test_two_real_download_workers_stop_and_resume_partial_files)


class MySQLStorageTests(MySQLFixture, StorageContract, unittest.TestCase):
    pass


class MySQLAuthTests(MySQLFixture, AuthContract, unittest.TestCase):
    pass


class MySQLTaskViewTests(MySQLFixture, TaskViewContract, unittest.TestCase):
    pass


class MySQLMigrationTests(MySQLFixture, unittest.TestCase):
    def empty_target(self):
        name = 'test_migration_' + uuid.uuid4().hex[:16]
        with self.admin.begin() as db:
            db.exec_driver_sql(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin')
        def cleanup():
            with self.admin.begin() as db:
                db.exec_driver_sql(f'DROP DATABASE `{name}`')
        self.addCleanup(cleanup)
        return self.mysql_url.set(database=name).render_as_string(hide_password=False)

    def test_mysql_copy_preserves_all_tables_and_resumes_after_committed_checkpoint(self):
        from scripts import migrate_mysql
        from webapp.store import TaskStore
        from unittest.mock import patch
        from webapp.database import Database
        task = self.store.create('download', {'config': {}, 'base': str(self.root),
                                              'urls': ['https://youtu.be/Abcdefghijk']})
        self.store.archive_add('youtube Abcdefghijk')
        self.store.set_downloads_paused(True)
        url = self.empty_target()
        destination = self.root / 'migration'
        original = Database.ignore
        interrupted = False

        def fail_once(database, table, rows):
            nonlocal interrupted
            if table.name == 'tasks' and not interrupted:
                interrupted = True
                raise RuntimeError('Simulate interruption after other table checkpoints committed')
            return original(database, table, rows)

        with patch.object(Database, 'ignore', fail_once):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                migrate_mysql.migrate(self.store.db.url, url, destination, chunk_size=1)
        result = migrate_mysql.migrate(self.store.db.url, url, destination, chunk_size=1, activate=True)
        restored = TaskStore(destination)
        self.assertTrue(result['activated'])
        self.assertEqual(restored.get(task['id'])['url'], task['url'])
        self.assertEqual(restored.counts()['total'], 1)
        self.assertTrue(restored.archive_has('youtube Abcdefghijk'))
        self.assertTrue(restored.downloads_paused())
        self.assertEqual(migrate_mysql.migrate(self.store.db.url, url, destination)['counts'], result['counts'])
        self.assertEqual(self.store.counts()['total'], 1)

    def test_nonempty_target_and_unpaused_source_are_rejected_without_activation(self):
        from scripts.migrate_mysql import migrate
        with self.assertRaisesRegex(ValueError, '停止全部下载'):
            migrate(self.store.db.url, self.empty_target(), self.root / 'target')
        self.store.set_downloads_paused(True)
        from webapp.store import TaskStore
        url = self.empty_target()
        target = TaskStore(self.root / 'target', url)
        target.create('check', {'config': {}, 'base': str(self.root)})
        with self.assertRaisesRegex(ValueError, '非空'):
            migrate(self.store.db.url, url, self.root / 'target', activate=True)
        self.assertEqual(target.counts()['total'], 1)
        self.assertFalse((self.root / 'target/database.json').exists())

    def test_postgresql_to_mysql_preserves_queue_and_sequence(self):
        from scripts.migrate_mysql import migrate
        from webapp.store import TaskStore
        from sqlalchemy.engine import URL
        connection = Path(__file__).resolve().parents[1] / '.cache/postgres/connection.json'
        if not connection.is_file():
            self.skipTest('Local PostgreSQL unavailable for cross-database migration test')
        config = json.loads(connection.read_text(encoding='utf-8'))
        pg_url = URL.create('postgresql+psycopg', username=config['user'], password=config['password'],
                            host='127.0.0.1', port=config['port'], database='postgres')
        admin = create_engine(pg_url, isolation_level='AUTOCOMMIT', poolclass=NullPool, hide_parameters=True)
        self.addCleanup(admin.dispose)
        name = 'test_mysql_migration_' + uuid.uuid4().hex[:12]
        with admin.connect() as db:
            db.exec_driver_sql(f'CREATE DATABASE "{name}"')
        def cleanup():
            with admin.connect() as db:
                db.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        self.addCleanup(cleanup)
        pg_source = TaskStore(self.root / 'pg-source', pg_url.set(database=name).render_as_string(hide_password=False))
        records = pg_source.create_batch([{'config': {}, 'base': str(self.root),
                                          'urls': [f'https://youtu.be/{i:011d}']} for i in range(3)])
        pg_source.set_downloads_paused(True)
        url = self.empty_target()
        migrate(pg_source.db.url, url, self.root / 'pg-source', activate=True)
        restored = TaskStore(self.root / 'pg-source')
        self.assertTrue(restored.db.mysql)
        self.assertEqual([row['id'] for row in restored.page(order='asc')['items']], [row['id'] for row in records])
        added = restored.create('check', {'config': {}, 'base': str(self.root)})
        self.assertGreater(added['seq'], records[-1]['seq'])
        self.assertEqual(pg_source.counts()['total'], 3)

    def test_sqlite_to_mysql_keeps_progress_media_and_existing_log_files(self):
        from scripts.migrate_mysql import migrate
        from webapp.store import TaskStore
        from webapp.catalog import MediaCatalog
        original = TaskStore(self.root / 'sqlite-source')
        task = original.create('download', {'config': {}, 'base': str(self.root),
                                             'batch_id': 'b' * 32, 'urls': ['https://youtu.be/Abcdefghijk']})
        original.update_progress(task['id'], {'percent': 42, 'title': '下载测试 😀'})
        original.log_path(task['id']).write_text('保留原日志', encoding='utf-8')
        original.archive_add('youtube Abcdefghijk')
        root = self.root / 'videos'
        root.mkdir()
        video = root / '下载测试 😀.mp4'
        video.write_bytes(b'0123456789')
        MediaCatalog(original).index_file(video, root)
        original.set_downloads_paused(True)
        result = migrate(original.db.url, self.empty_target(), original.directory, activate=True)
        restored = TaskStore(original.directory)
        self.assertTrue(restored.db.mysql)
        self.assertEqual(result['counts']['media_catalog'], 1)
        self.assertEqual(restored.get(task['id'])['progress']['percent'], 42)
        self.assertIn('保留原日志', restored.logs(task['id'])['text'])
        self.assertEqual(MediaCatalog(restored).page(root)['items'][0]['title'], video.stem)
        self.assertEqual(restored.batch_page()['items'][0]['counts']['total'], 1)
        self.assertTrue(restored.archive_has('youtube Abcdefghijk'))
