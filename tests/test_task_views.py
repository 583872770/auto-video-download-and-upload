"""History and live lists must filter before pagination on both databases."""
import unittest
from unittest.mock import patch

from webapp.database import tasks

from tests import test_scale


class TaskViewContract:
    def create(self, action='download', batch='a'):
        return self.store.create(action, {'config': {}, 'base': str(self.root),
                                         'batch_id': batch * 32})

    def test_history_filters_before_pagination_search_and_result_filter(self):
        completed = []
        for index in range(7):
            task = self.create()
            self.store.finish(task['id'], error='fixture failure' if index % 2 else None)
            completed.append(task)
        interrupted = self.create('check')
        self.store.claim('exited-worker')
        self.store.recover(expired_only=False)
        completed.append(interrupted)
        # Newer queued rows must not hide older finished records.
        self.store.create_batch([{'config': {}, 'base': str(self.root),
                                  'urls': [f'https://example.test/queued-{index}.mp4']} for index in range(80)])
        running = self.store.claim('current-worker')
        response = self.client.get('/api/tasks?scope=download&view=history&limit=3').json
        found = list(response['items'])
        while response['next_cursor']:
            response = self.client.get('/api/tasks?scope=download&view=history&limit=3&cursor=' + response['next_cursor']).json
            found.extend(response['items'])
        self.assertEqual([task['id'] for task in found], [task['id'] for task in reversed(completed)])
        self.assertTrue(all(task['status'] in ('succeeded', 'failed', 'interrupted') for task in found))
        for status, expected in (('succeeded', 4), ('failed', 4), ('running', 0)):
            result = self.client.get('/api/tasks?view=history&status=' + status).json['items']
            self.assertEqual(len(result), expected)
        self.assertEqual(self.client.get('/api/tasks?view=history&q=' + running['id']).json['items'], [])
        overview = self.client.get('/api/overview').json
        self.assertTrue(all(task['status'] in ('succeeded', 'failed', 'interrupted') for task in overview['recent_tasks']))
        # The original queue API remains available to operators.
        self.assertTrue(any(task['status'] == 'queued' for task in self.client.get('/api/tasks').json['items']))

    def test_live_downloads_span_batches_and_move_to_history_on_completion(self):
        first, second = self.create(batch='a'), self.create(batch='b')
        self.create('check')
        self.create('import')
        queued = self.create(batch='c')
        for index in range(4):
            self.store.claim('fixture-' + str(index))
        self.store.update_progress(first['id'], {'stage': 'downloading', 'speed': 123, 'percent': 42})
        active = self.client.get('/api/tasks?view=active&order=asc').json['items']
        self.assertEqual([task['id'] for task in active], [first['id'], second['id']])
        self.assertEqual(active[0]['progress']['speed'], 123)
        self.assertEqual(self.client.get('/api/tasks?view=history').json['items'], [])
        self.store.finish(first['id'], result={'message': 'done'})
        self.store.finish(second['id'], error='download failed')
        self.assertEqual(self.client.get('/api/tasks?view=active').json['items'], [])
        history = self.client.get('/api/tasks?view=history').json['items']
        self.assertEqual({task['id'] for task in history}, {first['id'], second['id']})
        self.store.set_downloads_paused(True)
        self.assertEqual(self.client.get('/api/tasks?view=active').json['items'], [])
        self.assertEqual(self.store.get(queued['id'])['status'], 'queued')

    def test_history_orders_by_completion_with_stable_time_ties_and_keyset_pages(self):
        with patch('webapp.store.now', return_value='2026-10-23T08:00:00+08:00'):
            first, second, third, fourth = [self.create() for _ in range(4)]
        for task, timestamp in [(second, '2026-10-24T10:00:00+08:00'),
                                (third, '2026-10-24T10:01:00+08:00'),
                                (first, '2026-10-24T10:02:00+08:00'),
                                (fourth, '2026-10-24T10:02:00+08:00')]:
            with patch('webapp.store.now', return_value=timestamp):
                self.store.finish(task['id'], error='fixture failure' if task is third else None)
        expected = [fourth['id'], first['id'], third['id'], second['id']]
        response = self.client.get('/api/tasks?scope=download&view=history&limit=1').json
        self.assertEqual(response['items'][0]['id'], fourth['id'])
        self.assertEqual(response['items'][0]['finished_at'], '2026-10-24T10:02:00+08:00')
        self.assertEqual(response['items'][0]['created_at'], '2026-10-23T08:00:00+08:00')
        # A completion arriving between pages belongs to the refreshed first page.
        newest = self.create()
        with patch('webapp.store.now', return_value='2026-10-24T10:03:00+08:00'):
            self.store.finish(newest['id'])
        found = [response['items'][0]['id']]
        while response['next_cursor']:
            response = self.client.get('/api/tasks?scope=download&view=history&limit=1&cursor=' + response['next_cursor']).json
            found.extend(row['id'] for row in response['items'])
        self.assertEqual(found, expected)
        ascending = self.store.page(finished_only=True, order='asc', limit=2)
        asc_ids = [row['id'] for row in ascending['items']]
        while ascending['next_cursor']:
            ascending = self.store.page(finished_only=True, order='asc', limit=2, cursor=ascending['next_cursor'])
            asc_ids.extend(row['id'] for row in ascending['items'])
        self.assertEqual(asc_ids, list(reversed([newest['id'], *expected])))
        overview = self.client.get('/api/overview').json
        self.assertEqual([row['id'] for row in overview['recent_tasks']], [newest['id'], *expected])
        failed = self.client.get('/api/tasks?view=history&status=failed').json['items']
        self.assertEqual([row['id'] for row in failed], [third['id']])

    def test_history_handles_legacy_missing_finish_times_and_rejects_invalid_cursors(self):
        legacy, legacy_second, current = [self.create() for _ in range(3)]
        for row in (legacy, legacy_second, current):
            self.store.finish(row['id'])
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id.in_([legacy['id'], legacy_second['id']])).values(finished_at=None))
        for order, expected in [('desc', [current['id'], legacy_second['id'], legacy['id']]),
                                ('asc', [legacy['id'], legacy_second['id'], current['id']])]:
            response = self.store.page(finished_only=True, order=order, limit=1)
            found = [row['id'] for row in response['items']]
            while response['next_cursor']:
                response = self.store.page(finished_only=True, order=order, limit=1, cursor=response['next_cursor'])
                found.extend(row['id'] for row in response['items'])
            self.assertEqual(found, expected)
        for cursor in ('123', 'h.invalid', 'h.bnVsbA==', 'h.' + 'x' * 600):
            self.assertEqual(self.client.get('/api/tasks?view=history&cursor=' + cursor).status_code, 400)


class SQLiteTaskViewTests(TaskViewContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


