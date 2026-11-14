"""Exercise real deletion in isolated directories, including billing and queue guards."""
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import create_app
from sqlalchemy import select
from webapp.database import delivery_artifacts
from webapp.media_cleanup import clear_media, validate_clear_root


class MediaCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = self.base / 'config.yaml'
        self.config.write_text('path: videos\n', encoding='utf-8')
        self.app = create_app(self.config, self.base / 'state', start_worker=False)
        self.app.testing = True
        self.client = self.app.test_client()
        page = self.client.get('/')
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        self.headers = {'X-CSRF-Token': token}
        self.store = self.app.extensions['task_store']
        self.catalog = self.app.extensions['media_catalog']
        self.root = self.base / 'videos'
        self.root.mkdir()
        self.store.set_downloads_paused(True)

    def write(self, name, data=b'fixture'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def request_clear(self, **changes):
        return self.client.post('/api/media/clear', json={
            'confirmation': '清空', 'directory': str(self.root), **changes}, headers=self.headers)

    def wait(self, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.store.get(task_id)
            if task['status'] != 'running':
                return task
            time.sleep(0.02)
        self.fail('Cleanup did not finish')

    def artifact(self, path, *, storage='local', instance='local'):
        with self.store.connect() as db:
            seq = self.store.db.sequence(db, 'delivery_artifact')
            identity = f'{seq:064x}'
            db.execute(delivery_artifacts.insert().values(
                id=identity, seq=seq, job_id='j' * 32, task_id='t' * 32,
                video_id='fixture', title='Fixture', kind='subtitle', language='en',
                variant='fixture', path=str(path), size=7, instance_id=instance,
                storage=storage, object_key='fixture' if storage == 'oss' else None,
                available=1, test_mode=0, created_at='2026-01-01'))
        return identity

    def test_partial_clear_keeps_retry_records_and_success_removes_current_instance_outputs(self):
        subtitle = self.write('job/opaque-subtitle.srt')
        local = self.artifact(subtitle)
        cloud = self.artifact(subtitle, storage='oss')
        foreign_path = self.base / 'foreign.srt'
        foreign_path.write_bytes(b'foreign')
        other_instance = self.artifact(foreign_path, instance='other-instance')
        outside = self.base / 'outside.srt'
        outside.write_bytes(b'outside')
        outside_record = self.artifact(outside)
        locked = self.write('other-job/locked.vtt')
        locked_record = self.artifact(locked)
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == locked:
                raise PermissionError('locked subtitle')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'unlink', autospec=True, side_effect=unlink):
            response = self.request_clear()
            self.assertEqual(self.wait(response.json['id'])['status'], 'failed')
        with self.store.connect() as db:
            available = dict(db.execute(select(delivery_artifacts.c.id, delivery_artifacts.c.available)).all())
        self.assertEqual(available[local], 0)
        self.assertEqual(available[outside_record], 0)
        for identity in (cloud, other_instance, locked_record):
            self.assertEqual(available[identity], 1)
        self.assertFalse(outside.is_file())
        self.assertEqual(self.client.get('/api/delivery/files/' + local).status_code, 404)
        visible = {item['id'] for item in self.client.get('/api/media/assets').json['artifacts']['items']}
        self.assertNotIn(local, visible)
        self.assertIn(cloud, visible)
        with patch('Utils.oss_storage.OSSStorage') as storage:
            storage.return_value.bucket.object_exists.return_value = False
            response = self.request_clear()
            done = self.wait(response.json['id'])
            self.assertEqual(done['status'], 'succeeded', done)
            storage.return_value.bucket.delete_object.assert_called_once_with('fixture')
        self.assertFalse(locked.exists())
        self.assertTrue(foreign_path.exists())
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(delivery_artifacts.c.id)).scalars().all(), [other_instance])

    def test_matroska_audio_and_its_cover_are_removed_without_a_video(self):
        audio = self.write('opaque-audio.mka')
        cover = self.write('opaque-audio.jpg')
        clear_media(self.catalog, self.root)
        self.assertFalse(audio.exists())
        self.assertFalse(cover.exists())

    def test_orphan_locked_cover_is_removed_on_retry_without_its_audio(self):
        audio = self.write('audio.m4a')
        cover = self.write('audio.jpg')
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == cover:
                raise PermissionError('locked cover')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'unlink', autospec=True, side_effect=unlink):
            with self.assertRaises(RuntimeError):
                clear_media(self.catalog, self.root)
        self.assertFalse(audio.exists())
        self.assertTrue(cover.exists())
        clear_media(self.catalog, self.root)
        self.assertFalse(audio.exists())
        self.assertFalse(cover.exists())

    def test_background_clear_removes_all_material_history_but_preserves_deduplication(self):
        video = self.write('np/npNnbj46_ik/video [npNnbj46_ik].mp4')
        for suffix in ('.jpg', '.en.vtt', '.f137.mp4', '.f140.m4a', '.mp4.part'):
            self.write('np/npNnbj46_ik/video [npNnbj46_ik]' + suffix)
        self.write('np/npNnbj46_ik/video [npNnbj46_ik].info.json', json.dumps({'title': 'Test'}).encode())
        chunk = self.write('np/npNnbj46_ik/video [npNnbj46_ik].mp4.parts/0001.bin')
        note = self.write('keep.txt')
        unrelated = self.write('np/npNnbj46_ik/video-other.jpg')
        standalone = self.write('not-indexed.webm')
        standalone_audio = self.write('delivery-output.m4a')
        standalone_subtitle = self.write('delivery-output.srt')
        outside = self.base / 'outside.mp4'
        outside.write_bytes(b'outside')
        (self.base / 'videos.txt').write_text('youtube npNnbj46_ik\n', encoding='utf-8')
        self.catalog.reindex(self.root)
        self.store.set_downloads_paused(False)
        task = self.store.create('download', {'base': str(self.base), 'urls': ['https://youtu.be/npNnbj46_ik']})
        self.store.claim(self.store.owner)
        self.store.statistics.record_file(task['id'], video)
        self.store.finish(task['id'], result={}, worker_id=self.store.owner)
        self.store.archive_add('youtube npNnbj46_ik', str(video))
        with self.store.connect() as db:
            db.execute(delivery_artifacts.insert().values(
                id='d' * 64, seq=self.store.db.sequence(db, 'delivery_artifact'), job_id=task['id'],
                task_id=task['id'], video_id='npNnbj46_ik', title='Test', kind='audio_only', language='',
                variant='fixture', path=str(standalone_audio), size=standalone_audio.stat().st_size,
                instance_id='local', storage='local', object_key=None, available=1, test_mode=0,
                created_at='2026-01-01'))
        self.assertGreater(self.store.statistics.overview()['summary']['file_count'], 0)
        self.store.set_downloads_paused(True)
        response = self.request_clear()
        self.assertEqual(response.status_code, 202, response.json)
        done = self.wait(response.json['id'])
        self.assertEqual(done['status'], 'succeeded', done)
        self.assertGreater(done['result']['deleted_bytes'], 0)
        self.assertFalse(video.exists())
        self.assertFalse(standalone.exists())
        self.assertFalse(standalone_audio.exists())
        self.assertFalse(standalone_subtitle.exists())
        self.assertFalse(chunk.exists())
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(note.exists())
        self.assertEqual(outside.read_bytes(), b'outside')
        self.assertTrue(self.root.is_dir())
        self.assertEqual(self.client.get('/api/media').json['total'], 0)
        self.assertEqual(self.client.get('/api/overview').json['recent_media'], [])
        self.assertIsNone(self.store.get(task['id']))
        self.assertEqual(self.store.statistics.overview()['summary']['file_count'], 0)
        self.assertTrue(self.store.archive_has('youtube npNnbj46_ik'))
        self.assertEqual((self.base / 'videos.txt').read_text(), 'youtube npNnbj46_ik\n')
        with self.store.connect() as db:
            self.assertIsNone(db.execute(select(delivery_artifacts.c.available).where(
                delivery_artifacts.c.id == 'd' * 64)).scalar_one_or_none())

    def test_confirmation_csrf_and_directory_must_match(self):
        video = self.write('video.mp4')
        self.assertEqual(self.client.post('/api/media/clear', json={}).status_code, 403)
        self.assertEqual(self.request_clear(confirmation='yes').status_code, 400)
        self.assertEqual(self.request_clear(directory=str(self.base)).status_code, 400)
        self.assertTrue(video.exists())

    def test_active_downloads_and_indexing_block_cleanup(self):
        self.store.set_downloads_paused(False)
        self.assertEqual(self.request_clear().status_code, 409)
        task = self.store.create('download', {'base': str(self.base), 'urls': ['https://example.test/video.mp4']})
        self.store.claim('other-host:123:fixture')
        self.store.set_downloads_paused(True)
        self.assertEqual(self.request_clear().status_code, 409)
        self.store.finish(task['id'], error='fixture')
        self.store.create('index', {'base': str(self.base), 'config': {}})
        self.assertEqual(self.request_clear().status_code, 409)

    def test_pending_downloads_survive_and_cannot_resume_during_cleanup(self):
        task = self.store.create('download', {'base': str(self.base), 'urls': ['https://example.test/video.mp4']})
        entered, release = threading.Event(), threading.Event()
        def held_clear(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {'message': 'fixture complete'}
        with patch('webapp.media_cleanup.clear_media', side_effect=held_clear):
            response = self.request_clear()
            self.assertEqual(response.status_code, 202, response.json)
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(self.request_clear().status_code, 409)
                self.assertEqual(self.client.post('/api/media/reindex', headers=self.headers).status_code, 409)
                self.assertEqual(self.client.put('/api/config', json={'path': 'other'}, headers=self.headers).status_code, 409)
                with self.assertRaisesRegex(ValueError, '正在清空'):
                    self.store.activate_download_lists()
                self.assertIsNone(self.store.claim(actions=('download',)))
                self.assertEqual(self.store.get(task['id'])['status'], 'queued')
            finally:
                release.set()
                self.wait(response.json['id'])
        self.assertEqual(self.store.get(task['id'])['status'], 'queued')

    def test_dangerous_roots_and_links_are_rejected(self):
        for root, protected in ((Path(self.base.anchor), ()), (Path.home(), ()),
                                (self.base, (self.config,)), (self.base, (self.store.directory,))):
            with self.subTest(root=str(root)), self.assertRaises(ValueError):
                validate_clear_root(root, protected)
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(ValueError):
                validate_clear_root(self.root)

    def test_junctions_are_not_followed(self):
        other = self.root / 'junction'
        other.mkdir()
        keep = other / 'video.mp4'
        keep.write_bytes(b'keep')
        self.write('delete.mp4')
        with patch.object(Path, 'is_junction', autospec=True, side_effect=lambda path: path == other):
            with self.assertRaisesRegex(RuntimeError, '部分素材'):
                clear_media(self.catalog, self.root)
        self.assertTrue(keep.exists())
        self.assertFalse((self.root / 'delete.mp4').exists())

    def test_failed_deletions_keep_index_and_allow_retry(self):
        video = self.write('locked.mp4')
        self.catalog.reindex(self.root)
        original = Path.unlink
        def locked(path, *args, **kwargs):
            if path == video:
                raise PermissionError('fixture locked file')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', autospec=True, side_effect=locked):
            response = self.request_clear()
            done = self.wait(response.json['id'])
        self.assertEqual(done['status'], 'failed')
        self.assertIn('部分素材未能删除', done['error'])
        self.assertEqual(self.catalog.root_status(self.root)['count'], 1)
        response = self.request_clear()
        self.assertEqual(self.wait(response.json['id'])['status'], 'succeeded')
        self.assertEqual(self.catalog.root_status(self.root)['count'], 0)

    def test_orphan_locked_cover_is_removed_on_retry_without_its_video(self):
        video = self.write('video.mp4')
        cover = self.write('video.jpg')
        original = Path.unlink
        def locked(path, *args, **kwargs):
            if path == cover:
                raise PermissionError('fixture locked cover')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', autospec=True, side_effect=locked):
            with self.assertRaises(RuntimeError):
                clear_media(self.catalog, self.root)
        self.assertFalse(video.exists())
        self.assertTrue(cover.exists())
        clear_media(self.catalog, self.root)
        self.assertFalse(video.exists())
        self.assertFalse(cover.exists())


if __name__ == '__main__':
    unittest.main()
