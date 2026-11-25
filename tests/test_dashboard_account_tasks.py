"""The worker status endpoint must include task identity and live progress for account filters."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app import create_app
from webapp.database import tasks
from webapp.config.settings import save_settings


class DashboardAccountTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\n'
                          'youtube_cookie_pool: [a, b]\n', encoding='utf-8')
        self.app = create_app(config, root / 'state', start_worker=False, role='hybrid',
                              instance_id='node', database_url='sqlite:///' + (root / 'test.db').as_posix())
        self.store = self.app.extensions['task_store']
        self.addCleanup(self.store.db.engine.dispose)
        local = self.store.fleet.local_settings()
        settings = {**local['config'], 'youtube_cookie_pool': ['a', 'b']}
        save_settings(config, settings)
        self.store.fleet.save_local(settings, local['base'], local['clash_directory'])
        self.client = self.app.test_client()

    def task(self, *, account='a', stage='extracting', status='running', instance='node', expired=False, details=None):
        task = self.store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']})
        progress = {'stage': stage, 'title': 'Task title', **(details or {})}
        if account:
            progress['cookie_profile_id'] = account
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status=status, instance_id=instance, worker_id='fixture',
                lease_until=time.time() + (-60 if expired else 600), progress=json.dumps(progress)))
        return task['id']

    def allocation(self):
        response = self.client.get('/api/status', headers={'X-Instance-ID': 'node'})
        self.assertEqual(response.status_code, 200)
        return response.json['concurrency']['cookie_allocation']

    def test_status_groups_multiple_tasks_and_unassigned_without_other_instance_or_expired_leases(self):
        first = self.task(stage='downloading')
        second = self.task(stage='waiting_request')
        unassigned = self.task(account=None, stage='waiting_cookie')
        self.task(account='b', status='succeeded')
        self.task(account='b', expired=True)
        self.task(account='b', instance='another-node')
        result = self.allocation()
        accounts = {account['id']: account for account in result['accounts']}
        self.assertEqual({task['task_id'] for task in accounts['a']['tasks']}, {first, second})
        self.assertNotIn('unassigned', [reason['code'] for reason in accounts['a']['reasons']])
        self.assertEqual(accounts['b']['tasks'], [])
        self.assertEqual([task['task_id'] for task in result['unassigned_tasks']], [unassigned])

    def test_cached_progress_drives_account_stage_title_and_speed(self):
        task_id = self.task()
        live = {'stage': 'downloading', 'cookie_profile_id': 'b', 'title': 'Live title',
                'speed': 12345, '_reported_at': time.time(), 'last_bytes_at': time.time()}
        for kind in ('video_only', 'subtitle'):
            with self.subTest(kind=kind):
                live['output_kind'] = kind
                with patch.object(self.store.progress_cache, 'read_many', return_value={task_id: {'progress': live}}):
                    result = self.allocation()
                accounts = {account['id']: account for account in result['accounts']}
                self.assertEqual(accounts['a']['tasks'], [])
                task = accounts['b']['tasks'][0]
                self.assertEqual((task['task_id'], task['stage'], task['title'], task['speed']),
                                 (task_id, 'downloading', 'Live title', 12345))
                self.assertIsNone(task['wait'])

    def test_queued_disk_wait_keeps_account_and_countdown_but_is_not_a_running_process(self):
        from webapp.notifications import settle_task
        from sqlalchemy import select
        identity = self.task(stage='disk_wait', status='queued', details={
            'retry_at': time.time()+60, 'disk_pressure': dict(kind='budget',
                requested_bytes=2*1024**3, allocatable_bytes=1024**3)})
        with self.store.connect() as db:
            row = db.execute(select(tasks).where(tasks.c.id == identity)).mappings().one()
            settle_task(self.store, db, row, error='临时预算不足', resource=True,
                progress=json.loads(row['progress']), retry_at=time.time()+60)
        response = self.client.get('/api/status', headers={'X-Instance-ID': 'node'})
        result = response.json['concurrency']
        task = result['cookie_allocation']['accounts'][0]['tasks'][0]
        self.assertEqual(task['task_id'], identity)
        self.assertEqual(task['wait']['label'], '等待临时预算')
        self.assertIn('2.0 GiB', task['wait']['detail'])
        self.assertGreater(task['wait']['until'], time.time())
        self.assertEqual(result['running'], 0)
        self.assertEqual(result['transfer_allocation']['occupied'], [])

    def test_missing_cache_falls_back_to_persisted_task_stage(self):
        task_id = self.task(stage='processing')
        with patch.object(self.store.progress_cache, 'read_many', return_value={}):
            result = self.allocation()
        task = result['accounts'][0]['tasks'][0]
        self.assertEqual((task['task_id'], task['stage']), (task_id, 'processing'))

    def test_live_wait_explains_resource_and_disappears_after_transfer_starts(self):
        task_id = self.task(stage='waiting_request')
        live = {'stage': 'waiting_transfer', 'cookie_profile_id': 'a',
                'transfer_wait_resource': 'connections', 'speed': 99999}
        with patch.object(self.store.progress_cache, 'read_many', return_value={task_id: {'progress': live}}):
            waiting = self.allocation()['accounts'][0]['tasks'][0]
        self.assertIn('服务器资源', waiting['wait']['label'])
        self.assertIsNone(waiting['speed'])
        live.update(stage='downloading', _reported_at=time.time(), last_bytes_at=time.time())
        with patch.object(self.store.progress_cache, 'read_many', return_value={task_id: {'progress': live}}):
            running = self.allocation()['accounts'][0]['tasks'][0]
        self.assertIsNone(running['wait'])
        self.assertEqual(running['speed'], 99999)

    def test_unassigned_wait_keeps_reported_reason_and_retry_time_without_secrets(self):
        deadline = time.time() + 120
        self.task(account=None, stage='scheduling_wait', details={
            'wait_reason': '等待上游恢复 https://media.test/file?signature=PRIVATE',
            'retry_at': deadline})
        waiting = self.allocation()['unassigned_tasks'][0]['wait']
        self.assertIn('等待上游恢复', waiting['detail'])
        self.assertNotIn('PRIVATE', waiting['detail'])
        self.assertEqual(waiting['until'], deadline)

    def test_request_interval_only_uses_a_current_applicable_deadline(self):
        from webapp.wait_diagnostics import task_wait
        states = {}
        progress = {'stage': 'waiting_request'}
        waiting = task_wait(progress, states=states, identity='node', stamp=100)
        self.assertEqual(waiting['code'], 'file_interval')
        self.assertNotIn('until', waiting)
        progress['request_wait_until'] = 115
        self.assertEqual(task_wait(progress, states=states, identity='node', stamp=100)['until'], 115)
        self.assertNotIn('until', task_wait(progress, states=states, identity='node', stamp=116))

    def test_file_sleep_reports_its_actual_deadline_and_clears_unrecognized_durations(self):
        from types import SimpleNamespace
        from webapp.delivery.executor import DeliveryDownloader
        from yt_dlp.downloader.http import HttpFD
        from webapp.wait_diagnostics import task_wait
        progress = SimpleNamespace(state={}, save=lambda **kwargs: None)
        session = SimpleNamespace(
            config={}, base=Path('.'), payload={}, store=None, progress=progress,
            test_mode=False, oss_mode=False, current_video={}, output_files=set(),
            existing_files=set(), deferred_archives={})
        with DeliveryDownloader(session, {'quiet': True}) as downloader, patch('webapp.transfer_observation.time.time', return_value=100):
            fd = HttpFD(downloader, downloader.params)
            with downloader.file_observation(fd, 'unused-file'):
                downloader.to_screen('[download] Sleeping 12.50 seconds as required by the site...')
                waiting = task_wait(progress.state, stamp=100)
                self.assertEqual(waiting['code'], 'file_interval')
                self.assertEqual(waiting['until'], 112.5)
                downloader.to_screen('[download] Sleeping unknown duration...')
                self.assertNotIn('until', task_wait(progress.state, stamp=100))

    def test_source_health_wait_shows_its_deadline(self):
        from webapp.wait_diagnostics import task_wait
        waiting = task_wait({'stage': 'waiting_transfer', 'transfer_wait_resource': 'source:youtube'},
                            states={'source:youtube': {'cooldown_until': 120}}, stamp=100)
        self.assertEqual(waiting['code'], 'source_cooldown')
        self.assertEqual(waiting['until'], 120)
        capacity = task_wait({'stage': 'waiting_transfer', 'transfer_wait_resource': 'resources'}, stamp=100)
        self.assertEqual(capacity['code'], 'resource_wait')
        self.assertNotIn('until', capacity)

    def test_all_output_types_show_the_common_file_start_countdown(self):
        from webapp.wait_diagnostics import task_wait
        for kind in ('video_only', 'subtitle'):
            task_id=self.task(stage='waiting_request', details={'output_kind':kind,'request_wait_reason':'file_start','request_wait_until':time.time()+12})
            waiting=next(task for task in self.allocation()['accounts'][0]['tasks'] if task['task_id']==task_id)['wait']
            self.assertEqual(waiting['label'],'下载前请求间隔')
            self.assertIn('until',waiting)
            self.assertNotIn('until',task_wait({'stage':'waiting_request','request_wait_reason':'file_start',
                                               'request_wait_until':90},stamp=100))


if __name__ == '__main__':
    unittest.main()
