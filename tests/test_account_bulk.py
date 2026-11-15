"""Real account operations with deterministic browser outcomes and cancellation."""
import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests import test_account_library as fixtures
from tests import test_account_library_fleet as fleet_fixtures
from webapp.account_routes import PREFIX
from webapp.database import console_users, youtube_account_cookies


class AccountBulkTests(unittest.TestCase):
    add, detail = fixtures.AccountLibraryTests.add, fixtures.AccountLibraryTests.detail

    def setUp(self):
        fixtures.AccountLibraryTests.setUp(self)
        self.batch = self.app.extensions['account_login_batch']
        self.batch.interval = .01
        self.release, self.entered = threading.Event(), threading.Event()
        self.release.set()
        self.calls = []

        async def browser(config, output, *, on_status, cancelled, credentials, expected_email, **options):
            from scripts.login_youtube import AutomaticLoginNeedsUser, LoginCancelled
            self.assertTrue(options.get('automatic_only'))
            self.calls.append(expected_email)
            self.entered.set()
            on_status('waiting', '测试账号正在自动登录。')
            while not self.release.is_set():
                if cancelled.is_set():
                    raise LoginCancelled('登录已取消。')
                await asyncio.sleep(.01)
            if expected_email.startswith('manual'):
                raise AutomaticLoginNeedsUser('此账号需要短信验证。')
            output.write_bytes(fixtures.COOKIE)
            return {'account_name': expected_email}

        mock = patch('webapp.youtube_login.login', browser)
        mock.start()
        self.addCleanup(mock.stop)

        def cleanup():
            state = self.batch.status()
            if state['active']:
                self.batch.cancel(state['id'])
            self.release.set()
            if self.batch.thread:
                self.batch.thread.join(5)
            manager = self.app.extensions['youtube_login']
            manager.cancel()
            if manager.thread:
                manager.thread.join(5)
        self.addCleanup(cleanup)

    def selected(self, *identities):
        return [{'id': identity, 'version': self.detail(identity)['version']} for identity in identities]

    def start(self, identities, **extra):
        return self.client.post(PREFIX+'/batch-login', headers=self.headers,
            json={'items': self.selected(*identities), **extra})

    def finish(self):
        self.batch.thread.join(5)
        self.assertFalse(self.batch.thread.is_alive())
        return self.client.get(PREFIX+'/batch-login').json

    def test_sequential_login_continues_after_manual_and_preserves_private_credentials(self):
        first = self.add('first@example.test', instance_id=self.scope)
        manual = self.add('manual@example.test', instance_id=self.scope)
        last = self.add('last@example.test')
        result = self.start([first, manual, last], instance_id=self.scope)
        self.assertEqual(result.status_code, 202, result.json)
        status = self.finish()
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(status['finished'], 3)
        self.assertEqual(status['counts']['succeeded'], 2)
        self.assertEqual(status['counts']['needs_attention'], 1)
        self.assertEqual(self.calls, ['first@example.test', 'manual@example.test', 'last@example.test'])
        self.assertEqual(self.detail(last)['instance_id'], self.scope)
        self.assertEqual(self.detail(first)['cookie_count'], 1)
        self.assertEqual(self.detail(manual)['cookie_count'], 0)
        self.assertNotIn('Secret', json.dumps(status) + self.batch.path.read_text())
        self.assertNotIn('password', json.dumps(status))
        compact = self.client.get(PREFIX+'/batch-login?compact=1').json
        self.assertNotIn('items', compact)
        self.assertEqual(compact['counts'], status['counts'])

    def test_stale_account_fails_without_blocking_later_accounts(self):
        first, second = self.add(instance_id=self.scope), self.add('next@example.test', instance_id=self.scope)
        selected = self.selected(first, second)
        self.client.patch(PREFIX+'/'+first, headers=self.headers, json={'version': 1, 'note': 'newer'})
        result = self.client.post(PREFIX+'/batch-login', headers=self.headers, json={'items': selected})
        self.assertEqual(result.status_code, 202, result.json)
        status = self.finish()
        self.assertEqual([row['state'] for row in status['items']], ['failed', 'succeeded'])
        self.assertIn('变化', status['items'][0]['message'])
        self.assertEqual(self.calls, ['next@example.test'])

    def test_stop_cancels_only_owned_session_and_no_later_account_starts(self):
        self.release.clear()
        first, second = self.add(instance_id=self.scope), self.add('next@example.test', instance_id=self.scope)
        result = self.start([first, second])
        self.assertEqual(result.status_code, 202)
        self.assertTrue(self.entered.wait(3))
        duplicate = self.start([second])
        self.assertEqual(duplicate.status_code, 409)
        wrong = self.client.post(PREFIX+'/batch-login/cancel', headers=self.headers, json={'id': 'wrong'})
        self.assertEqual(wrong.status_code, 409)
        cancel = self.client.post(PREFIX+'/batch-login/cancel', headers=self.headers, json={'id': result.json['id']})
        self.assertEqual(cancel.status_code, 200, cancel.json)
        status = self.finish()
        self.assertEqual(status['state'], 'cancelled')
        self.assertEqual(status['counts']['cancelled'], 2)
        self.assertEqual(len(self.calls), 1)
        with self.store.connect() as db:
            self.assertEqual(len(db.execute(select(youtube_account_cookies)).all()), 0)

    def test_missing_target_invalid_selections_and_permissions_start_nothing(self):
        identity = self.add()
        self.assertEqual(self.start([identity]).status_code, 400)
        selected = self.selected(identity)
        for items in ([], selected * 2, [{'id': identity, 'version': True}], selected * 1001):
            result = self.client.post(PREFIX+'/batch-login', headers=self.headers,
                json={'items': items, 'instance_id': self.scope})
            self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.post(PREFIX+'/batch-bind', json={'items': selected, 'instance_id': self.scope}).status_code, 403)
        with self.store.connect() as db:
            db.execute(console_users.update().values(role='operator'))
        for route in ('batch-bind', 'batch-login', 'batch-login/cancel'):
            self.assertEqual(self.client.post(PREFIX+'/'+route, headers=self.headers,
                json={'items': selected, 'instance_id': self.scope}).status_code, 403)
        self.assertEqual(self.client.get(PREFIX+'/batch-login').status_code, 403)
        self.assertFalse(self.calls)

    def test_bulk_binding_reports_each_stale_result_and_preserves_secrets(self):
        first, second = self.add(), self.add('other@example.test')
        selected = self.selected(first, second)
        self.client.patch(PREFIX+'/'+first, headers=self.headers, json={'version': 1, 'note': 'newer'})
        result = self.client.post(PREFIX+'/batch-bind', headers=self.headers,
            json={'items': selected, 'instance_id': self.scope})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['failed'], 1)
        self.assertEqual(result.json['succeeded'], 1)
        self.assertIsNone(self.detail(first)['instance_id'])
        self.assertEqual(self.detail(second)['instance_id'], self.scope)
        self.assertEqual(self.client.post(PREFIX+'/'+second+'/secrets', headers=self.headers,
            json={'field': 'password'}).json['value'], ' a&!#Secret 123 ')

    def test_interrupted_batch_has_explicit_results(self):
        self.batch._write({'id': 'fixture', 'state': 'running', 'message': '', 'items': [
            {'id': 'one', 'state': 'succeeded'}, {'id': 'two', 'state': 'running'}, {'id': 'three', 'state': 'queued'}]})
        result = self.batch.status()
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['counts']['succeeded'], 1)
        self.assertEqual(result['counts']['interrupted'], 2)
        self.assertFalse(result['active'])


