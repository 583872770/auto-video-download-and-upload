"""Browser credentials, owner routing and atomic Cookie association contracts."""
import asyncio
import json
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests import test_account_library as library_tests, test_account_library_fleet as fleet_tests
from webapp.account_routes import PREFIX
from webapp.database import cookie_profiles, youtube_account_cookies, youtube_account_owners


class BrowserFixture:
    def __init__(self):
        self.ready, self.complete = threading.Event(), threading.Event()
        self.options, self.account_name = None, None

    async def run(self, config, output, *, on_status, cancelled, **options):
        from scripts.login_youtube import LoginCancelled
        self.options = options
        on_status('waiting', 'Fixture manual challenge')
        self.ready.set()
        while not self.complete.is_set():
            if cancelled.is_set():
                raise LoginCancelled('登录已取消。')
            await asyncio.sleep(0.01)
        output.write_bytes(library_tests.COOKIE)
        return {'account_name': self.account_name or options['expected_email']}


class AccountBrowserLoginTests(unittest.TestCase):
    add, detail = library_tests.AccountLibraryTests.add, library_tests.AccountLibraryTests.detail

    def setUp(self):
        library_tests.AccountLibraryTests.setUp(self)
        self.fixture = BrowserFixture()
        self.manager = self.app.extensions['youtube_login']
        mock = patch('webapp.youtube_login.login', self.fixture.run)
        mock.start(); self.addCleanup(mock.stop)

        def cleanup():
            self.manager.cancel()
            if self.manager.thread:
                self.manager.thread.join(timeout=5)
        self.addCleanup(cleanup)

    def start(self, identity, **data):
        return self.client.post(PREFIX+'/'+identity+'/login', headers=self.headers,
            json={'version':self.detail(identity)['version'], 'automatic':True, **data})

    def finish(self, identity):
        self.fixture.complete.set()
        self.manager.thread.join(timeout=5)
        self.assertFalse(self.manager.thread.is_alive())
        return self.client.get(PREFIX+'/'+identity+'/login').json

    def count(self, table):
        with self.store.connect() as db:
            return len(db.execute(select(table)).all())

    def test_automatic_login_keeps_password_in_memory_and_links_without_changing_pool(self):
        identity = self.add(instance_id=self.scope)
        before = self.config.read_bytes()
        result = self.start(identity)
        self.assertEqual(result.status_code,202,result.json)
        self.assertTrue(self.fixture.ready.wait(5))
        self.assertEqual(self.fixture.options['credentials']['password'],' a&!#Secret 123 ')
        self.assertEqual(self.fixture.options['credentials']['code_url'],'http://example.com/')
        self.assertNotIn('extra',self.fixture.options['credentials'])
        self.assertNotIn('Secret',result.text)
        self.assertNotIn('Secret',self.manager.path.read_text(encoding='utf-8'))
        self.assertEqual(self.finish(identity)['state'],'succeeded')
        item=self.detail(identity)
        self.assertEqual(item['cookie_count'],1)
        self.assertFalse(item['cookies'][0]['pooled'])
        self.assertEqual(self.config.read_bytes(),before)
        self.assertEqual(self.count(youtube_account_owners),1)

    def test_unbound_requires_explicit_instance_and_respects_version(self):
        identity=self.add()
        self.assertEqual(self.start(identity).status_code,400)
        result=self.start(identity,instance_id=self.scope,version=7)
        self.assertEqual(result.status_code,409)
        self.assertEqual(self.start(identity,instance_id=self.scope).status_code,202)
        self.assertTrue(self.fixture.ready.wait(5))
        self.assertEqual(self.detail(identity)['instance_id'],self.scope)
        self.assertEqual(self.finish(identity)['state'],'succeeded')

    def test_wrong_email_never_saves_cookie_or_association(self):
        identity=self.add(instance_id=self.scope)
        self.fixture.account_name='wrong@example.com'
        self.assertEqual(self.start(identity).status_code,202)
        self.assertTrue(self.fixture.ready.wait(5))
        status=self.finish(identity)
        self.assertEqual(status['state'],'failed')
        self.assertIn('邮箱',status['message'])
        self.assertEqual(self.count(cookie_profiles),0)
        self.assertEqual(self.count(youtube_account_cookies),0)
        self.assertEqual(self.count(youtube_account_owners),0)

    def test_edit_during_login_fences_commit_and_leaves_no_orphan_cookie(self):
        identity=self.add(instance_id=self.scope)
        self.assertEqual(self.start(identity).status_code,202)
        self.assertTrue(self.fixture.ready.wait(5))
        self.client.patch(PREFIX+'/'+identity,headers=self.headers,json={'version':1,'note':'changed while logging in'})
        self.assertEqual(self.finish(identity)['state'],'failed')
        self.assertEqual(self.count(cookie_profiles),0)
        self.assertEqual(self.count(youtube_account_cookies),0)

    def test_other_account_cannot_cancel_login_and_stale_session_is_rejected(self):
        identity=self.add(instance_id=self.scope)
        other=self.add('other@example.com',instance_id=self.scope)
        started=self.start(identity)
        self.assertEqual(started.status_code,202)
        self.assertTrue(self.fixture.ready.wait(5))
        other_status=self.client.get(PREFIX+'/'+other+'/login').json
        self.assertEqual(other_status['state'],'busy')
        self.assertFalse(other_status['can_cancel'])
        for account,session in ((other,started.json['id']),(identity,'stale')):
            result=self.client.post(PREFIX+'/'+account+'/login/cancel',headers=self.headers,json={'session_id':session})
            self.assertEqual(result.status_code,409)
        result=self.client.post(PREFIX+'/'+identity+'/login/cancel',headers=self.headers,json={'session_id':started.json['id']})
        self.assertEqual(result.status_code,200)
        self.manager.thread.join(5)
        self.assertEqual(self.count(cookie_profiles),0)

    def test_manual_login_does_not_decrypt_and_csrf_is_required(self):
        identity=self.add(instance_id=self.scope)
        self.assertEqual(self.client.post(PREFIX+'/'+identity+'/login',json={'version':1}).status_code,403)
        with patch('webapp.account_library.AccountSecrets',side_effect=ValueError('no key')):
            self.assertEqual(self.start(identity).status_code,400)
            self.assertEqual(self.start(identity,automatic=False).status_code,202)
        self.assertTrue(self.fixture.ready.wait(5))
        self.assertIsNone(self.fixture.options['credentials'])
        self.assertEqual(self.finish(identity)['state'],'succeeded')


