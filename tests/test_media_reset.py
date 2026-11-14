"""Full material reset, idempotent failures, and executor ownership boundaries."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import event, func, select

from tests import test_media_cleanup as cleanup_tests
from webapp import database as schema
from webapp.media_cleanup import clear_media


class MediaResetTests(unittest.TestCase):
    setUp = cleanup_tests.MediaCleanupTests.setUp
    write = cleanup_tests.MediaCleanupTests.write
    artifact = cleanup_tests.MediaCleanupTests.artifact
    request_clear = cleanup_tests.MediaCleanupTests.request_clear
    wait = cleanup_tests.MediaCleanupTests.wait

    def test_removes_orphans_hidden_files_and_all_download_work_caches(self):
        for name in ('orphan.jpg', 'orphan.ytdl', 'orphan.mp4.parts/0001.bin',
                     '.hidden/.deep/file.bin', 'leftover.txt', 'nested/.state.json'):
            self.write(name)
        caches = ('runtime/cache', 'runtime/downloads', 'runtime/uploads', 'runtime/browser-profiles',
                  'test-downloads', 'yt5s', 'imports', 'download-metrics')
        for folder in caches:
            target = self.store.directory / folder / '.hidden/leftover.bin'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'cache')
        archive = self.base / 'videos.txt'
        archive.write_text('youtube fixture\n', encoding='utf-8')
        self.store.archive_add('youtube fixture')
        result = clear_media(self.catalog, self.root, base=self.base)
        self.assertEqual(list(self.root.iterdir()), [])
        for folder in caches:
            self.assertFalse((self.store.directory / folder).exists(), folder)
        self.assertEqual(archive.read_text(), 'youtube fixture\n')
        self.assertTrue(self.store.archive_has('youtube fixture'))
        self.assertTrue(self.config.is_file())
        self.assertGreater(result['deleted_files'], 6)

    def test_deletes_task_logs_sessions_delivery_receipts_and_keeps_reservations(self):
        self.store.set_downloads_paused(False)
        task = self.store.create('download', {'urls': ['https://example.test/finished.mp4']})
        self.store.claim(self.store.owner)
        video = self.write('finished.mp4')
        self.store.statistics.record_file(task['id'], video)
        self.store.finish(task['id'], result={}, worker_id=self.store.owner)
        queued = self.store.create('download', {'urls': ['https://example.test/queued.mp4']})
        artifact = self.artifact(video)
        with self.store.connect() as db:
            db.execute(schema.delivery_artifacts.update().where(schema.delivery_artifacts.c.id == artifact)
                       .values(task_id=task['id']))
            db.execute(schema.delivery_runs.insert().values(task_id=task['id'], job_id='j' * 32,
                version=1, spec='{}', status='complete', test_mode=0))
            db.execute(schema.delivery_requirements.insert().values(task_id=task['id'], key='r',
                kind='subtitle', language='en', variant='fixture', status='done',
                artifact_id=artifact, updated_at='2026-11-14'))
            reservations = db.execute(select(schema.reservations)).all()
        path = self.store.log_path(task['id'])
        path.write_text('old log')
        path.with_suffix('.offset').write_text('0')
        self.store.archive_add('youtube finished', str(video))
        self.store.set_downloads_paused(True)
        response = self.request_clear()
        done = self.wait(response.json['id'])
        self.assertEqual(done['status'], 'succeeded', done)
        self.assertEqual(done['result']['deleted_tasks'], 2)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix('.offset').exists())
        self.assertIsNone(self.store.get(task['id']))
        self.assertIsNone(self.store.get(queued['id']))
        self.assertTrue(self.store.archive_has('youtube finished'))
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(schema.reservations)).all(), reservations)
            for table in (schema.delivery_artifacts, schema.delivery_runs, schema.delivery_requirements,
                          schema.deliveries, schema.test_files, schema.session_tasks, schema.download_sessions,
                          schema.method_tasks, schema.method_totals, schema.provider_tasks, schema.test_totals):
                self.assertEqual(db.execute(select(func.count()).select_from(table)).scalar_one(), 0, table.name)
        self.assertEqual(self.store.download_queue()['queued'], 0)
        self.assertEqual(self.store.statistics.dashboard()['file_bytes'], 0)

    def test_bounded_deletion_clears_more_than_one_database_batch(self):
        tasks = self.store.create_batch([{'urls': [f'https://example.test/{n}.mp4']} for n in range(1103)])
        self.assertEqual(len(tasks), 1103)
        reads = []
        def record_read(connection, cursor, statement, parameters, context, many):
            if statement.lstrip().startswith('SELECT') and 'tasks.payload' in statement and 'FROM tasks' in statement:
                reads.append(statement)
        event.listen(self.store.db.engine, 'before_cursor_execute', record_read)
        try:
            with patch.object(self.store, 'log_path', wraps=self.store.log_path) as log_path:
                result = clear_media(self.catalog, self.root)
                log_path.assert_not_called()  # Never-started tasks have no files to probe.
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', record_read)
        self.assertGreaterEqual(len(reads), 3)
        self.assertTrue(all('LIMIT' in statement for statement in reads), reads)
        self.assertEqual(result['deleted_tasks'], 1103)
        self.assertEqual(self.store.download_queue()['queued'], 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(func.count()).select_from(schema.tasks)).scalar_one(), 0)
            self.assertEqual(db.execute(select(func.count()).select_from(schema.reservations)).scalar_one(), 1103)

    def test_another_instance_keeps_its_files_history_and_statistics(self):
        job = 'j' * 32
        with self.store.connect() as db:
            self.store.lists.create(db, job, {}, 'Shared job')
            db.execute(schema.download_lists.update().where(schema.download_lists.c.id == job).values(released=1))
        self.store.set_downloads_paused(False)
        records = []
        for name in ('local', 'other'):
            path = self.root / 'local.mp4' if name == 'local' else self.base / 'foreign.mp4'
            path.write_bytes(b'fixture')
            task = self.store.create('download', {'batch_id': job, 'urls': [f'https://example.test/{name}.mp4']})
            self.store.claim(self.store.owner)
            self.store.statistics.record_file(task['id'], path)
            self.store.finish(task['id'], result={}, worker_id=self.store.owner)
            artifact = self.artifact(path, instance=name)
            with self.store.connect() as db:
                db.execute(schema.tasks.update().where(schema.tasks.c.id == task['id']).values(instance_id=name))
                db.execute(schema.delivery_artifacts.update().where(schema.delivery_artifacts.c.id == artifact)
                    .values(task_id=task['id'], job_id=job))
            records.append((task, path, artifact))
        self.store.set_downloads_paused(True)
        self.store.fleet = SimpleNamespace(id='local')
        self.addCleanup(setattr, self.store, 'fleet', None)
        clear_media(self.catalog, self.root)
        self.assertIsNone(self.store.get(records[0][0]['id']))
        self.assertIsNotNone(self.store.get(records[1][0]['id']))
        self.assertTrue(records[1][1].is_file())
        self.assertEqual(self.store.statistics.dashboard()['file_count'], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(schema.delivery_artifacts.c.id)).scalars().all(), [records[1][2]])
            row = db.execute(select(schema.download_lists).where(schema.download_lists.c.id == job)).mappings().one()
            self.assertEqual((row['file_count'], row['file_bytes']), (1, 7))

    def test_cloud_delete_must_succeed_before_its_history_is_discarded(self):
        path = self.write('cloud.srt')
        artifact = self.artifact(path, storage='oss')
        with patch('Utils.oss_storage.OSSStorage') as storage:
            storage.return_value.bucket.delete_object.side_effect = RuntimeError('permission denied')
            response = self.request_clear()
            failed = self.wait(response.json['id'])
            self.assertEqual(failed['status'], 'failed')
            with self.store.connect() as db:
                self.assertIsNotNone(db.execute(select(schema.delivery_artifacts).where(
                    schema.delivery_artifacts.c.id == artifact)).first())
            storage.return_value.bucket.delete_object.side_effect = None
            storage.return_value.bucket.object_exists.return_value = False
            response = self.request_clear()
            done = self.wait(response.json['id'])
            self.assertEqual(done['status'], 'succeeded', done)
        self.assertEqual(self.client.get('/api/media/assets').json['counts']['all'], 0)

    def test_a_shared_cloud_object_is_retained_for_the_other_instance(self):
        path = self.root / 'shared.srt'
        self.artifact(path, storage='oss')
        foreign = self.artifact(path, storage='oss', instance='other')
        with patch('Utils.oss_storage.OSSStorage') as storage:
            clear_media(self.catalog, self.root)
            storage.return_value.bucket.delete_object.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(schema.delivery_artifacts.c.id)).scalars().all(), [foreign])

    def test_active_task_blocks_deletion_before_any_file_is_removed(self):
        path = self.write('active.mp4')
        self.store.set_downloads_paused(False)
        self.store.create('download', {'urls': ['https://example.test/active.mp4']})
        self.store.claim(self.store.owner)
        with self.assertRaisesRegex(RuntimeError, '仍有下载'):
            clear_media(self.catalog, self.root)
        self.assertTrue(path.is_file())

    def test_locked_task_log_keeps_its_task_for_a_successful_retry(self):
        task = self.store.create('download', {'urls': ['https://example.test/queued.mp4']})
        path = self.store.log_path(task['id'])
        path.write_text('locked')
        unlink = Path.unlink
        def locked(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError('locked')
            return unlink(candidate, *args, **kwargs)
        with patch.object(Path, 'unlink', autospec=True, side_effect=locked):
            with self.assertRaisesRegex(RuntimeError, '任务日志'):
                clear_media(self.catalog, self.root)
        self.assertIsNotNone(self.store.get(task['id']))
        clear_media(self.catalog, self.root)
        self.assertIsNone(self.store.get(task['id']))
        self.assertFalse(path.exists())

    def test_orphan_delivery_task_receipts_are_removed_with_the_artifact(self):
        path = self.write('orphan.srt')
        artifact = self.artifact(path)
        with self.store.connect() as db:
            db.execute(schema.delivery_runs.insert().values(task_id='t' * 32, job_id='j' * 32,
                version=1, spec='{}', status='complete', test_mode=0))
            db.execute(schema.delivery_requirements.insert().values(task_id='t' * 32, key='r',
                kind='subtitle', language='en', variant='fixture', status='done',
                artifact_id=artifact, updated_at='2026-11-14'))
        clear_media(self.catalog, self.root)
        with self.store.connect() as db:
            for table in (schema.delivery_runs, schema.delivery_requirements, schema.delivery_artifacts):
                self.assertEqual(db.execute(select(func.count()).select_from(table)).scalar_one(), 0)
