import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from tests.test_progress_cache import MemoryRedis
from webapp.database import settings, tasks
from webapp.progress_cache import ProgressCache
from webapp.workbench_live import live_snapshot, publish_queue, queue_details
from webapp.wait_diagnostics import start_wait


class WorkbenchLiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = Path(temp.name) / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        self.app = create_app(config, Path(temp.name) / 'state', start_worker=False)
        self.store = self.app.extensions['task_store']
        self.addCleanup(self.store.db.engine.dispose)
        self.redis = MemoryRedis()
        self.store.progress_cache = ProgressCache(self.store.db.url, client=self.redis)

    def test_live_endpoint_reads_only_cache_and_matches_current_attempt(self):
        cache = self.store.progress_cache
        task = dict(id='video', worker_id=self.store.owner, started_at='now', attempts=2,
                    action='download', video_id='fixture', title='Title')
        self.store.worker_status(self.store.owner, '0', 'running', task['id'], task=task)
        self.store.worker_status('idle', '1', 'idle')
        until = time.time() + 9
        self.store.worker_status('gate', '2', 'waiting_start', message='等待任务启动间隔',
            wait={'code': 'start_interval', 'detail': '间隔结束后会自动尝试领取。', 'until': until})
        self.store.worker_status('other', 'other-node:1', 'idle')
        progress = dict(stage='downloading', network_phase='transferring', last_bytes_at=time.time(), speed=123,
                        output_kind='subtitle', output_language='ja')
        cache.publish({**task, 'attempts': 1}, progress)
        publish_queue(self.store)
        self.assertEqual(live_snapshot(cache, 'local')['rows'][0]['state'], 'pending_start')
        cache.publish(task, dict(stage='waiting_request', request_wait_until=time.time() + 9,
                                 output_kind='subtitle', output_language='ja'))
        self.assertEqual(live_snapshot(cache, 'local')['rows'][0]['state'], 'pending_transfer')
        cache.publish(task, progress)
        # The normal authentication guard is tested separately. A live data
        # request must never open the business database, even during an outage.
        with patch('webapp.console_auth.ConsoleAuth.guard', return_value=None), patch.object(
                self.store, 'connect', side_effect=AssertionError('live SQL read')):
            client = self.app.test_client()
            data = client.get('/api/workbench/live').get_json()
            self.assertEqual(data['counts'], dict(downloading=1, pending_transfer=0, pending_start=1, disk_wait=0, idle=1))
            self.assertEqual(data['transfer_summary']['speed'], 123)
            self.assertEqual(data['rows'][2]['wait'], {'code': 'start_interval', 'label': '等待任务启动间隔',
                'detail': '间隔结束后会自动尝试领取。', 'until': until})
            cache.forget_worker(self.store.owner)
            self.assertEqual(client.get('/api/workbench/live').get_json()['counts']['downloading'], 0)
            self.redis.failed = True
            self.assertEqual(client.get('/api/workbench/live').status_code, 503)

    def test_delivery_speed_endpoint_works_without_an_active_session(self):
        with patch('webapp.console_auth.ConsoleAuth.guard', return_value=None):
            response = self.app.test_client().get('/api/workbench/delivery-speed')
        self.assertEqual(response.status_code, 200)
        history = response.get_json()
        self.assertEqual(history['end_at'] - history['start_at'], 86400)
        self.assertTrue(all(point['file_count'] == 0 for point in history['points']))

    def test_backoff_and_disk_waits_keep_actionable_details(self):
        until = time.time() + 5
        wait = start_wait('disk_coordination', until=until)
        self.store.worker_status('backoff', '0', 'waiting_start', message=wait['label'], wait=wait)
        task = dict(id='fixture', worker_id='disk', started_at='now', attempts=1, action='download')
        cache = self.store.progress_cache
        cache.publish(task, dict(stage='downloading', network_phase='transferring', speed=1000))
        self.store.worker_status('disk', '1', 'disk_wait', task['id'], task=task, message='等待磁盘空间',
            wait=dict(code='disk_wait', detail='本机临时文件总预算不足'))
        self.store.worker_status('processing', 'processing-0', 'disk_wait', 'local-video')
        self.store.worker_status('peer', hashlib.sha256(b'peer').hexdigest()[:16] + ':1', 'disk_wait', 'peer-video')
        self.store.worker_status('old', '2', 'disk_wait')
        publish_queue(self.store)
        data = live_snapshot(cache, 'local')
        backoff, disk, *_ = data['rows']
        self.assertEqual(backoff['wait'], wait)
        self.assertEqual(disk['state'], 'disk_wait')
        self.assertIsNone(disk['progress']['speed'])
        self.assertEqual(disk['wait']['detail'], '本机临时文件总预算不足')
        self.assertEqual(data['disk_waiting'], dict(download=1, processing=1))
        from webapp.workbench import compact_status
        self.assertEqual(compact_status({}, cache)['disk_waiting'], data['disk_waiting'])
        cache.forget_worker('disk')
        self.assertEqual(live_snapshot(cache, 'local')['disk_waiting'], dict(download=0, processing=1))

    def test_queue_summary_groups_match_paginated_task_details(self):
        stamp = time.time()
        batch = 'fixture-batch'
        with self.store.connect() as db:
            db.execute(self.store.db.upsert(settings, {'key': 'selected_download_job', 'value': batch}, ['key']))
        for index in range(23):
            task = self.store.create('download', {'batch_id': batch, 'urls': [f'https://youtu.be/{index:011d}']})
            with self.store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                    progress='{"stage":"disk_wait"}' if index == 0 else '{}',
                    available_at=stamp+300 if index == 1 else 0))

        publish_queue(self.store)
        self.assertEqual(live_snapshot(self.store.progress_cache, 'local')['queue'],
                         dict(processing=0, retry=1, ready=22))
        first = queue_details(self.store, 'local', 'ready', 0)
        second = queue_details(self.store, 'local', 'ready', first['next_cursor'])
        self.assertEqual((len(first['items']), len(second['items']), second['next_cursor']), (20, 2, None))
