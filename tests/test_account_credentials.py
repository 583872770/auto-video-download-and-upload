"""Account-first credentials: replacement, ownership, cleanup and pacing."""
import io
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests import test_account_library as fixtures
COOKIE = fixtures.COOKIE
from webapp.account_library import AccountLibrary
from webapp.account_migration import cleanup_credentials, reconcile_config, upgrade_account_credentials
from webapp.account_routes import PREFIX
from webapp.database import (cookie_profiles, youtube_account_cookies as links,
    youtube_profile_auth, youtube_accounts, download_resources)


class AccountCredentialTests(unittest.TestCase):
    setUp = fixtures.AccountLibraryTests.setUp
    add = fixtures.AccountLibraryTests.add
    detail = fixtures.AccountLibraryTests.detail

    def upload(self, identity, data=COOKIE, **extra):
        return self.client.post(PREFIX+'/'+identity+'/upload', headers=self.headers, data={
            'version': str(self.detail(identity)['version']), 'confirmed': 'true',
            'cookie': (io.BytesIO(data), 'account.txt'), **extra})

    def test_reupload_replaces_in_place_and_preserves_pool_cooldown_and_encrypted_secrets(self):
        from Utils.config import load_config
        from webapp.cookie_pool import CookiePool
        from webapp.downloads import YouTubeAccountLimited
        identity = self.add(instance_id=self.scope)
        other = self.add('other@example.com', instance_id=self.scope)
        first = self.upload(identity).json['profile_id']
        second = self.upload(other, COOKIE.replace(b'fixture', b'other')).json['profile_id']
        config, base = load_config(self.config)
        config.update(youtube_cookie_pool=[first, second], download_methods=['yt_dlp'])
        pool = CookiePool(self.store, config, base, None)
        group = pool.accounts[0][2]
        pool.outcome(group, YouTubeAccountLimited('fixture account limit'))
        before = next(r for r in pool.status()['session_states'] if r['id'] == first)
        file_before = self.config.read_bytes()
        with self.store.connect() as db:
            secrets = db.execute(select(youtube_accounts.c.secrets_ciphertext).where(youtube_accounts.c.id == identity)).scalar_one()
        response = self.upload(identity, COOKIE.replace(b'fixture', b'replaced'))
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.json['profile_id'], first)
        self.assertEqual(self.detail(identity)['cookie_count'], 1)
        with self.store.connect() as db:
            self.assertEqual(len(db.execute(select(links).where(links.c.account_id == identity)).all()), 1)
            self.assertIn('replaced', db.execute(select(cookie_profiles.c.content).where(cookie_profiles.c.id == first)).scalar_one())
            self.assertEqual(db.execute(select(youtube_accounts.c.secrets_ciphertext).where(youtube_accounts.c.id == identity)).scalar_one(), secrets)
        after = next(r for r in CookiePool(self.store, config, base, None).status()['session_states'] if r['id'] == first)
        self.assertEqual(after['cooldown_until'], before['cooldown_until'])
        self.assertEqual(after['state'], 'cooldown')
        self.assertEqual(self.config.read_bytes(), file_before)

    def test_stale_invalid_and_unconfirmed_upload_leave_old_cookie_untouched(self):
        identity = self.add(instance_id=self.scope)
        profile = self.upload(identity).json['profile_id']
        for content, fields, code in ((COOKIE, {'version': '1'}, 409), (b'invalid', {}, 400),
                                      (COOKIE, {'confirmed': 'false'}, 400)):
            response = self.upload(identity, content, **fields)
            self.assertEqual(response.status_code, code, response.json)
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(cookie_profiles.c.content).where(cookie_profiles.c.id == profile)).scalar_one(), COOKIE.decode())

    def test_replacement_invalidates_only_this_accounts_auth(self):
        first, second = self.add(instance_id=self.scope), self.add('second@example.com', instance_id=self.scope)
        p1, p2 = self.upload(first).json['profile_id'], self.upload(second).json['profile_id']
        with self.store.connect() as db:
            db.execute(youtube_profile_auth.insert(), [dict(instance_id=self.scope, profile_id=p,
                fingerprint='fixture', context='fixture', auth='{}', last_valid_at=1) for p in (p1, p2)])
        self.assertEqual(self.upload(first).status_code, 201)
        with self.store.connect() as db:
            self.assertEqual(list(db.execute(select(youtube_profile_auth.c.profile_id)).scalars()), [p2])

    def test_legacy_entrypoints_cannot_create_orphan_credentials(self):
        for route in ('/api/youtube/cookie', '/api/youtube/cookies'):
            response = self.client.post(route, headers=self.headers, data={'cookie': (io.BytesIO(COOKIE), 'test.txt')})
            self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.client.post('/api/youtube/login', headers=self.headers, json={}).status_code, 400)
        with self.store.connect() as db:
            self.assertFalse(db.execute(select(cookie_profiles)).first())

    def test_account_required_before_cookie_upload_and_manual_link_is_retired(self):
        identity = self.add()
        self.assertEqual(self.upload(identity).status_code, 400)
        response = self.client.put(PREFIX+'/'+identity+'/cookies', headers=self.headers,
            json={'version': 1, 'confirmed': True, 'profile_ids': []})
        self.assertEqual(response.status_code, 400)

    def test_orphans_never_appear_or_execute_and_old_files_are_not_imported(self):
        from webapp.cookie_profiles import CookieProfiles
        orphan = 'f'*32
        with self.store.connect() as db:
            db.execute(cookie_profiles.insert().values(scope=self.scope, id=orphan, name='orphan',
                content=COOKIE.decode(), created_at='2026-11-01', deleted=0))
        directory = self.root/'Cookies'
        directory.mkdir()
        (directory/'youtube.txt').write_bytes(COOKIE)
        profiles = CookieProfiles(self.root, self.store)
        self.assertEqual(profiles.items(), [])
        self.assertEqual(self.client.get(PREFIX+'/overview').json['total'], 0)
        self.assertEqual(self.client.get(PREFIX+'/cookies').json['total'], 0)
        for method in (profiles.require, profiles.export):
            with self.assertRaises(ValueError): method(orphan)
        with self.assertRaises(ValueError): profiles.require('legacy')
        with self.assertRaises(ValueError): profiles.restore_selected(str(directory/'custom.txt'))

    def test_delete_account_erases_cookie_and_email_edit_cannot_reassign_credentials(self):
        identity = self.add(instance_id=self.scope)
        profile = self.upload(identity).json['profile_id']
        response = self.client.patch(PREFIX+'/'+identity, headers=self.headers,
            json={'version': self.detail(identity)['version'], 'email': 'different@example.com'})
        self.assertEqual(response.status_code, 409)
        result = self.client.delete(PREFIX+'/'+identity, headers=self.headers,
            json={'version': self.detail(identity)['version']})
        self.assertEqual(result.status_code, 200, result.json)
        with self.store.connect() as db:
            row = db.execute(select(cookie_profiles).where(cookie_profiles.c.id == profile)).mappings().one()
            self.assertEqual((row['deleted'], row['content']), (1, ''))
            self.assertFalse(db.execute(select(links)).first())

    def test_database_enforces_one_cookie_per_account(self):
        identity = self.add(instance_id=self.scope)
        self.upload(identity)
        with self.assertRaises(IntegrityError):
            with self.store.connect() as db:
                db.execute(links.insert().values(instance_id=self.scope, profile_id='f'*32,
                    account_id=identity, confirmed_by='test', created_at=1))

    def test_cleanup_keeps_latest_cookie_erases_orphan_and_is_repeatable(self):
        identity = self.add(instance_id=self.scope)
        old = self.upload(identity).json['profile_id']
        latest, orphan = 'a'*32, 'b'*32
        with self.store.db.engine.begin() as db:
            db.exec_driver_sql('DROP INDEX uq_youtube_account_cookie')
            db.execute(cookie_profiles.insert(), [dict(scope=self.scope, id=p, name=p,
                content=COOKIE.decode(), created_at='2026-11-13', deleted=0) for p in (latest, orphan)])
            db.execute(links.insert().values(instance_id=self.scope, profile_id=latest, account_id=identity,
                confirmed_by='test', created_at=9999999999))
            result = cleanup_credentials(db)
        self.assertEqual(set(result['removed']), {(self.scope, old), (self.scope, orphan)})
        config = {'youtube_cookie_pool': [old, latest, orphan], 'youtube_cookie': f'Cookies/profiles/{old}.txt'}
        cleaned = reconcile_config(config, {latest}, result['aliases'][self.scope])
        self.assertEqual(cleaned['youtube_cookie_pool'], [])
        self.assertTrue(cleaned['youtube_cookie'].endswith(latest+'.txt'))
        upgrade_account_credentials(self.store.db.engine)
        with self.store.connect() as db:
            again = cleanup_credentials(db)
            self.assertEqual(again['removed'], [])
            self.assertEqual(again['removed_links'], 0)
        self.assertEqual(self.detail(identity)['cookies'][0]['id'], latest)

    def test_startup_upgrade_cleans_orphan_cache_and_reconciles_selection(self):
        from app import create_app
        from webapp.database import schema_migrations
        import yaml
        identity = self.add(instance_id=self.scope)
        valid = self.upload(identity).json['profile_id']
        orphan = 'f'*32
        directory = self.root/'Cookies/profiles'
        directory.mkdir(parents=True)
        (directory/(orphan+'.txt')).write_bytes(COOKIE)
        with self.store.connect() as db:
            db.execute(cookie_profiles.insert().values(scope=self.scope, id=orphan, name='orphan',
                content=COOKIE.decode(), created_at='2026-11-01', deleted=0))
            db.execute(schema_migrations.delete().where(schema_migrations.c.version=='20261113_account_credentials'))
        saved = yaml.safe_load(self.config.read_text())
        saved.update(youtube_cookie_pool=[valid,orphan],youtube_cookie=f'Cookies/profiles/{orphan}.txt')
        self.config.write_text(yaml.safe_dump(saved),encoding='utf-8')
        # Database engines are reused within a test process; a fresh production
        # process initializes the schema before assembling the application.
        self.store.db.initialize()
        create_app(self.config, self.root/'state', start_worker=False)
        after = yaml.safe_load(self.config.read_text())
        self.assertEqual(after['youtube_cookie_pool'],[])
        self.assertEqual(after['youtube_cookie'],f'Cookies/profiles/{valid}.txt')
        self.assertFalse((directory/(orphan+'.txt')).exists())
        with self.store.connect() as db:
            row = db.execute(select(cookie_profiles).where(cookie_profiles.c.id==orphan)).mappings().one()
            self.assertEqual((row['deleted'],row['content']),(1,''))


if __name__ == '__main__':
    unittest.main()
