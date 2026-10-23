"""Verify the real SQLite-to-PostgreSQL migration and its resumability."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import unittest
import uuid

from scripts.migrate_database import migrate
from tests.test_scale import PostgreSQLFixture


class MigrationTests(PostgreSQLFixture, unittest.TestCase):
    def test_legacy_records_logs_batches_rotation_and_archive_survive_idempotent_migration(self):
        state = self.root / 'legacy'
        state.mkdir()
        batch = uuid.uuid4().hex
        identifiers = [uuid.uuid4().hex for _ in range(5)]
        statuses = ['succeeded', 'failed', 'succeeded', 'queued', 'running']
        with closing(sqlite3.connect(state / 'tasks.sqlite3')) as db, db:
            db.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, action TEXT, status TEXT, created_at TEXT, '
                       'started_at TEXT, finished_at TEXT, payload TEXT, result TEXT, error TEXT, progress TEXT)')
            for index, status in enumerate(statuses):
                payload = {'config': {'path': 'videos'}, 'base': str(self.root), 'batch_id': batch,
                           'urls': [f'https://www.youtube.com/watch?v={index:011d}']}
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (identifiers[index], 'download', status, '2026-10-20T10:00:00+08:00', None, None,
                            json.dumps(payload), json.dumps({'skipped': index == 2}), None,
                            json.dumps({'rotation_id': 'legacy-generation', 'title': f'旧标题 {index}'})))
        (state / f'{identifiers[0]}.log').write_text('旧任务日志\n', encoding='utf-8')
        (self.root / 'videos.txt').write_text('youtube 00000000000\nyoutube 00000000002', encoding='utf-8')
        videos = self.root / 'videos'
        videos.mkdir()
        (videos / '旧视频.mp4').write_bytes(b'0123456789')
        (videos / '旧视频.jpg').write_bytes(b'cover fixture')
        # The destination was initialized but has no tasks; its URL is explicit.
        result = migrate(state, self.store.db.url, self.config, activate=True)
        self.assertEqual(result['tasks'], 5)
        self.assertTrue((Path(result['backup']) / 'tasks.sqlite3').is_file())
        self.assertEqual(self.store.counts()['total'], 5)
        self.assertEqual(self.store.get(identifiers[4])['status'], 'interrupted')
        self.assertEqual(self.store.get(identifiers[0], private=True)['payload']['config']['path'], 'videos')
        self.assertEqual(self.store.rotation_completed('legacy-generation'), 2)
        self.assertEqual(self.store.batch_page()['items'][0]['counts']['total'], 5)
        self.assertTrue(self.store.archive_has('youtube 00000000002'))
        self.assertEqual(self.catalog.page(videos)['total'], 1)
        from webapp.store import TaskStore
        migrated = TaskStore(state)
        self.assertIn('旧任务日志', migrated.logs(identifiers[0])['text'])
        second = migrate(state, self.store.db.url, self.config, activate=True)
        self.assertEqual(second['tasks'], 5)
        self.assertEqual(self.store.rotation_completed('legacy-generation'), 2)
        self.assertEqual(self.store.counts()['total'], 5)
        added = self.store.create('check', {'config': {}, 'base': str(self.root)})
        self.assertGreater(added['seq'], 5)

    def test_backup_roundtrip_and_refusal_to_overwrite_existing_tables(self):
        from psycopg import sql
        from scripts.backup_database import backup, restore
        from webapp.store import TaskStore
        original = self.store.create('check', {'config': {}, 'base': str(self.root)})
        output = backup(self.root / 'state/database.json', self.root / 'backup', self.config, self.root / 'state')
        self.assertGreater((output / 'database.dump').stat().st_size, 0)
        with self.assertRaisesRegex(ValueError, '非空'):
            restore(output / 'database.dump', self.root / 'state/database.json')
        database = 'restore_video_' + uuid.uuid4().hex[:16]
        self.admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
        self.addCleanup(lambda: self.admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database))))
        from sqlalchemy.engine import make_url
        url = make_url(self.store.db.url).set(database=database).render_as_string(hide_password=False)
        target = self.root / 'restored'
        target.mkdir()
        connection = target / 'database.json'
        connection.write_text(json.dumps({'url': url}), encoding='utf-8')
        restore(output / 'database.dump', connection)
        restored = TaskStore(target)
        self.assertEqual(restored.counts()['total'], 1)
        self.assertEqual(restored.get(original['id'])['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
