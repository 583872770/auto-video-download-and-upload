"""Incident lifecycle, grouped counts, delivery gaps and bounded UI reads."""
import json
import time
import unittest
from unittest.mock import patch

from sqlalchemy import event, select

from tests import test_scale
from webapp.database import (notifications, notification_tasks, tasks, instances,
    settings, delivery_requirements, delivery_runs)
from webapp.notifications import settle_task


class NotificationTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def create_job(self, urls='SjePkzdiyWU', outputs=None):
        response = self.client.post('/api/jobs', json={'customer': '通知测试', 'name': '多产物任务',
            'urls': urls, 'delivery': {'outputs': outputs or ['video_only', 'audio_only']}}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        identity = response.json['id']
        from webapp.jobs import Jobs
        Jobs(self.store).control(identity, 'start', self.store.runtime(), {})
        return identity

    def page(self, **query):
        response = self.client.get('/api/notifications', query_string=query)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def fail(self, **kwargs):
        task = self.store.claim(self.store.owner, actions=['download'])
        self.assertIsNotNone(task)
        self.store.update_progress(task['id'], {'stage': 'downloading', 'last_failure': {
            'kind': 'access_denied', 'http_status': 403, 'phase': 'transfer'}}, self.store.owner)
        self.assertTrue(self.store.finish(task['id'], error='HTTP Error 403: Forbidden', worker_id=self.store.owner, **kwargs))
        return task

    def test_failures_group_persist_and_retry_does_not_pretend_to_be_resolved(self):
        job = self.create_job('SjePkzdiyWU\nITNqgQkWpGM')
        first, second = self.fail(), self.fail()
        page = self.page()
        self.assertEqual(page['counts'], {'action': 1, 'recovering': 0, 'resolved': 0})
        group = page['items'][0]
        self.assertEqual(group['active_count'], 2)
        self.assertEqual(group['detail']['phase'], '下载传输')
        self.assertEqual(group['detail']['http_status'], 403)
        self.assertEqual(self.page()['items'][0]['id'], group['id'], 'opening must not dismiss a problem')
        affected = self.client.get('/api/notifications/' + group['id'] + '/tasks').json
        self.assertEqual({r['task_id'] for r in affected['items']}, {first['id'], second['id']})
        response = self.client.post(f'/api/jobs/{job}/redeliver', json={'ids': [second['id']]}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        # One task remains failed, so the group still needs attention.
        self.assertEqual(self.page()['counts']['action'], 1)
        again = self.store.claim(self.store.owner, actions=['download'])
        self.assertEqual(again['id'], second['id'])
        # Isolate a successful settlement from a delivery pipeline fixture.
        with self.store.connect() as db:
            db.execute(delivery_runs.delete().where(delivery_runs.c.task_id == second['id']))
        self.store.finish(second['id'], result={'file_count': 1}, worker_id=self.store.owner)
        remaining = self.page()['items'][0]
        self.assertEqual(remaining['active_count'], 1)
        self.assertEqual(remaining['detail']['task_id'], first['id'])

    def test_disk_wait_shows_completed_and_missing_files_and_real_phase(self):
        self.create_job()
        task = self.store.claim(self.store.owner, actions=['download'])
        with self.store.connect() as db:
            for kind, state in [('video_only', 'done'), ('audio_only', 'pending')]:
                db.execute(delivery_requirements.insert().values(task_id=task['id'], key=kind,
                    kind=kind, language='', variant='v', status=state, updated_at='2026-11-16T00:00:00+08:00'))
        self.store.update_progress(task['id'], {'stage': 'processing'}, self.store.owner)
        self.store.release(task['id'], self.store.owner, '临时总预算不足；实际剩余 57 GB。', resource=True)
        detail = self.page()['items'][0]['detail']
        self.assertEqual(detail['completed'], ['无声视频'])
        self.assertEqual(detail['missing'], ['纯音频'])
        self.assertEqual(detail['phase'], '本地处理')
        self.assertIn('预算不足', detail['reason'])
        self.assertEqual(self.store.get(task['id'])['attempts'], 0)

    def test_auto_retry_countdown_escalates_after_thirty_minutes(self):
        self.create_job()
        task = self.fail(retryable=True, retry_after=60)
        automatic = self.page(state='recovering')['items'][0]
        self.assertGreater(automatic['detail']['retry_at'], time.time())
        self.assertEqual(self.page()['counts']['action'], 0)
        with self.store.connect() as db:
            db.execute(notification_tasks.update().where(notification_tasks.c.task_id == task['id']).values(
                first_seen=time.time() - 1900, escalate_at=time.time() - 1))
        promoted = self.page()['items'][0]
        self.assertTrue(promoted['detail']['prolonged'])
        self.assertEqual(promoted['status'], 'action')

    def test_auth_failure_needs_attention_even_when_retry_is_queued(self):
        self.create_job()
        task = self.store.claim(self.store.owner, actions=['download'])
        self.store.update_progress(task['id'], {'stage': 'checking_login', 'last_failure': {
            'kind': 'authentication_required', 'phase': 'auth'}}, self.store.owner)
        self.store.finish(task['id'], error='需要登录验证', retryable=True, worker_id=self.store.owner)
        item = self.page()['items'][0]
        self.assertEqual(item['code'], 'account_auth')
        self.assertEqual(item['detail']['phase'], '检查登录')

    def test_routine_start_pacing_does_not_create_error_notifications(self):
        self.create_job()
        task = self.store.claim(self.store.owner, actions=['download'])
        self.store.update_progress(task['id'], {'stage': 'scheduling_wait'}, self.store.owner)
        self.store.release(task['id'], self.store.owner, 'Cookie 账号池正在按账号并发及请求间隔等待。', delay=2)
        self.assertEqual(self.page()['counts']['action'], 0)
        self.assertEqual(self.page()['counts']['recovering'], 0)

    def test_source_missing_is_visible_and_acceptance_resolves_it(self):
        job = self.create_job(outputs=['subtitle'])
        task = self.store.claim(self.store.owner, actions=['download'])
        from webapp.delivery import artifacts
        with self.store.connect() as db:
            spec = json.loads(db.execute(select(delivery_runs.c.spec).where(delivery_runs.c.task_id == task['id'])).scalar_one())
            key = artifacts.requirement(self.store, db, task['id'], spec, 'subtitle', 'en')
        artifacts.mark(self.store, task['id'], key, 'missing', '来源没有英文字幕。')
        item = self.page()['items'][0]
        self.assertEqual(item['code'], 'delivery')
        self.assertEqual(item['detail']['missing'], ['字幕 · en'])
        self.store.finish(task['id'], result={'file_count': 0}, worker_id=self.store.owner)
        response = self.client.post(f'/api/jobs/{job}/requirements/{task["id"]}/{key}/waive',
            json={'reason': '确认接受来源缺失'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.page()['counts']['action'], 0)
        self.assertEqual(self.page(state='resolved')['counts']['resolved'], 1)

    def test_group_filters_keep_global_counts_and_names_are_current(self):
        self.create_job()
        task = self.fail()
        page = self.page(instance_id='not-this-node')
        self.assertEqual(page['counts']['action'], 0)
        self.assertEqual(page['global_counts']['action'], 1)
        with self.store.connect() as db:
            from webapp.database import download_lists
            db.execute(download_lists.update().values(name='新任务名称').where(download_lists.c.id == task['batch_id']))
        self.assertEqual(self.page()['items'][0]['job_name'], '新任务名称')

    def test_removed_or_deleted_tasks_do_not_leave_active_notifications(self):
        job = self.create_job()
        task = self.fail()
        response = self.client.post(f'/api/jobs/{job}/videos/remove', json={'ids': [task['id']]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.page()['counts']['action'], 0)
        response = self.client.post(f'/api/jobs/{job}/control', json={'action': 'delete', 'confirmation': '多产物任务'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.page(state='resolved')['counts']['resolved'], 0)

    def test_expired_owner_cannot_create_a_false_failure_notification(self):
        self.create_job()
        task = self.store.claim(self.store.owner, actions=['download'])
        self.assertFalse(self.store.finish(task['id'], error='fake error', worker_id='another-owner'))
        self.assertEqual(self.page()['counts']['action'], 0)

    def test_manual_retry_stays_action_until_paused_job_can_run(self):
        job = self.create_job()
        task = self.fail()
        from webapp.jobs import Jobs
        Jobs(self.store).control(job, 'pause', self.store.runtime(), {})
        response = self.client.post(f'/api/jobs/{job}/redeliver', json={'ids': [task['id']]}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        item = self.page()['items'][0]
        self.assertEqual(item['status'], 'action')
        self.assertIn('开启任务启动', item['detail']['activity'])
        self.assertIsNone(self.store.claim(self.store.owner, actions=['download']))
        Jobs(self.store).control(job, 'start', self.store.runtime(), {'resume_system': True})
        self.assertEqual(self.store.claim(self.store.owner, actions=['download'])['id'], task['id'])
        self.assertEqual(self.page(state='recovering')['items'][0]['status'], 'recovering')

    def test_material_reset_removes_notification_history_and_preserves_deduplication(self):
        self.create_job('SjePkzdiyWU\nITNqgQkWpGM')
        task = self.fail()
        self.fail()
        from webapp.notifications import resolve_task
        with self.store.connect() as db:
            resolve_task(self.store, db, task['id'])
        self.store.archive_add('youtube SjePkzdiyWU')
        self.store.set_downloads_paused(True)
        from webapp.media_reset import reset_records
        reset_records(self.store, 'local', lambda path: path.unlink(missing_ok=True))
        self.assertEqual(self.page()['counts'], {'action': 0, 'recovering': 0, 'resolved': 0})
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(notification_tasks)).all(), [])
        self.assertTrue(self.store.archive_has('youtube SjePkzdiyWU'))

    def test_notification_reads_do_not_scan_task_history_or_return_secrets(self):
        self.create_job()
        task = self.store.claim(self.store.owner, actions=['download'])
        self.store.finish(task['id'], error='HTTP Error 403: https://user:pass@example.org/video?token=hidden Cookie: secret', worker_id=self.store.owner)
        statements = []
        def observed(connection, cursor, statement, parameters, context, many):
            statements.append(statement.lower())
        event.listen(self.store.db.engine, 'before_cursor_execute', observed)
        try:
            page = self.page(compact='1')
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', observed)
        self.assertEqual(page['global_counts']['action'], 1)
        self.assertFalse(any('from tasks' in sql for sql in statements), statements)
        self.assertEqual(page['items'], [])
        text = json.dumps(self.page(), ensure_ascii=False)
        self.assertNotIn('hidden', text)
        self.assertNotIn('user:pass', text)
        self.assertNotIn('Cookie: secret', text)

    def test_notification_keyset_pagination_keeps_equal_timestamp_groups(self):
        self.create_job()
        task = self.store.claim(self.store.owner, actions=['download'])
        with self.store.connect() as db:
            row = db.execute(select(tasks).where(tasks.c.id == task['id'])).mappings().one()
            for number in range(33):
                settle_task(self.store, db, {**row, 'id': f'{number:032x}', 'instance_id': f'node-{number}'},
                            error='fixture failure', progress={'stage': 'downloading'})
            db.execute(notifications.update().values(updated_at=123456))
        first = self.page()
        second = self.page(cursor=first['next_cursor'])
        self.assertEqual(len(first['items']), 30)
        self.assertEqual(len(second['items']), 3)
        self.assertEqual(len({x['id'] for x in first['items'] + second['items']}), 33)
        self.assertEqual(self.client.get('/api/notifications?cursor=bad').status_code, 400)

    def test_preflight_blocker_is_visible_before_any_task_starts_and_clears_on_recovery(self):
        from webapp.download_policy import execution_decision
        with self.store.connect() as db:
            execution_decision(self.store, db, 'local', error=RuntimeError('下载环境缺少 ffmpeg'))
        item = self.page()['items'][0]
        self.assertIsNone(item['detail']['task_id'])
        self.assertIn('ffmpeg', item['detail']['reason'])
        with self.store.connect() as db:
            execution_decision(self.store, db, 'local')
        self.assertEqual(self.page()['counts']['action'], 0)

    def test_instance_offline_only_warns_for_pending_work_and_names_update(self):
        from tests.test_fleet import FleetTests
        temporary = self.root / 'fleet-fixture'
        temporary.mkdir()
        fixture = FleetTests()
        fixture.root = temporary
        fixture.url = 'sqlite:///' + (temporary / 'fleet.sqlite3').as_posix()
        fixture.setup_fleet()
        response = fixture.client.post('/api/jobs', json={'customer': 'A', 'name': 'B',
            'urls': 'SjePkzdiyWU'}, headers=fixture.headers)
        self.assertEqual(response.status_code, 201, response.json)
        job = response.json['id']
        with fixture.manager.connect() as db:
            db.execute(fixture.manager.db.upsert(settings, {'key': 'workbench_job:alpha', 'value': job}, ['key']))
            db.execute(instances.update().where(instances.c.id.in_(('alpha', 'beta'))).values(heartbeat=1))
        page = fixture.client.get('/api/notifications').json
        self.assertEqual(page['counts']['action'], 1, 'idle offline beta should not alert')
        self.assertEqual(page['items'][0]['instance_id'], 'alpha')
        with fixture.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(heartbeat=time.time(), name='新实例名称'))
        page = fixture.client.get('/api/notifications?state=resolved').json
        self.assertEqual(page['counts']['action'], 0)
        self.assertEqual(page['items'][0]['instance_name'], '新实例名称')


if __name__ == '__main__':
    unittest.main()
