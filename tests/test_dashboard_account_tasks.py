"""The worker status endpoint must include task identity and live progress for account filters."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app import create_app
from webapp.database import tasks
from webapp.settings import save_settings


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

    def task(self, *, account='a', stage='extracting', status='running', instance='node', expired=False):
        task = self.store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']})
        progress = {'stage': stage, 'title': 'Task title'}
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
        with patch.object(self.store.progress_cache, 'read_many', return_value={task_id: {'progress': live}}):
            result = self.allocation()
        accounts = {account['id']: account for account in result['accounts']}
        self.assertEqual(accounts['a']['tasks'], [])
        task = accounts['b']['tasks'][0]
        self.assertEqual((task['task_id'], task['stage'], task['title'], task['speed']),
                         (task_id, 'downloading', 'Live title', 12345))

    def test_missing_cache_falls_back_to_persisted_task_stage(self):
        task_id = self.task(stage='processing')
        with patch.object(self.store.progress_cache, 'read_many', return_value={}):
            result = self.allocation()
        task = result['accounts'][0]['tasks'][0]
        self.assertEqual((task['task_id'], task['stage']), (task_id, 'processing'))


if __name__ == '__main__':
    unittest.main()
