"""UI snapshots never become an execution source of truth."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

from flask import Flask

from webapp.display_cache import display_cache


class DisplayCacheTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.views = display_cache(self.app)
        self.value = 0
        self.calls = 0

        @self.app.route('/view', methods=['GET', 'POST'])
        def view():
            from flask import request
            if request.method == 'POST':
                self.value += 1
            return self.views.read(('view', request.args.get('instance'), request.args.get('job')), self.load)

    def load(self):
        self.calls += 1
        return {'value': self.value}

    def test_only_poll_gets_cached_and_manual_refresh_and_writes_invalidate(self):
        client = self.app.test_client()
        self.assertEqual(client.get('/view?poll=1').json, {'value': 0})
        self.value = 1
        self.assertEqual(client.get('/view?poll=1').json, {'value': 0})
        self.assertEqual(client.get('/view').json, {'value': 1})
        self.assertEqual(client.get('/view?poll=1').json, {'value': 1})
        self.assertEqual(client.post('/view?poll=1').json, {'value': 2})
        self.assertEqual(client.get('/view?poll=1').json, {'value': 2})

    def test_ttl_target_and_preview_are_isolated_and_results_detached(self):
        with patch('webapp.read_cache.time.monotonic', return_value=10) as clock:
            with self.app.test_request_context('/view?poll=1'):
                value = self.views.read(('a', 'one'), self.load)
                value['value'] = 99
                self.assertEqual(self.views.read(('a', 'one'), self.load)['value'], 0)
                self.views.read(('a', 'two'), self.load)
                self.views.read(('b', 'one'), self.load)
                self.assertEqual(self.calls, 3)
                clock.return_value = 11.01
                self.value = 4
                self.assertEqual(self.views.read(('a', 'one'), self.load)['value'], 4)

    def test_concurrent_pollers_coalesce_and_subsequent_write_invalidates(self):
        entered, release = threading.Event(), threading.Event()
        def load():
            entered.set()
            self.assertTrue(release.wait(3))
            return self.load()
        def poll():
            with self.app.test_request_context('/view?poll=1'):
                return self.views.read(('view', None, None), load)
        with ThreadPoolExecutor(4) as pool:
            first = pool.submit(poll)
            self.assertTrue(entered.wait(3))
            others = [pool.submit(poll) for _ in range(2)]
            release.set()
            self.assertEqual(first.result(), {'value': 0})
            self.assertTrue(all(item.result() == {'value': 0} for item in others))
        self.assertEqual(self.calls, 1)
        client = self.app.test_client()
        client.post('/view')
        self.assertEqual(client.get('/view?poll=1').json, {'value': 1})

    def test_failed_loads_are_not_cached_and_failed_writes_also_invalidate(self):
        with self.app.test_request_context('/view?poll=1'):
            with self.assertRaises(ValueError):
                self.views.read(('error',), lambda: (_ for _ in ()).throw(ValueError('fixture')))
            self.assertEqual(self.views.read(('error',), self.load), {'value': 0})
        @self.app.post('/failed')
        def failed():
            self.value = 7
            return {'error': 'fixture'}, 409
        client = self.app.test_client()
        client.get('/view?poll=1')
        client.post('/failed')
        self.assertEqual(client.get('/view?poll=1').json, {'value': 7})

    def test_write_invalidates_a_snapshot_that_was_loading_concurrently(self):
        entered, release, changed = threading.Event(), threading.Event(), threading.Event()
        def poll():
            def load():
                value = self.value
                entered.set()
                self.assertTrue(release.wait(3))
                return {'value': value}
            with self.app.test_request_context('/view?poll=1'):
                return self.views.read(('view', None, None), load)
        @self.app.post('/change')
        def change():
            self.value = 8
            changed.set()
            return {'saved': True}
        def write():
            return self.app.test_client().post('/change').status_code
        with ThreadPoolExecutor(2) as pool:
            reader = pool.submit(poll)
            self.assertTrue(entered.wait(3))
            writer = pool.submit(write)
            try:
                self.assertTrue(changed.wait(3))
            finally:
                release.set()
            self.assertEqual(reader.result(), {'value': 0})
            self.assertEqual(writer.result(), 200)
        self.assertEqual(self.app.test_client().get('/view?poll=1').json, {'value': 8})


if __name__ == '__main__':
    unittest.main()
