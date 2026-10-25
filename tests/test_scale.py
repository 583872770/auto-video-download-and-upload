"""Scale foundations: import checkpoints, indexed listings, leases and concurrency."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from sqlalchemy import event, select

from app import create_app
from webapp.bulk import CHUNK, run_import
from webapp.database import imports, tasks
from webapp.store import TaskStore


class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        self.app = create_app(self.config, self.root / 'state', start_worker=False)
        self.client = self.app.test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]
        self.headers = {'X-CSRF-Token': token}
        self.store = self.app.extensions['task_store']
        self.catalog = self.app.extensions['media_catalog']

    def upload(self, content, name='videos.txt'):
        response = self.client.post('/api/downloads/import', data={'file': (io.BytesIO(content), name)}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        return self.store.get(response.json['id'], private=True)

    def test_stream_import_csv_deduplicates_and_invalid_records_do_not_block_valid_rows(self):
        task = self.upload(b'title,video_id\nfirst,npNnbj46_ik\nagain,npNnbj46_ik\nbad,INVALID\nnext,2Gu7q-KUXCk\n', 'list.csv')
        result = run_import(self.store, task['payload'], lambda value: None)
        self.assertEqual((result['added'], result['duplicates'], result['invalid']), (2, 1, 1))
        self.assertEqual(self.client.get('/api/tasks?q=npNnbj46_ik').json['items'][0]['video_id'], 'npNnbj46_ik')
        self.assertEqual(self.store.counts(actions=['download'])['total'], 2)
        repeated = run_import(self.store, task['payload'], lambda value: None)
        self.assertEqual(repeated['added'], 2)
        self.assertEqual(self.store.counts(actions=['download'])['total'], 2)
        another = self.upload(b'npNnbj46_ik\nWMu6ANTg8mk\n')
        result = run_import(self.store, another['payload'], lambda value: None)
        self.assertEqual((result['added'], result['duplicates']), (1, 1))

    def test_import_resumes_after_committed_chunk_without_recreating_tasks(self):
        total = CHUNK * 2 + 200
        task = self.upload(('\n'.join(f'{index:011d}' for index in range(total)) + '\n').encode())

        def interruption(value):
            raise RuntimeError('simulated worker exit after commit')

        with self.assertRaisesRegex(RuntimeError, 'simulated worker exit'):
            run_import(self.store, task['payload'], interruption)
        self.assertEqual(self.store.counts(actions=['download'])['total'], CHUNK)
        result = run_import(self.store, task['payload'], lambda value: None)
        self.assertEqual(result['added'], total)
        self.assertEqual(self.store.counts(actions=['download'])['total'], total)
        self.assertEqual(result['duplicates'], 0)

    def test_import_reuses_connection_and_reports_final_counts_and_speed(self):
        total = CHUNK * 2 + 200
        task = self.upload(('\n'.join(f'{index:011d}' for index in range(total)) + '\n').encode())
        reports, connections = [], []
        def connected(dbapi, record):
            connections.append(record)
        event.listen(self.store.db.engine, 'connect', connected)
        try:
            with patch('webapp.bulk.time.monotonic', side_effect=[10.0, 10.2, 10.4, 10.6]):
                result = run_import(self.store, task['payload'], reports.append)
        finally:
            event.remove(self.store.db.engine, 'connect', connected)
        self.assertLessEqual(len(connections), 2)
        self.assertEqual([report['processed'] for report in reports], [CHUNK, total])
        self.assertEqual((reports[-1]['percent'], reports[-1]['eta']), (100, 0))
        self.assertGreater(reports[0]['records_per_second'], 0)
        self.assertGreater(reports[0]['speed'], 0)
        self.assertEqual(result['added'], total)
        restored = self.store.get(self.store.page(actions=['download'], limit=1)['items'][0]['id'], private=True)
        self.assertNotIn('config', restored['payload'])
        self.assertNotIn('config', task['payload'])
        self.assertEqual(restored['payload']['batch_id'], task['payload']['import_id'])

    def test_failed_import_chunk_rolls_back_tasks_reservations_counters_and_checkpoint(self):
        total = CHUNK + 10
        task = self.upload(('\n'.join(f'{index:011d}' for index in range(total)) + '\n').encode())
        insert = self.store.insert_downloads
        def fail_after_insert(*args, **kwargs):
            insert(*args, **kwargs)
            raise RuntimeError('interruption before checkpoint')
        with patch.object(self.store, 'insert_downloads', side_effect=fail_after_insert):
            with self.assertRaisesRegex(RuntimeError, 'before checkpoint'):
                run_import(self.store, task['payload'], lambda value: None)
        self.assertEqual(self.store.counts(actions=['download'])['total'], 0)
        with self.store.connect() as db:
            offset = db.execute(select(imports.c.offset).where(imports.c.id == task['payload']['import_id'])).scalar_one()
        self.assertEqual(offset, 0)
        result = run_import(self.store, task['payload'], lambda value: None)
        self.assertEqual((result['added'], result['duplicates']), (total, 0))

    def test_task_keyset_pages_are_stable_when_new_tasks_arrive(self):
        payload = {'config': {}, 'base': str(self.root), 'batch_id': 'a' * 32}
        created = self.store.create_batch([{**payload, 'urls': [f'https://www.youtube.com/watch?v={i:011d}']} for i in range(70)])
        first = self.client.get('/api/tasks?limit=25&scope=download').json
        self.store.create('check', {'config': {}, 'base': str(self.root)})
        second = self.client.get('/api/tasks?limit=25&scope=download&cursor=' + first['next_cursor']).json
        third = self.client.get('/api/tasks?limit=25&scope=download&cursor=' + second['next_cursor']).json
        ids = [task['id'] for page in (first, second, third) for task in page['items']]
        self.assertEqual(len(ids), 70)
        self.assertEqual(len(set(ids)), 70)
        found = self.client.get('/api/tasks?q=00000000012').json['items']
        self.assertEqual([task['video_id'] for task in found], ['00000000012'])
        self.assertEqual(self.client.get('/api/tasks?limit=1000').status_code, 400)
        batch = self.client.get('/api/batches').json['items'][0]
        self.assertEqual(batch['counts']['total'], 70)

    def test_media_listing_uses_index_without_scanning_and_supports_sharded_ranges(self):
        root = self.root / 'videos'
        folder = root / 'np' / 'npNnbj46_ik'
        folder.mkdir(parents=True)
        video = folder / 'video.mp4'
        video.write_bytes(b'0123456789')
        video.with_suffix('.info.json').write_text(json.dumps({'title': 'Example title', 'id': 'npNnbj46_ik'}))
        self.catalog.index_file(video, root)
        with patch('webapp.catalog.os.scandir', side_effect=AssertionError('listing must not scan')):
            response = self.client.get('/api/media?q=Example')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['total'], 1)
            self.assertEqual(response.json['items'][0]['name'], 'np/npNnbj46_ik/video.mp4')
        with self.client.get('/media/videos/np/npNnbj46_ik/video.mp4', headers={'Range': 'bytes=2-5'}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, b'2345')
        video.unlink()
        self.catalog.reindex(root)
        self.assertEqual(self.client.get('/api/media').json['total'], 0)

    def test_expired_lease_recovery_fences_old_owner_and_finish_is_idempotent(self):
        task = self.store.create('download', {'config': {}, 'base': str(self.root), 'urls': ['https://example.test/video.mp4']})
        self.store.claim('old-owner', lease_seconds=-1)
        self.store.update_progress(task['id'], {'rotation_id': 'generation', 'stage': 'downloading'}, 'old-owner')
        self.assertEqual(self.store.recover(expired_only=True), 1)
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(available_at=0))
        claimed = self.store.claim('new-owner')
        self.assertEqual(claimed['id'], task['id'])
        self.assertFalse(self.store.finish(task['id'], result={'message': 'stale'}, worker_id='old-owner'))
        self.assertFalse(self.store.update_progress(task['id'], {}, 'old-owner'))
        self.assertTrue(self.store.finish(task['id'], result={'message': 'done'}, worker_id='new-owner'))
        self.assertFalse(self.store.finish(task['id'], result={}, worker_id='new-owner'))
        self.assertEqual(self.store.rotation_completed('generation'), 1)
        self.assertEqual(self.store.counts()['succeeded'], 1)

    def test_concurrent_claims_do_not_execute_the_same_task_twice(self):
        payload = {'config': {}, 'base': str(self.root)}
        self.store.create_batch([{**payload, 'urls': [f'https://example.test/{i}.mp4']} for i in range(30)])

        def work(number):
            claimed = []
            while task := self.store.claim(f'worker-{number}'):
                claimed.append(task['id'])
                self.store.finish(task['id'], result={'message': 'done'}, worker_id=f'worker-{number}')
            return claimed

        with ThreadPoolExecutor(4) as pool:
            ids = sum(pool.map(work, range(4)), [])
        self.assertEqual(len(ids), 30)
        self.assertEqual(len(set(ids)), 30)
        self.assertEqual(self.store.counts()['succeeded'], 30)

    def test_network_retries_back_off_but_permanent_failures_finish(self):
        self.client.put('/api/config', json={'download_attempts': 2}, headers=self.headers)
        task = self.store.create('download', {'config': {'download_attempts': 2}, 'base': str(self.root)})
        self.store.claim('worker')
        self.store.finish(task['id'], error='HTTP Error 503', retryable=True, worker_id='worker')
        current = self.store.get(task['id'])
        self.assertEqual(current['status'], 'queued')
        self.assertGreater(current['available_at'], time.time())
        self.assertIsNone(self.store.claim('worker'))
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(available_at=0))
        self.store.claim('worker')
        self.store.finish(task['id'], error='HTTP Error 503', retryable=True, worker_id='worker')
        self.assertEqual(self.store.get(task['id'])['status'], 'failed')


