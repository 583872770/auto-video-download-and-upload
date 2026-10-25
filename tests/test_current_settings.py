"""Pending lists bind the current Web settings only when a download starts."""
from io import BytesIO
import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests import test_scale
from webapp.bulk import run_import
from webapp.database import snapshots, tasks
from webapp.store import TaskStore
from web_worker import execute


class CurrentSettingsTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def save(self, **values):
        response = self.client.put('/api/config', json=values, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)

    def upload(self, content=b'npNnbj46_ik\n2Gu7q-KUXCk\n'):
        response = self.client.post('/api/downloads/import',
            data={'file': (BytesIO(content), 'videos.txt')}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        return self.store.get(response.json['id'], private=True)

    def test_import_needs_no_cookie_browser_or_proxy_and_stores_only_input(self):
        self.save(download_method='yt5s_fallback', proxy_mode='clash')
        with patch('app.require_browser_environment', side_effect=AssertionError('no browser')), \
                patch('app.require_fallback_environment', side_effect=AssertionError('no tools')), \
                patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie')), \
                patch.object(self.app.extensions['clash'], 'snapshot', side_effect=AssertionError('no proxy')):
            task = self.upload(b'npNnbj46_ik\nhttps://www.youtube.com/playlist?list=PLfixture\n')
            run_import(self.store, task['payload'], lambda value: None)
        self.assertNotIn('config', task['payload'])
        self.assertNotIn('clash', task['payload'])
        for row in self.store.page(actions=('download',))['items']:
            payload = self.store.get(row['id'], private=True)['payload']
            self.assertNotIn('config', payload)
            self.assertNotIn('clash', payload)
        self.assertEqual(self.store.counts(actions=('download',))['total'], 2)

    def test_old_queue_uses_all_current_settings_without_rewriting_pending_rows(self):
        old = self.store.create('download', {'config': {'download_method': 'yt5s', 'path': 'old',
            'storage_mode': 'local', 'download_attempts': 2}, 'base': str(self.root),
            'clash': {'directory': 'old', 'node': 'removed'}, 'urls': ['https://youtu.be/npNnbj46_ik']})
        self.store.set_downloads_paused(True)
        with self.store.connect() as db:
            before = list(db.execute(select(tasks)).all())
            old_snapshots = list(db.execute(select(snapshots)).all())
        self.save(download_method='downmp3', path='new-videos', dir='customer', downmp3_quality='1080',
                  downmp3_timeout=90, download_attempts=7, fragment_concurrency=1,
                  shard_downloads=False, proxy_mode='manual', proxy='http://127.0.0.1:9000')
        with self.store.connect() as db:
            self.assertEqual(list(db.execute(select(tasks)).all()), before)
            self.assertEqual(list(db.execute(select(snapshots)).all()), old_snapshots)
        # A separate worker process reads the shared database settings.
        worker = TaskStore(self.store.directory)
        worker.set_downloads_paused(False)
        task = worker.claim(worker.owner)
        self.assertEqual(task['id'], old['id'])
        config = task['payload']['config']
        self.assertEqual((config['download_method'], config['path'], config['dir']),
                         ('downmp3', 'new-videos', 'customer'))
        self.assertEqual((config['downmp3_quality'], config['downmp3_timeout'], task['max_attempts']), ('1080', 90, 7))
        self.assertEqual((config['fragment_concurrency'], config['shard_downloads'], config['proxy_mode']),
                         (1, False, 'manual'))
        self.assertNotIn('clash', task['payload'])
        rows = {row['method']: row for row in worker.statistics.method_summary(task['payload']['download_session_id'])}
        self.assertEqual(rows['downmp3']['running'], 1)
        self.assertEqual(rows['yt5s']['attempted'], 0)

    def test_running_attempt_keeps_config_and_retry_reads_new_settings(self):
        task = self.upload(b'npNnbj46_ik\n')
        run_import(self.store, task['payload'], lambda value: None)
        self.store.finish(task['id'], result={})
        self.save(download_method='yt5s', path='before')
        first = self.store.claim(self.store.owner, actions=('download',))
        self.save(download_method='downmp3', path='after', download_attempts=8)
        with patch('web_worker._execute', return_value={'message': 'fixture'}) as work:
            execute('download', first['payload'], store=self.store, task_id=first['id'])
        self.assertEqual(work.call_args.args[1]['config']['download_method'], 'yt5s')
        self.assertEqual(work.call_args.args[1]['config']['path'], 'before')
        self.store.release(first['id'], self.store.owner, 'fixture pause', delay=0)
        again = self.store.claim(self.store.owner, actions=('download',))
        self.assertEqual(again['id'], first['id'])
        self.assertEqual(again['payload']['config']['download_method'], 'downmp3')
        self.assertTrue(again['payload']['_restart_transfer'])
        self.assertEqual(again['max_attempts'], 8)
        self.store.finish(again['id'], result={}, worker_id=self.store.owner)
        rows = {row['method']: row for row in self.store.statistics.method_summary(again['payload']['download_session_id'])}
        self.assertEqual((rows['downmp3']['succeeded'], rows['downmp3']['running'], rows['yt5s']['running']), (1, 0, 0))

    def test_resume_preflight_uses_current_method_not_old_queue_or_connection_override(self):
        old = self.store.create('download', {'config': {'download_method': 'yt5s', 'path': 'old'},
            'base': str(self.root), 'urls': ['https://youtu.be/npNnbj46_ik']})
        self.store.set_downloads_paused(True)
        self.save(download_method='downmp3', storage_mode='test')
        with patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie')), \
                patch('app.require_downmp3_environment') as probe:
            response = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        probe.assert_called_once()
        task = self.store.claim(self.store.owner)
        self.assertEqual(task['id'], old['id'])
        self.assertEqual(task['payload']['config']['storage_mode'], 'test')
        self.assertEqual(task['payload']['config']['download_method'], 'downmp3')

    def test_method_restrictions_are_checked_at_execution_after_import(self):
        imported = self.upload(b'https://www.youtube.com/playlist?list=PLfixture\n')
        run_import(self.store, imported['payload'], lambda value: None)
        self.store.finish(imported['id'], result={})
        self.save(download_method='downmp3')
        task = self.store.claim(self.store.owner, actions=('download',))
        with patch('webapp.downmp3.resolve') as resolve:
            with self.assertRaisesRegex(ValueError, 'downmp3'):
                execute('download', task['payload'], store=self.store, task_id=task['id'])
        resolve.assert_not_called()

    def test_old_import_snapshot_is_not_inherited_by_new_downloads(self):
        imported = self.upload()
        legacy = {**imported['payload'], 'config': {'download_method': 'yt5s', 'path': 'old'},
                  'clash': {'directory': 'old', 'node': 'deleted'}}
        run_import(self.store, legacy, lambda value: None)
        for row in self.store.page(actions=('download',))['items']:
            payload = self.store.get(row['id'], private=True)['payload']
            self.assertNotIn('config', payload)
            self.assertNotIn('clash', payload)
