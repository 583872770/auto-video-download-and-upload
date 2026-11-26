import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from tests.test_progress_cache import MemoryRedis
from webapp.database import counters, notifications, notification_tasks, settings, tasks
from webapp.progress_cache import ProgressCache
from webapp.workbench_live import live_snapshot, publish_queue, queue_details


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
        self.store.worker_status('gate', '2', 'waiting_start', message='等待账号容量或启动间隔')
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
            self.assertEqual(data['counts'], dict(downloading=1, pending_transfer=0, pending_start=1, idle=1))
            self.assertEqual(data['transfer_summary']['speed'], 123)
            cache.forget_worker(self.store.owner)
            self.assertEqual(client.get('/api/workbench/live').get_json()['counts']['downloading'], 0)
            self.redis.failed = True
            self.assertEqual(client.get('/api/workbench/live').status_code, 503)

    def test_queue_summary_groups_match_paginated_task_details(self):
        stamp = time.time()
        batch = 'fixture-batch'
        with self.store.connect() as db:
            db.execute(self.store.db.upsert(settings, {'key': 'selected_download_job', 'value': batch}, ['key']))
            db.execute(counters.insert().values(scope='batch:' + batch, status='queued', value=23))
            db.execute(notifications.insert().values(id='disk', instance_id='local', job_id=batch, code='disk',
                status='action', active_count=1, action_count=1, first_seen=stamp, updated_at=stamp, detail='{}'))
        for index in range(23):
            task = self.store.create('download', {'urls': [f'https://youtu.be/{index:011d}']})
            progress = {'stage': 'disk_wait'} if index == 0 else {}
            with self.store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                    batch_id=batch, progress=json.dumps(progress), available_at=stamp+300 if index == 1 else 0))
                if index == 0:
                    db.execute(notification_tasks.insert().values(task_id=task['id'], group_id='disk', state='action',
                        first_seen=stamp, updated_at=stamp, detail='{}'))
        publish_queue(self.store)
        summary = live_snapshot(self.store.progress_cache, 'local')['queue']
        self.assertEqual(summary, dict(processing=0, disk=1, retry=1, ready=21))
        first = queue_details(self.store, 'local', 'ready', 0)
        second = queue_details(self.store, 'local', 'ready', first['next_cursor'])
        self.assertEqual((len(first['items']), len(second['items']), second['next_cursor']), (20, 1, None))
