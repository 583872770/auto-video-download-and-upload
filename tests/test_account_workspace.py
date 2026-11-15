"""Consolidated account filters and background login checks, without real requests."""
import json
import threading
import unittest
from unittest.mock import patch

from tests import test_account_library as fixtures
from webapp.account_library import AccountLibrary
from webapp.account_routes import PREFIX


class AccountWorkspaceTests(unittest.TestCase):
    setUp = fixtures.AccountLibraryTests.setUp
    add = fixtures.AccountLibraryTests.add
    detail = fixtures.AccountLibraryTests.detail
    cookie = fixtures.AccountLibraryTests.cookie

    def selected(self, *ids):
        return [{'id': identity, 'version': self.detail(identity)['version']} for identity in ids]

    def check(self, selected):
        response = self.client.post(PREFIX + '/batch-check', headers=self.headers, json={'items': selected})
        self.assertEqual(response.status_code, 202, response.json)
        batch = self.app.extensions['account_check_batch']
        batch.thread.join(5)
        self.assertFalse(batch.thread.is_alive())
        return self.client.get(PREFIX + '/batch-check').json

    def test_check_records_valid_missing_and_stale_without_stopping_the_batch(self):
        first = self.add('valid@example.test', instance_id=self.scope)
        stale = self.add('stale@example.test', instance_id=self.scope)
        missing = self.add('missing@example.test')
        self.cookie(identity=first)
        selected = self.selected(stale, missing, first)
        self.client.patch(PREFIX + '/' + stale, headers=self.headers,
                          json={'version': selected[0]['version'], 'note': 'updated'})
        with patch('webapp.youtube_auth.probe', return_value=('valid', '测试登录通过。')) as probe:
            result = self.check(selected)
        self.assertEqual([r['state'] for r in result['items']], ['failed', 'skipped', 'succeeded'])
        self.assertIn('尚未保存', result['items'][1]['message'])
        probe.assert_called_once()
        self.assertEqual(self.detail(first)['auth_state'], 'valid')
        self.assertNotIn('password', json.dumps(result))
        self.assertNotIn('items', self.client.get(PREFIX + '/batch-check?compact=1').json)

    def test_stop_finishes_current_check_and_does_not_start_the_next(self):
        first = self.add('first@example.test', instance_id=self.scope)
        second = self.add('next@example.test', instance_id=self.scope)
        self.cookie(identity=first)
        self.cookie(fixtures.COOKIE.replace(b'fixture', b'next-fixture'), identity=second)
        entered, release = threading.Event(), threading.Event()
        def probe(*args, **kwargs):
            entered.set()
            release.wait(5)
            return 'valid', '测试检测通过。'
        batch = self.app.extensions['account_check_batch']
        with patch('webapp.youtube_auth.probe', side_effect=probe) as mocked:
            response = self.client.post(PREFIX + '/batch-check', headers=self.headers,
                                        json={'items': self.selected(first, second)})
            try:
                self.assertTrue(entered.wait(3))
                duplicate = self.client.post(PREFIX + '/batch-check', headers=self.headers,
                                            json={'items': self.selected(second)})
                self.assertEqual(duplicate.status_code, 409)
                wrong = self.client.post(PREFIX + '/batch-check/cancel', headers=self.headers, json={'id': 'wrong'})
                self.assertEqual(wrong.status_code, 409)
                stopped = self.client.post(PREFIX + '/batch-check/cancel', headers=self.headers,
                                           json={'id': response.json['id']})
                self.assertEqual(stopped.status_code, 200)
            finally:
                release.set()
                batch.thread.join(5)
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual([r['state'] for r in batch.status()['items']], ['succeeded', 'cancelled'])

    def test_status_counts_cover_all_pages_and_cooldown_is_independent_of_login(self):
        valid = self.add('valid@example.test', instance_id=self.scope)
        missing = self.add('missing@example.test')
        self.cookie(identity=valid)
        with patch('webapp.youtube_auth.probe', return_value=('valid', '测试通过。')):
            self.check(self.selected(valid))
        inventory = AccountLibrary.inventory
        def cooling(lib, db, profiles=None):
            nodes, rows = inventory(lib, db, profiles)
            for row in rows:
                row['download_state'] = 'cooldown'
                row['session'] = {'cooldown_until': 9999999999}
            return nodes, rows
        with patch.object(AccountLibrary, 'inventory', cooling):
            data = self.client.get(PREFIX + '/overview?status=cooldown&size=1').json
            self.assertEqual(data['summary']['all'], 2)
            self.assertEqual(data['summary']['valid'], 1)
            self.assertEqual(data['summary']['needs_login'], 1)
            self.assertEqual(data['summary']['cooldown'], 1)
            self.assertEqual(data['total'], 1)
            self.assertEqual(data['items'][0]['auth_state'], 'valid')
            with patch('webapp.youtube_auth.probe') as probe:
                result = self.check(self.selected(valid))
            self.assertIn('冷却中', result['items'][0]['message'])
            self.assertFalse(probe.called)
        unbound = self.client.get(PREFIX + '/overview?instance=unbound').json
        self.assertEqual(unbound['summary']['all'], 1)
        self.assertEqual(unbound['items'][0]['id'], missing)
        pooled = self.client.get(PREFIX + '/overview?pooled=1').json
        self.assertEqual(pooled['summary']['all'], pooled['total'])

    def test_manual_pause_can_also_have_a_live_cooldown(self):
        item = AccountLibrary.account_item({'instance_id': 'fixture'}, [{
            'auth': {'state': 'verification_required'}, 'download_state': 'manual',
            'session': {'cooldown_until': 9999999999}}], {})
        self.assertEqual(item['download_state'], 'manual')
        self.assertTrue(item['cooling'])
        self.assertEqual(item['cooldown_until'], 9999999999)

    def test_check_requires_csrf_and_admin_and_rejects_invalid_selection(self):
        identity = self.add()
        self.assertEqual(self.client.post(PREFIX + '/batch-check', json={'items': self.selected(identity)}).status_code, 403)
        self.assertEqual(self.client.post(PREFIX + '/batch-check', headers=self.headers, json={'items': []}).status_code, 400)
        from sqlalchemy import update
        from webapp.database import console_users
        with self.store.connect() as db:
            db.execute(update(console_users).values(role='operator'))
        self.assertEqual(self.client.get(PREFIX + '/batch-check').status_code, 403)
        self.assertEqual(self.client.post(PREFIX + '/batch-check', headers=self.headers, json={'items': self.selected(identity)}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
