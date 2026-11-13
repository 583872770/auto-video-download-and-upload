"""Explicit Cookie exports preserve saved data and instance/admin boundaries."""
import re
import unittest
from urllib.parse import unquote

from sqlalchemy import select

from tests import test_cookie_profiles, test_fleet
from tests.account_cookie_fixture import AccountCookieProfiles as CookieProfiles
from webapp.database import cookie_profiles

COOKIE = test_cookie_profiles.COOKIE


class CookieExportTests(unittest.TestCase):
    setUp = test_cookie_profiles.CookieProfileTests.setUp
    add = test_cookie_profiles.CookieProfileTests.add
    action = test_cookie_profiles.CookieProfileTests.action

    def export(self, identity, **kwargs):
        return self.client.post('/api/youtube/cookies/export', json={'id': identity},
                                headers=self.headers, **kwargs)

    def test_export_reads_database_preserves_selection_and_can_be_imported(self):
        first = self.add().json['item']['id']
        second = self.add('备用账号 / 测试.txt', COOKIE.replace(b'PRIVATE_COOKIE_VALUE', b'SECOND_SECRET')).json['item']['id']
        profiles = CookieProfiles(self.root, self.store)
        cache = profiles.require(second)
        cache.write_bytes(b'tampered cache')
        config_before, runtime_before = self.config.read_bytes(), self.store.runtime()
        self.store.set_downloads_paused(False)
        response = self.export(second)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.mimetype, 'text/plain')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('ETag', response.headers)
        filename = unquote(response.headers['Content-Disposition'])
        self.assertTrue(filename.startswith('attachment;'))
        self.assertIn('备用账号 _ 测试-', filename)
        self.assertNotIn(' / ', filename)
        self.assertIn(b'SECOND_SECRET', response.data)
        self.assertNotIn(b'tampered cache', response.data)
        with self.store.connect() as db:
            saved = db.execute(select(cookie_profiles.c.content).where(cookie_profiles.c.id == second)).scalar_one()
        self.assertEqual(response.data, saved.encode('utf-8'))
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.store.runtime(), runtime_before)
        self.assertFalse(self.store.downloads_paused())
        self.assertEqual(cache.read_bytes(), b'tampered cache')
        imported = self.add('Imported backup', response.data)
        self.assertEqual(imported.status_code, 201, imported.json)
        self.assertEqual([item['id'] for item in self.client.get('/api/youtube/cookies').json['items']
                          if item['active']], [first])

    def test_legacy_and_expired_saved_content_can_be_exported_without_cache(self):
        profiles = CookieProfiles(self.root, self.store)
        profiles.save_legacy(COOKIE)
        expired = COOKIE.replace(b'\t0\tSID', b'\t1\tSID')
        with self.store.connect() as db:
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id == 'legacy').values(content=expired.decode()))
        self.assertFalse(profiles.path('legacy').exists())
        response = self.export('legacy')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, expired)
        self.assertFalse(profiles.path('legacy').exists())

    def test_missing_deleted_and_path_ids_do_not_export_credentials(self):
        identity = self.add().json['item']['id']
        self.assertEqual(self.action('delete', identity).status_code, 200)
        for value in (identity, '0' * 32, '../youtube.txt', None, ['legacy']):
            response = self.export(value)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn('PRIVATE_COOKIE_VALUE', response.text)
            self.assertNotIn('Content-Disposition', response.headers)
        self.assertEqual(self.client.get('/api/youtube/cookies/export').status_code, 405)

    def test_export_uses_existing_admin_and_csrf_controls(self):
        identity = self.add().json['item']['id']
        self.assertEqual(self.client.post('/api/youtube/cookies/export', json={'id': identity}).status_code, 403)
        response = self.client.post('/api/auth/setup', json={
            'username': 'admin', 'password': 'fixture-export-password',
            'bootstrap_key': self.app.extensions['console_auth'].bootstrap_key}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        def token(client):
            return {'X-CSRF-Token': re.search(r'name="csrf-token" content="([^"]+)"', client.get('/login').text)[1]}
        self.headers = token(self.client)
        for username, role in [('reader', 'viewer'), ('operator', 'operator')]:
            response = self.client.post('/api/auth/users', json={
                'username': username, 'role': role, 'password': 'fixture-export-password'}, headers=self.headers)
            self.assertEqual(response.status_code, 201, response.json)
            client = self.app.test_client()
            response = client.post('/api/auth/login', json={
                'username': username, 'password': 'fixture-export-password'}, headers=token(client))
            self.assertEqual(response.status_code, 200, response.json)
            response = client.post('/api/youtube/cookies/export', json={'id': identity}, headers=token(client))
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('PRIVATE_COOKIE_VALUE', response.text)
        anonymous = self.app.test_client()
        response = anonymous.post('/api/youtube/cookies/export', json={'id': identity}, headers=token(anonymous))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.export(identity).status_code, 200)


class CookieExportFleetTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet
    serve = test_fleet.FleetTests.serve

    def test_download_attachment_is_forwarded_from_selected_instance_only(self):
        for name in ('manager', 'alpha', 'beta'):
            store = self.apps[name].extensions['task_store']
            CookieProfiles(self.root / name, store).save_legacy(
                COOKIE.replace(b'PRIVATE_COOKIE_VALUE', name.encode() + b'_SECRET'))
        self.serve('alpha')
        response = self.client.post('/api/youtube/cookies/export', json={'id': 'legacy'},
                                    headers={**self.headers, 'X-Instance-ID': 'alpha'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(b'alpha_SECRET', response.data)
        self.assertNotIn(b'manager_SECRET', response.data)
        self.assertNotIn(b'beta_SECRET', response.data)
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        foreign = CookieProfiles(self.root / 'beta', self.b).add('beta only', COOKIE)['id']
        denied = self.client.post('/api/youtube/cookies/export', json={'id': foreign},
                                 headers={**self.headers, 'X-Instance-ID': 'alpha'})
        self.assertEqual(denied.status_code, 400)
        self.assertNotIn('PRIVATE_COOKIE_VALUE', denied.text)