class AccountBrowserFleetTests(unittest.TestCase):
    setup_fleet=fleet_tests.AccountLibraryFleetTests.setup_fleet
    serve=fleet_tests.AccountLibraryFleetTests.serve
    add=fleet_tests.AccountLibraryFleetTests.add

    def setUp(self):
        fleet_tests.AccountLibraryFleetTests.setUp(self)
        self.fixture=BrowserFixture()
        mock=patch('webapp.youtube_login.login',self.fixture.run)
        mock.start();self.addCleanup(mock.stop)
        def cleanup():
            for app in self.apps.values():
                manager=app.extensions['youtube_login']
                manager.cancel()
                if manager.thread:manager.thread.join(5)
        self.addCleanup(cleanup)

    def test_selected_frontend_never_changes_owner_and_remote_result_links_atomically(self):
        identity=self.add()
        before={i:(self.root/i/'config.yaml').read_bytes() for i in self.apps}
        result=self.client.post(PREFIX+'/'+identity+'/login',headers={**self.headers,'X-Instance-ID':'alpha'},
            json={'version':1,'automatic':True})
        self.assertEqual(result.status_code,202,result.json)
        self.assertTrue(self.fixture.ready.wait(5))
        self.assertTrue(self.apps['beta'].extensions['youtube_login'].status()['active'])
        self.assertFalse(self.apps['alpha'].extensions['youtube_login'].status()['active'])
        self.assertFalse(self.apps['manager'].extensions['youtube_login'].status()['active'])
        self.fixture.complete.set()
        self.apps['beta'].extensions['youtube_login'].thread.join(5)
        status=self.client.get(PREFIX+'/'+identity+'/login',headers={'X-Instance-ID':'alpha'}).json
        self.assertEqual(status['state'],'succeeded',status)
        item=self.client.get(PREFIX+'/'+identity).json['item']
        self.assertEqual(item['cookies'][0]['instance_id'],'beta')
        self.assertFalse(item['cookies'][0]['pooled'])
        self.assertEqual({i:(self.root/i/'config.yaml').read_bytes() for i in self.apps},before)
        self.assertNotIn('dummy-fleet-secret',json.dumps(status))

    def test_offline_owner_has_no_fallback_and_operator_cannot_use_saved_password(self):
        from webapp.database import instances,console_users
        identity=self.add()
        with self.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id=='beta').values(heartbeat=0))
        result=self.client.post(PREFIX+'/'+identity+'/login',headers=self.headers,json={'version':1,'automatic':True})
        self.assertEqual(result.status_code,400)
        self.assertFalse(self.fixture.ready.is_set())
        with self.manager.connect() as db:
            db.execute(console_users.update().values(role='operator'))
        result=self.client.post(PREFIX+'/'+identity+'/login',headers=self.headers,json={'version':1,'automatic':True})
        self.assertEqual(result.status_code,403)