class AccountBulkFleetTests(unittest.TestCase):
    setup_fleet = fleet_fixtures.AccountLibraryFleetTests.setup_fleet
    serve = fleet_fixtures.AccountLibraryFleetTests.serve
    add = fleet_fixtures.AccountLibraryFleetTests.add
    detail = fleet_fixtures.AccountLibraryFleetTests.detail
    upload = fleet_fixtures.AccountLibraryFleetTests.upload

    def setUp(self):
        fleet_fixtures.AccountLibraryFleetTests.setUp(self)
        self.batch = self.apps['manager'].extensions['account_login_batch']
        self.batch.interval = .01

        def cleanup():
            state = self.batch.status()
            if state['active']:
                self.batch.cancel(state['id'])
            if self.batch.thread:
                self.batch.thread.join(5)
            for app in self.apps.values():
                manager = app.extensions['youtube_login']
                manager.cancel()
                if manager.thread:
                    manager.thread.join(5)
        self.addCleanup(cleanup)

    def test_batch_login_routes_each_account_to_owner_not_selected_instance(self):
        first, second = self.add(target='beta'), self.add('alpha@example.test', target='alpha')
        calls = []

        async def browser(config, output, *, expected_email, **options):
            calls.append(expected_email)
            self.assertTrue(options['automatic_only'])
            output.write_bytes(fleet_fixtures.COOKIE.replace(b'fleet-account-test', expected_email.encode()))
            return {'account_name': expected_email}

        with patch('webapp.youtube_login.login', browser):
            result = self.client.post(PREFIX+'/batch-login', headers={**self.headers, 'X-Instance-ID': 'alpha'},
                json={'items': [{'id': first, 'version': 1}, {'id': second, 'version': 1}]})
            self.assertEqual(result.status_code, 202, result.json)
            self.batch.thread.join(8)
            self.assertFalse(self.batch.thread.is_alive())
        status = self.batch.status()
        self.assertEqual(status['counts']['succeeded'], 2, status)
        self.assertEqual(self.detail(first)['cookies'][0]['instance_id'], 'beta')
        self.assertEqual(self.detail(second)['cookies'][0]['instance_id'], 'alpha')
        self.assertEqual(calls, ['fleet@example.com', 'alpha@example.test'])
        self.assertFalse(self.apps['manager'].extensions['youtube_login'].status()['active'])

    def test_batch_bind_moves_unlinked_account_but_keeps_cookie_ownership(self):
        owned, movable = self.add(), self.add('movable@example.test')
        self.assertEqual(self.upload(owned).status_code, 201)
        selected = [{'id': identity, 'version': self.detail(identity)['version']} for identity in (owned, movable)]
        result = self.client.post(PREFIX+'/batch-bind', headers=self.headers,
            json={'items': selected, 'instance_id': 'alpha'})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['succeeded'], 1)
        self.assertEqual(result.json['failed'], 1)
        self.assertEqual(self.detail(owned)['instance_id'], 'beta')
        self.assertEqual(self.detail(owned)['cookies'][0]['instance_id'], 'beta')
        self.assertEqual(self.detail(movable)['instance_id'], 'alpha')

    def test_background_check_uses_owner_instance_not_selected_instance(self):
        identity = self.add(target='beta')
        self.assertEqual(self.upload(identity).status_code, 201)
        batch = self.apps['manager'].extensions['account_check_batch']
        with patch.object(self.apps['beta'].extensions['youtube_auth'], 'check_profile',
                          return_value={'state': 'valid', 'message': '所属实例检测通过。'}) as owner, \
                patch.object(self.apps['alpha'].extensions['youtube_auth'], 'check_profile') as other:
            result = self.client.post(PREFIX + '/batch-check', headers={**self.headers, 'X-Instance-ID': 'alpha'},
                json={'items': [{'id': identity, 'version': self.detail(identity)['version']}]})
            self.assertEqual(result.status_code, 202, result.json)
            batch.thread.join(8)
            self.assertFalse(batch.thread.is_alive())
            self.assertEqual(batch.status()['counts']['succeeded'], 1)
            owner.assert_called_once()
            self.assertFalse(other.called)


if __name__ == '__main__':
    unittest.main()
