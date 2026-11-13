"""Seed account-owned credentials for scheduler tests, without a browser.

This fixture deliberately bypasses HTTP setup. Production CookieProfiles.add
and save_legacy are retired and their rejection is covered by API tests.
"""
from datetime import datetime, timezone
import uuid
from sqlalchemy import select

from Utils.youtube_login import parse_youtube_cookie_file
from webapp.cookie_profiles import CookieProfiles
from webapp.cookie_binding import claim, credential_keys
from webapp.database import youtube_accounts, youtube_account_cookies, cookie_profiles


class AccountCookieProfiles(CookieProfiles):
    def add(self, name, data, filename='cookies.txt'):
        return self.seed(name, data)

    def save_legacy(self, data):
        with self.store.connect() as db:
            old = db.execute(select(cookie_profiles.c.id).where(cookie_profiles.c.scope == self.scope,
                cookie_profiles.c.id == 'legacy')).first()
            if old:
                parse_youtube_cookie_file(data)
                claim(db, self.scope, credential_keys(data))
                db.execute(cookie_profiles.update().where(cookie_profiles.c.scope == self.scope,
                    cookie_profiles.c.id == 'legacy').values(content=data.decode('utf-8-sig'), deleted=0))
                return
        return self.seed('Legacy fixture', data, identity='legacy')

    def seed(self, name, data, *, identity=None):
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError('Invalid fixture name')
        parse_youtube_cookie_file(data)
        identity = identity or uuid.uuid4().hex
        account = uuid.uuid4().hex
        created = datetime.now(timezone.utc).isoformat()
        with self.store.connect() as db:
            claim(db, self.scope, credential_keys(data))
            db.execute(youtube_accounts.insert().values(id=account, email=account+'@fixture.test',
                normalized_email=account+'@fixture.test', secrets_ciphertext='test-fixture', key_id='fixture',
                note='', instance_id=self.scope, version=1, created_at=1, updated_at=1))
            db.execute(cookie_profiles.insert().values(scope=self.scope, id=identity, name=name,
                content=data.decode('utf-8-sig'), created_at=created, deleted=0))
            db.execute(youtube_account_cookies.insert().values(instance_id=self.scope, profile_id=identity,
                account_id=account, confirmed_by='fixture', created_at=1))
        return dict(id=identity, name=name, created_at=created, active=False)
