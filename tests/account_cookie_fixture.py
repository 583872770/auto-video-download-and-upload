"""Prepare account-owned credentials for scheduler and HTTP tests.

HTTP helpers exercise real authentication and account uploads. The
AccountCookieProfiles fixture bypasses HTTP setup for scheduler tests.
Production CookieProfiles.add and save_legacy stay retired.
"""
from datetime import datetime, timezone
import base64
from io import BytesIO
import os
import re
from unittest.mock import patch
import uuid
from sqlalchemy import select

from Utils.youtube_login import parse_youtube_cookie_file
from webapp.cookie_profiles import CookieProfiles
from webapp.cookie_binding import claim, credential_keys
from webapp.database import youtube_accounts, youtube_account_cookies, cookie_profiles


def sign_in_cookie_admin(test, client, headers):
    """Use real sessions and CSRF when a Cookie API test needs an administrator."""
    status = client.get('/api/auth/status').json
    if status['user']:
        test.assertEqual(status['user']['role'], 'admin')
        return
    credentials = {'username': 'cookie-fixture-admin', 'password': 'cookie-fixture-password'}
    path = '/api/auth/login'
    if not status['configured']:
        path = '/api/auth/setup'
        credentials['bootstrap_key'] = client.application.extensions['console_auth'].bootstrap_key
    response = client.post(path, json=credentials, headers=headers)
    test.assertEqual(response.status_code, 201 if path.endswith('/setup') else 200, response.json)
    headers['X-CSRF-Token'] = re.search(
        r'name="csrf-token" content="([^"]+)"', client.get('/login').text)[1]


def upload_account_cookie(test, client, headers, data, instance_id):
    """Create an owned credential through the supported account upload API."""
    from webapp.account_routes import PREFIX
    sign_in_cookie_admin(test, client, headers)
    with patch.dict(os.environ, {'VIDEO_ACCOUNT_ENCRYPTION_KEY': base64.urlsafe_b64encode(b'f' * 32).decode()}):
        response = client.post(PREFIX, headers=headers, json={
            'email': uuid.uuid4().hex + '@fixture.test', 'password': 'account-fixture-password',
            'instance_id': instance_id})
        test.assertEqual(response.status_code, 201, response.json)
        response = client.post(PREFIX + '/' + response.json['id'] + '/upload', headers=headers,
            data={'cookie': (BytesIO(data), 'fixture.txt'), 'version': '1', 'confirmed': 'true'})
        test.assertEqual(response.status_code, 201, response.json)
        return response.json['profile_id']


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
