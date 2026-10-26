"""Operator login, permissions, revocation and safe first-account initialization."""
import re
import unittest

from tests import test_scale


class ConsoleAuthTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def token(self, client):
        page = client.get('/login')
        return {'X-CSRF-Token': re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]}

    def setup_admin(self):
        key = self.app.extensions['console_auth'].bootstrap_key
        response = self.client.post('/api/auth/setup', json=dict(username='admin', password='secure-test-password',
            bootstrap_key=key), headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        self.headers = self.token(self.client)

    def login(self, username, password='secure-test-password'):
        client = self.app.test_client()
        headers = self.token(client)
        response = client.post('/api/auth/login', json=dict(username=username, password=password), headers=headers)
        self.assertEqual(response.status_code, 200, response.json)
        return client, self.token(client)

    def test_first_account_requires_bootstrap_key_and_network_is_closed_before_setup(self):
        self.assertEqual(self.client.get('/jobs').status_code, 200)
        remote = self.app.test_client()
        response = remote.get('/api/jobs', environ_overrides={'REMOTE_ADDR':'192.0.2.1'})
        self.assertEqual(response.status_code, 401)
        response = remote.get('/jobs', environ_overrides={'REMOTE_ADDR':'192.0.2.1'}, headers={'X-Forwarded-For':'127.0.0.1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/login')
        response = self.client.post('/api/auth/setup', json=dict(username='admin', password='secure-test-password',
            bootstrap_key='wrong'), headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.setup_admin()
        self.assertEqual(self.client.get('/api/jobs').status_code, 200)
        self.assertEqual(remote.get('/api/jobs').status_code, 401)
        self.assertEqual(remote.get('/jobs').status_code, 302)
        # Local-only access also ends after an administrator has been created.
        self.assertEqual(self.app.test_client().get('/api/jobs').status_code, 401)

    def test_login_csrf_logout_and_failed_password(self):
        self.setup_admin()
        client = self.app.test_client()
        self.assertEqual(client.post('/api/auth/login', json=dict(username='admin',password='secure-test-password')).status_code, 403)
        headers = self.token(client)
        response = client.post('/api/auth/login', json=dict(username='admin',password='wrong'), headers=headers)
        self.assertEqual(response.status_code, 401)
        client, headers = self.login('admin')
        self.assertEqual(client.get('/api/auth/status').json['user']['role'], 'admin')
        self.assertEqual(client.post('/api/auth/logout', headers=headers).status_code, 200)
        self.assertEqual(client.get('/api/jobs').status_code, 401)

    def test_roles_are_enforced_and_disabled_sessions_are_revoked(self):
        self.setup_admin()
        for name, role in [('reader','viewer'), ('operator','operator')]:
            response = self.client.post('/api/auth/users', json=dict(username=name, role=role,
                password='secure-test-password'), headers=self.headers)
            self.assertEqual(response.status_code, 201, response.json)
        reader, reader_headers = self.login('reader')
        operator, operator_headers = self.login('operator')
        self.assertEqual(reader.get('/api/jobs').status_code, 200)
        self.assertEqual(reader.post('/api/jobs', json=dict(customer='A',name='B'), headers=reader_headers).status_code, 403)
        self.assertEqual(reader.get('/api/auth/users').status_code, 403)
        self.assertEqual(operator.post('/api/jobs', json=dict(customer='A',name='B'), headers=operator_headers).status_code, 201)
        self.assertEqual(operator.put('/api/config', json=dict(worker_count=2), headers=operator_headers).status_code, 403)
        self.assertEqual(operator.post('/api/auth/users', json=dict(username='intruder'), headers=operator_headers).status_code, 403)
        response = self.client.put('/api/auth/users/reader', json=dict(enabled=False), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(reader.get('/api/jobs').status_code, 401)
        self.assertEqual(self.client.put('/api/auth/users/admin', json=dict(enabled=False), headers=self.headers).status_code, 409)
        self.assertEqual(self.client.put('/api/auth/users/admin', json=dict(role='viewer'), headers=self.headers).status_code, 409)
        accounts = self.client.get('/api/auth/users').json
        self.assertNotIn('password', str(accounts))

    def test_password_reset_invalidates_existing_sessions_and_never_resets_bootstrap(self):
        self.setup_admin()
        self.client.post('/api/auth/users', json=dict(username='reader', role='viewer', password='secure-test-password'), headers=self.headers)
        reader, _ = self.login('reader')
        response = self.client.put('/api/auth/users/reader', json=dict(password='a-new-test-password'), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reader.get('/api/jobs').status_code, 401)
        reader, _ = self.login('reader','a-new-test-password')
        self.assertEqual(reader.get('/api/jobs').status_code, 200)
        response = self.client.post('/api/auth/setup', json=dict(username='replacement', password='secure-test-password',
            bootstrap_key=self.app.extensions['console_auth'].bootstrap_key), headers=self.headers)
        self.assertEqual(response.status_code, 409)
