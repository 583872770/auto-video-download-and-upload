"""Apply connection changes to large paused queues without rewriting jobs."""
from copy import deepcopy
from io import BytesIO
import unittest
from unittest.mock import patch

from sqlalchemy import event, select

from tests import test_scale
from tests.test_mysql import MySQLFixture
from webapp.clash import ClashError
from webapp.database import snapshots, tasks
from webapp.store import TaskStore
from web_worker import execute


class ConnectionContract:
    def old_task(self):
        payload = {'config': {'path': 'videos', 'proxy_mode': 'direct', 'download_method': 'yt5s',
                              'yt5s_quality': '360', 'youtube_cookie': 'Cookies/original.txt'},
                   'base': str(self.root), 'urls': ['https://www.youtube.com/watch?v=npNnbj46_ik']}
        return self.store.create('download', payload), payload

    def apply(self):
        return self.client.post('/api/downloads/connection', headers=self.headers)

    def use_clash(self):
        response = self.client.put('/api/config', json={'proxy_mode': 'clash'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return {'directory': str(self.app.extensions['clash'].directory), 'node': 'fixture-node',
                'proxy': 'http://127.0.0.1:17890'}

    def test_requires_csrf_pause_drained_queue_and_idle_login(self):
        page = self.client.get('/').text
        self.assertIn('id="clash-apply-connection"', page)
        self.assertIn('将当前连接应用到排队任务</button>', page)
        self.assertEqual(self.client.post('/api/downloads/connection').status_code, 403)
        task, old = self.old_task()
        self.assertTrue(self.client.get('/api/clash').json['connection_apply_locked'])
        self.assertEqual(self.apply().status_code, 409)
        self.store.claim('owner')
        self.store.set_downloads_paused(True)
        self.assertEqual(self.apply().status_code, 409)
        self.assertEqual(self.client.put('/api/config', json={'proxy_mode': 'clash'},
                                        headers=self.headers).status_code, 409)
        self.store.stop_running_downloads(lambda owner: True)
        with patch.object(self.app.extensions['youtube_login'], 'status', return_value={'active': True}):
            self.assertTrue(self.client.get('/api/clash').json['connection_apply_locked'])
            self.assertEqual(self.apply().status_code, 409)
        self.assertFalse(self.client.get('/api/clash').json['connection_apply_locked'])
        self.assertEqual(self.apply().status_code, 200)
        self.assertEqual(self.store.get(task['id'])['attempts'], 0)
        self.store.finish(task['id'], result={})
        self.assertEqual(self.apply().status_code, 409)
        self.assertTrue(self.client.get('/api/clash').json['connection_apply_locked'])

    def test_applies_without_task_or_snapshot_writes_and_survives_restart(self):
        task, old = self.old_task()
        before = deepcopy(old)
        self.store.set_downloads_paused(True)
        clash = self.use_clash()
        with self.store.connect() as db:
            saved_tasks = list(db.execute(select(tasks)).all())
            saved_snapshots = list(db.execute(select(snapshots)).all())
        writes = []

        def statement(db, cursor, sql, parameters, context, many):
            if sql.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
                writes.append(sql)

        event.listen(self.store.db.engine, 'before_cursor_execute', statement)
        try:
            with patch.object(self.app.extensions['clash'], 'snapshot', return_value=clash):
                response = self.apply()
                self.assertEqual(response.status_code, 200, response.json)
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', statement)
        self.assertEqual(len(writes), 1, writes)
        self.assertIn('schema_settings', writes[0])
        with self.store.connect() as db:
            self.assertEqual(list(db.execute(select(tasks)).all()), saved_tasks)
            self.assertEqual(list(db.execute(select(snapshots)).all()), saved_snapshots)
        reopened = TaskStore(self.store.directory)
        effective = reopened.effective_download_payload(task['id'], old)
        self.assertEqual(effective['config'], {**old['config'], 'proxy_mode': 'clash', 'proxy': ''})
        self.assertEqual(effective['clash'], clash)
        self.assertEqual(effective['urls'], old['urls'])
        self.assertEqual(old, before)
        self.assertEqual(reopened.download_queue(), {'paused': True, 'queued': 1, 'running': 0})

    def test_cutoff_new_tasks_keep_snapshots_and_reapply_replaces_connection(self):
        task, old = self.old_task()
        self.store.set_downloads_paused(True)
        clash = self.use_clash()
        with patch.object(self.app.extensions['clash'], 'snapshot', return_value=clash):
            self.assertEqual(self.apply().status_code, 200)
        newer = self.store.create('download', {**old, 'urls': ['https://www.youtube.com/watch?v=2Gu7q-KUXCk']})
        self.assertEqual(self.store.effective_download_payload(newer['id'], old), old)
        manual = 'http://user:fixture-secret@127.0.0.1:9999'
        self.assertEqual(self.client.put('/api/config', json={'proxy_mode': 'manual', 'proxy': manual},
                                        headers=self.headers).status_code, 200)
        self.assertEqual(self.apply().status_code, 200)
        for identity in (task['id'], newer['id']):
            effective = self.store.effective_download_payload(identity, {**old, 'clash': clash})
            self.assertNotIn('clash', effective)
            self.assertEqual(effective['config']['proxy'], manual)
        state = self.client.get('/api/clash')
        self.assertNotIn('fixture-secret', state.text)
        self.assertEqual(state.json['download_connection']['mode'], 'manual')
        self.assertEqual(self.store.effective_download_payload(None, old), old)

    def test_clash_failure_keeps_previous_override_and_queue_paused(self):
        task, old = self.old_task()
        self.store.set_downloads_paused(True)
        self.assertEqual(self.apply().status_code, 200)
        previous = self.store.download_connection()
        self.use_clash()
        with patch.object(self.app.extensions['clash'], 'snapshot', side_effect=ClashError('fixture failure')):
            self.assertEqual(self.apply().status_code, 502)
        self.assertEqual(self.store.download_connection(), previous)
        self.assertEqual(self.store.effective_download_payload(task['id'], old)['config']['proxy_mode'], 'direct')
        self.assertTrue(self.store.downloads_paused())

    def test_worker_and_resume_preflight_receive_effective_connection(self):
        task, old = self.old_task()
        self.store.set_downloads_paused(True)
        clash = self.use_clash()
        with patch.object(self.app.extensions['clash'], 'snapshot', return_value=clash):
            self.assertEqual(self.apply().status_code, 200)
        with patch('web_worker._execute', return_value={'message': 'fixture'}) as run:
            execute('download', old, store=self.store, task_id=task['id'], worker_slot=1)
        used = run.call_args.args[1]
        self.assertEqual(used['config']['proxy_mode'], 'clash')
        self.assertEqual(used['clash'], clash)
        self.assertEqual(used['config']['download_method'], 'yt5s')
        # Preflight method one must receive the applied proxy, even when the
        # page's current configuration has subsequently changed back to direct.
        first = {**old, 'config': {**old['config'], 'download_method': 'yt_dlp'}}
        with self.store.connect() as db:
            packed = self.store._pack(db, first)
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(payload=packed))
        self.assertEqual(self.client.put('/api/config', json={'proxy_mode': 'direct'},
                                        headers=self.headers).status_code, 200)
        with patch('webapp.youtube_auth.YouTubeAuth.require', return_value={}) as auth:
            response = self.client.post('/api/downloads/resume', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(auth.call_args.kwargs['proxy'], clash['proxy'])
        self.assertEqual(auth.call_args.kwargs['node'], clash['node'])
        self.assertEqual(old['config']['proxy_mode'], 'direct')

    def test_pending_import_creates_new_downloads_with_applied_connection(self):
        response = self.client.post('/api/downloads/import', headers=self.headers,
            data={'file': (BytesIO(b'npNnbj46_ik\n2Gu7q-KUXCk\n'), 'videos.txt')})
        self.assertEqual(response.status_code, 202, response.json)
        task = self.store.get(response.json['id'], private=True)
        self.store.set_downloads_paused(True)
        clash = self.use_clash()
        with patch.object(self.app.extensions['clash'], 'snapshot', return_value=clash):
            self.assertEqual(self.apply().status_code, 200)
        result = execute('import', task['payload'], store=self.store, task_id=task['id'])
        self.assertEqual(result['added'], 2)
        jobs = self.store.page(actions=('download',))['items']
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            payload = self.store.get(job['id'], private=True)['payload']
            self.assertEqual(payload['config']['proxy_mode'], 'clash')
            self.assertEqual(payload['clash'], clash)
        self.assertEqual(task['payload']['config']['proxy_mode'], 'direct')

    def test_method_two_browser_receives_clash_proxy_for_each_worker(self):
        task, old = self.old_task()
        self.store.set_downloads_paused(True)
        clash = self.use_clash()
        manager = self.app.extensions['clash']
        with patch.object(manager, 'snapshot', return_value=clash):
            self.assertEqual(self.apply().status_code, 200)
        for slot in (0, 1):
            resolved = {**clash, 'proxy': f'http://127.0.0.1:{17890 + slot}'}
            with self.subTest(slot=slot), patch('webapp.clash.ClashManager.for_worker', return_value=manager), \
                    patch('webapp.clash.ClashManager.select'), \
                    patch('webapp.clash.ClashManager.download_snapshot', return_value=resolved), \
                    patch('Download.require_download_tools'), \
                    patch('webapp.yt5s.resolve', side_effect=RuntimeError('stop at browser fixture')) as browser, \
                    patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('method two uses no cookie')):
                with self.assertRaisesRegex(RuntimeError, 'stop at browser fixture'):
                    execute('download', old, store=self.store, worker_slot=slot, task_id=task['id'])
                config = browser.call_args.args[1]
                self.assertEqual(config['proxy_mode'], 'clash')
                self.assertEqual(config['_resolved_clash_proxy'], resolved['proxy'])
                self.assertEqual(config['yt5s_quality'], '360')
        self.assertEqual(old['config']['proxy_mode'], 'direct')


class SQLiteConnectionTests(ConnectionContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


class MySQLConnectionTests(MySQLFixture, ConnectionContract, unittest.TestCase):
    pass
