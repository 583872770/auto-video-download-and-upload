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

    def test_background_clear_removes_nested_media_and_sidecars_but_preserves_history(self):
        video = self.write('np/npNnbj46_ik/video [npNnbj46_ik].mp4')
        for suffix in ('.jpg', '.en.vtt', '.f137.mp4', '.f140.m4a', '.mp4.part'):
            self.write('np/npNnbj46_ik/video [npNnbj46_ik]' + suffix)
        self.write('np/npNnbj46_ik/video [npNnbj46_ik].info.json', json.dumps({'title': 'Test'}).encode())
        chunk = self.write('np/npNnbj46_ik/video [npNnbj46_ik].mp4.parts/0001.bin')
        note = self.write('keep.txt')
        unrelated = self.write('np/npNnbj46_ik/video-other.jpg')
        standalone = self.write('not-indexed.webm')
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
        before = self.store.statistics.overview()['summary']
        self.store.set_downloads_paused(True)
        response = self.request_clear()
        self.assertEqual(response.status_code, 202, response.json)
        done = self.wait(response.json['id'])
        self.assertEqual(done['status'], 'succeeded', done)
        self.assertGreater(done['result']['deleted_bytes'], 0)
        self.assertFalse(video.exists())
        self.assertFalse(standalone.exists())
        self.assertFalse(chunk.exists())
        self.assertEqual(sorted(p.name for p in video.parent.iterdir()), ['video-other.jpg'])
        self.assertTrue(note.exists())
        self.assertEqual(outside.read_bytes(), b'outside')
        self.assertTrue(self.root.is_dir())
        self.assertEqual(self.client.get('/api/media').json['total'], 0)
        self.assertEqual(self.client.get('/api/overview').json['recent_media'], [])
        self.assertEqual(self.store.get(task['id'])['status'], 'succeeded')
        self.assertEqual(self.store.statistics.overview()['summary'], before)
        self.assertTrue(self.store.archive_has('youtube npNnbj46_ik'))
        self.assertEqual((self.base / 'videos.txt').read_text(), 'youtube npNnbj46_ik\n')

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
        def held_clear(*args):
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
        self.assertIn('1 个文件', done['error'])
        self.assertEqual(self.catalog.root_status(self.root)['count'], 1)
        response = self.request_clear()
        self.assertEqual(self.wait(response.json['id'])['status'], 'succeeded')
        self.assertEqual(self.catalog.root_status(self.root)['count'], 0)

    def test_locked_cover_preserves_video_until_a_successful_retry(self):
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
        self.assertTrue(video.exists())
        self.assertTrue(cover.exists())
        clear_media(self.catalog, self.root)
        self.assertFalse(video.exists())
        self.assertFalse(cover.exists())


if __name__ == '__main__':
    unittest.main()
