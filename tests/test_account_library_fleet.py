"""Every frontend shares records; credential actions execute only on the owner."""
import base64
from io import BytesIO
import os
import re
import unittest
from unittest.mock import patch

from sqlalchemy import select
from tests import test_fleet
from webapp.account_routes import PREFIX
from webapp.database import instances, youtube_accounts, cookie_profiles

COOKIE=b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfleet-account-test\n'


class AccountLibraryFleetTests(unittest.TestCase):
    setup_fleet=test_fleet.FleetTests.setup_fleet
    serve=test_fleet.FleetTests.serve

    def setUp(self):
        env=patch.dict(os.environ,{'VIDEO_ACCOUNT_ENCRYPTION_KEY':base64.urlsafe_b64encode(b'f'*32).decode()})
        env.start();self.addCleanup(env.stop)
        test_fleet.FleetTests.setUp(self)
        response=self.client.post('/api/auth/setup',headers=self.headers,json={'username':'admin','password':'fleet-test-password',
            'bootstrap_key':self.apps['manager'].extensions['console_auth'].bootstrap_key})
        self.assertEqual(response.status_code,201,response.json)
        self.headers={'X-CSRF-Token':re.search(r'name="csrf-token" content="([^"]+)"',self.client.get('/').text)[1]}
        self.serve('alpha');self.serve('beta')

    def add(self, email='fleet@example.com', target='beta'):
        result=self.client.post(PREFIX,headers={**self.headers,'X-Instance-ID':'alpha'},json={
            'email':email,'password':'dummy-fleet-secret','instance_id':target})
        self.assertEqual(result.status_code,201,result.json)
        return result.json['id']

    def upload(self, identity):
        return self.client.post(PREFIX+'/'+identity+'/upload',headers={**self.headers,'X-Instance-ID':'alpha'},
            data={'version':'1','confirmed':'true','cookie':(BytesIO(COOKIE),'test.txt')})

    def detail(self, identity):
        return self.client.get(PREFIX+'/'+identity).json['item']

    def test_upload_and_check_target_database_owner_not_selected_frontend(self):
        identity=self.add()
        before={i:(self.root/i/'config.yaml').read_bytes() for i in self.apps}
        result=self.upload(identity)
        self.assertEqual(result.status_code,201,result.json)
        item=self.detail(identity)
        self.assertEqual(item['cookies'][0]['instance_id'],'beta')
        overview=self.client.get(PREFIX+'/overview?instance=beta',headers={'X-Instance-ID':'alpha'}).json
        self.assertEqual(overview['total'],1)
        self.assertEqual(overview['items'][0]['cookies'][0]['instance_id'],'beta')
        self.assertEqual(self.client.get(PREFIX+'/overview?instance=alpha').json['total'],0)
        with self.manager.connect() as db:
            self.assertEqual(set(db.execute(select(cookie_profiles.c.scope)).scalars()),{'beta'})
        with patch('webapp.youtube_auth.probe',return_value=('valid','Fixture valid')) as probe:
            result=self.client.post(PREFIX+'/'+identity+'/check',headers={**self.headers,'X-Instance-ID':'alpha'},
                json={'profile_id':item['cookies'][0]['id']})
        self.assertEqual(result.status_code,200,result.json)
        probe.assert_called_once()
        self.assertEqual(self.detail(identity)['auth_state'],'valid')
        self.assertEqual({i:(self.root/i/'config.yaml').read_bytes() for i in self.apps},before)
        beta=self.apps['beta'].test_client()
        token=re.search(r'name="csrf-token" content="([^"]+)"',beta.get('/login').text)[1]
        response=beta.post('/api/auth/login',headers={'X-CSRF-Token':token},json={'username':'admin','password':'fleet-test-password'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(beta.get(PREFIX,headers={'X-Instance-ID':'alpha'}).json['total'],1)
        fleet=self.apps['beta'].extensions['fleet']
        self.assertEqual(beta.post(PREFIX+'/'+identity+'/secrets',headers={'Authorization':'Bearer '+fleet.token},json={'field':'password'}).status_code,403)

    def test_offline_owner_never_falls_back_to_entry(self):
        identity=self.add()
        with self.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id=='beta').values(heartbeat=0))
        result=self.upload(identity)
        self.assertGreaterEqual(result.status_code,400)
        with self.manager.connect() as db:
            self.assertFalse(db.execute(select(cookie_profiles)).first())
        self.assertEqual(self.detail(identity)['instance_id'],'beta')

    def test_renaming_linked_account_is_rejected(self):
        identity=self.add()
        self.assertEqual(self.upload(identity).status_code,201)
        response=self.client.patch(PREFIX+'/'+identity,headers=self.headers,json={
            'version':self.detail(identity)['version'],'email':'renamed@example.com'})
        self.assertEqual(response.status_code,409,response.json)
        self.assertEqual(self.detail(identity)['email'],'fleet@example.com')

    def test_cross_instance_link_and_reimport_do_not_release_ownership(self):
        identity=self.add()
        self.assertEqual(self.upload(identity).status_code,201)
        item=self.detail(identity)
        response=self.client.patch(PREFIX+'/'+identity,headers=self.headers,json={'version':item['version'],'instance_id':'alpha'})
        self.assertEqual(response.status_code,409)
        response=self.client.put(PREFIX+'/'+identity+'/cookies',headers=self.headers,json={
            'version':item['version'],'instance_id':'alpha','profile_ids':[item['cookies'][0]['id']],'confirmed':True})
        self.assertEqual(response.status_code,400)
        response=self.client.put(PREFIX+'/'+identity+'/cookies',headers=self.headers,json={
            'version':item['version'],'instance_id':'beta','profile_ids':[],'confirmed':True})
        self.assertEqual(response.status_code,400)
        version=self.detail(identity)['version']
        response=self.client.delete(PREFIX+'/'+identity,headers=self.headers,json={'version':version})
        self.assertEqual(response.status_code,200,response.json)
        response=self.client.post(PREFIX,headers=self.headers,json={'email':'fleet@example.com','password':'new','instance_id':'alpha'})
        self.assertEqual(response.status_code,409)
        with self.manager.connect() as db:
            self.assertFalse(db.execute(select(youtube_accounts)).first())


if __name__=='__main__':unittest.main()
