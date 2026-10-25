"""Queue-only input, asynchronous preflight and cross-session customer billing."""
import csv
from io import BytesIO, StringIO
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from tests import test_scale
from webapp.bulk import run_import
from webapp.database import download_lists, download_sessions
from webapp.startup import DownloadStartup


class WorkflowTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def enqueue(self, urls='npNnbj46_ik', **values):
        response = self.client.post('/api/downloads/queue', json={'urls': urls, **values}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        return response.json

    def wait_startup(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get('/api/downloads/startup')
            self.assertEqual(response.status_code, 200)
            if response.json['state'] != 'running':
                return response.json
            time.sleep(0.01)
        self.fail('Startup did not complete')

    def test_adding_list_needs_no_environment_and_never_auto_starts(self):
        with patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no login')):
            first = self.enqueue(customer='Client A', list_name='One')
            duplicate = self.enqueue()
        self.assertEqual((first['added'], duplicate['added'], duplicate['duplicates']), (1, 0, 1))
        self.assertIsNone(self.store.claim(self.store.owner))
        self.assertEqual(self.store.runnable_counts(), {})
        self.assertFalse(self.store.download_connection_busy())
        self.assertEqual(self.store.download_queue()['waiting_lists'], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(download_sessions)).all(), [])
        self.store.activate_download_lists()
        running = self.store.claim(self.store.owner)
        self.assertIsNotNone(running)
        self.enqueue('2Gu7q-KUXCk')
        self.assertIsNone(self.store.claim(self.store.owner))
        self.store.finish(running['id'], result={}, worker_id=self.store.owner)
        session = self.store.statistics.session(running['payload']['download_session_id'])
        self.assertEqual(session['status'], 'completed')  # An unrelated new list cannot hold it open.

    def test_import_runs_while_paused_but_videos_wait_and_proxy_stays_editable(self):
        self.store.set_downloads_paused(True)
        response = self.client.post('/api/downloads/import', data={
            'customer': 'Client A', 'list_name': '', 'file': (BytesIO(b'npNnbj46_ik\ninvalid\n'), 'client.txt')}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        imported = self.store.claim(self.store.owner, actions=('import',))
        result = run_import(self.store, imported['payload'], lambda value: None)
        self.store.finish(imported['id'], result=result, worker_id=self.store.owner)
        self.assertIsNone(self.store.claim(self.store.owner, actions=('download',)))
        self.assertFalse(self.store.download_connection_busy())
        row = self.client.get('/api/statistics/lists').json['items'][0]
        self.assertEqual((row['name'], row['customer'], row['queued'], row['invalid']), ('client.txt', 'Client A', 1, 1))
        self.assertEqual(self.client.put('/api/config', json={'proxy_mode': 'manual'}, headers=self.headers).status_code, 200)
        self.assertFalse(self.client.get('/api/clash').json['rotation_locked'])

    def test_only_released_pending_imports_extend_a_session(self):
        self.enqueue()
        self.store.activate_download_lists()
        video = self.store.claim(self.store.owner)
        self.client.post('/api/downloads/import', data={'file': (BytesIO(b'2Gu7q-KUXCk'), 'later.txt')}, headers=self.headers)
        self.store.finish(video['id'], result={}, worker_id=self.store.owner)
        self.assertEqual(self.store.statistics.session(video['payload']['download_session_id'])['status'], 'completed')
        # The released import will keep the next interval open until it ends,
        # even if there is a short gap between imported chunks.
        self.enqueue('WMu6ANTg8mk')
        self.store.activate_download_lists()
        video = self.store.claim(self.store.owner, actions=('download',))
        self.store.finish(video['id'], result={}, worker_id=self.store.owner)
        identity = video['payload']['download_session_id']
        self.assertEqual(self.store.statistics.session(identity)['status'], 'active')
        imported = self.store.claim(self.store.owner, actions=('import',))
        self.store.finish(imported['id'], error='invalid file', worker_id=self.store.owner)
        self.assertEqual(self.store.statistics.session(identity)['status'], 'completed')

    def test_async_start_reports_stages_checks_selected_providers_and_can_cancel(self):
        self.enqueue()
        self.client.put('/api/config', json={'download_methods': ['downmp3']}, headers=self.headers)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def check():
            entered.set()
            self.assertTrue(release.wait(4))
        with patch('app.require_downmp3_environment', side_effect=check), \
                patch('app.require_browser_environment', side_effect=AssertionError('no browser')), \
                patch('webapp.youtube_auth.YouTubeAuth.require', side_effect=AssertionError('no cookie')):
            result = self.client.post('/api/downloads/start', headers=self.headers)
            self.assertEqual(result.status_code, 202)
            self.assertTrue(entered.wait(2))
            current = self.client.get('/api/downloads/startup').json
            self.assertEqual(current['stage'], 'checking_environment')
            again = self.client.post('/api/downloads/start', headers=self.headers).json
            self.assertEqual(again['id'], result.json['id'])
            self.client.post('/api/downloads/stop', headers=self.headers)
            release.set()
            current = self.wait_startup()
        self.assertEqual(current['state'], 'cancelled')
        self.assertTrue(self.store.downloads_paused())
        self.assertEqual(self.store.download_queue()['waiting_lists'], 1)
        with patch('app.require_downmp3_environment'):
            self.client.post('/api/downloads/start', headers=self.headers)
            current = self.wait_startup()
        self.assertEqual(current['state'], 'succeeded', current)
        self.assertEqual([step['stage'] for step in current['steps']], [
            'checking_database', 'reading_queue', 'preparing_proxy', 'checking_environment', 'releasing_queue'])
        self.assertTrue(all(step['elapsed'] >= 0 for step in current['steps']))
        self.assertIsNotNone(self.store.claim(self.store.owner))

    def test_preflight_config_changes_or_database_failure_do_not_release_lists(self):
        self.enqueue()
        self.client.put('/api/config', json={'download_methods': ['downmp3']}, headers=self.headers)
        def changed():
            self.client.put('/api/config', json={'downmp3_quality': '1080'}, headers=self.headers)
        with patch('app.require_downmp3_environment', side_effect=changed):
            self.client.post('/api/downloads/start', headers=self.headers)
            self.assertEqual(self.wait_startup()['state'], 'failed')
        self.assertEqual(self.store.download_queue()['waiting_lists'], 1)
        with patch.object(self.store, 'download_queue', side_effect=OperationalError('secret', {}, Exception('No route to host'))):
            self.client.post('/api/downloads/start', headers=self.headers)
            result = self.wait_startup()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('MySQL', result['message'])
        self.assertNotIn('secret', result['message'])

    def test_customer_bill_spans_pauses_and_retains_deleted_test_bytes(self):
        queued = self.enqueue('npNnbj46_ik\n2Gu7q-KUXCk\nWMu6ANTg8mk', customer='=Client A', list_name='List One')
        self.store.activate_download_lists()
        first = self.store.claim(self.store.owner)
        path = self.root / 'first.mp4'
        path.write_bytes(b'a' * 1000)
        self.assertTrue(self.store.statistics.record_file(first['id'], path))
        self.assertFalse(self.store.statistics.record_file(first['id'], path))
        self.store.finish(first['id'], result={}, worker_id=self.store.owner)
        self.store.set_downloads_paused(True)
        self.store.activate_download_lists()
        second = self.store.claim(self.store.owner)
        self.assertNotEqual(first['payload']['download_session_id'], second['payload']['download_session_id'])
        path = self.root / 'test.mp4'
        path.write_bytes(b'a' * 2000)
        self.store.statistics.record_file(second['id'], path, test_mode=True)
        path.unlink()
        self.store.finish(second['id'], result={'test_mode': True}, worker_id=self.store.owner)
        third = self.store.claim(self.store.owner)
        self.store.finish(third['id'], error='permanent fixture', worker_id=self.store.owner)
        route = '/api/statistics/lists/' + queued['batch_id']
        response = self.client.put(route, json={'customer': '=Client A', 'name': 'List One', 'price': '15000', 'unit': 'GB'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        row = self.client.get('/api/statistics/lists?q=Client').json['items'][0]
        self.assertEqual((row['file_count'], row['file_bytes'], row['test_file_bytes'], row['amount']), (1, 1000, 2000, '0.02'))
        self.assertEqual((row['succeeded'], row['test_succeeded'], row['failed']), (1, 1, 1))
        exported = self.client.get('/api/statistics/lists/export').data.decode('utf-8-sig')
        self.assertIn("'=Client A", exported)
        testing = self.client.get(route + '/files/export?kind=test').data.decode('utf-8-sig')
        self.assertIn('2000', testing)
        self.assertIn('否（测试文件已删除）', testing)
        self.assertNotIn('test.mp4', self.client.get(route + '/files/export').text)
        self.assertEqual(self.client.put(route, json={'price': '-1'}, headers=self.headers).status_code, 400)

    def test_list_pagination_exact_bytes_and_legacy_disclaimer(self):
        identity = self.enqueue()['batch_id']
        with self.store.connect() as db:
            db.execute(download_lists.update().where(download_lists.c.id == identity).values(file_bytes=2**53+1))
        self.store.create('download', {'urls': ['https://youtu.be/2Gu7q-KUXCk'], 'batch_id': 'legacy'})
        rows = self.store.lists.page(limit=1)
        self.assertFalse(rows['items'][0]['tracked'])
        next_row = self.store.lists.page(rows['next_cursor'], limit=1)['items'][0]
        self.assertEqual(next_row['file_bytes_exact'], str(2**53+1))
        exported = list(csv.reader(StringIO(self.client.get('/api/statistics/lists/export').data.decode('utf-8-sig'))))
        self.assertIn(str(2**53+1), exported[2])

    def test_unreleased_backlog_uses_batch_counters_without_scanning_tasks(self):
        from sqlalchemy import event
        self.enqueue('\n'.join(f'{index:011d}' for index in range(100)))
        queries = []
        def capture(connection, cursor, statement, *args):
            queries.append(statement)
        event.listen(self.store.db.engine, 'before_cursor_execute', capture)
        try:
            self.assertEqual(self.store.runnable_counts(), {})
            self.assertFalse(self.store.download_connection_busy())
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', capture)
        self.assertFalse(any('FROM tasks' in query for query in queries), queries)


class StartupTimingTests(unittest.TestCase):
    def test_elapsed_uses_finished_time_and_snapshot_is_detached(self):
        startup = DownloadStartup()
        startup.value = {'state': 'succeeded', 'started_at': 10, 'finished_at': 20,
                         'steps': [{'stage': 'checking_database', 'started_at': 10, 'finished_at': 14},
                                   {'stage': 'checking_environment', 'started_at': 14}]}
        value = startup.status()
        self.assertEqual((value['elapsed'], value['slowest']), (10, 'checking_environment'))
        value['steps'].clear()
        self.assertEqual(len(startup.status()['steps']), 2)
