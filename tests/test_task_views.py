"""History and live lists must filter before pagination on both databases."""
import unittest

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
        self.store.recover()
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


class SQLiteTaskViewTests(TaskViewContract, unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp


class PostgreSQLTaskViewTests(test_scale.PostgreSQLFixture, TaskViewContract, unittest.TestCase):
    pass
