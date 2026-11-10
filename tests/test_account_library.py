"""Account secrets, import fences, ownership and global state without probes."""
import base64
import io
import json
import os
import re
import time
import unittest
from unittest.mock import patch

from sqlalchemy import event, select

from tests import test_web
from webapp.account_import import parse_import
from webapp.account_routes import PREFIX
from webapp.database import (youtube_accounts as accounts, youtube_account_audit as audit,
    cookie_profiles, youtube_profile_auth)

KEY = base64.urlsafe_b64encode(b'k' * 32).decode()
COOKIE = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\taccount-library-fixture\n'


class AccountLibraryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'VIDEO_ACCOUNT_ENCRYPTION_KEY':KEY})
        self.env.start(); self.addCleanup(self.env.stop)
        test_web.WebTests.setUp(self)
        response = self.client.post('/api/auth/setup', headers=self.headers, json={
            'bootstrap_key': self.app.extensions['console_auth'].bootstrap_key,
            'username':'admin', 'password':'account-library-test-password'})
        self.assertEqual(response.status_code,201,response.json)
        self.headers = {'X-CSRF-Token':re.search(r'name="csrf-token" content="([^"]+)"',self.client.get('/').text)[1]}
        self.scope = self.client.get(PREFIX).json['instances'][0]['id']

    def add(self, email='sample@example.com', **extra):
        response = self.client.post(PREFIX,headers=self.headers,json={
            'email':email,'password':' a&!#Secret 123 ','extra':'opaque-test-data','code_url':'http://example.com/',**extra})
        self.assertEqual(response.status_code,201,response.json)
        return response.json['id']

    def detail(self, identity):
        return self.client.get(PREFIX+'/'+identity).json['item']

    def cookie(self, value=COOKIE):
        response = self.client.post('/api/youtube/cookies',headers=self.headers,
            data={'cookie':(io.BytesIO(value),'fixture.txt'),'activate':'false'})
        self.assertEqual(response.status_code,201,response.json)
        return response.json['item']['id']

    def link(self, identity, profiles, **extra):
        return self.client.put(PREFIX+'/'+identity+'/cookies',headers=self.headers,json={
            'version':self.detail(identity)['version'],'instance_id':self.scope,
            'confirmed':True,'profile_ids':profiles,**extra})

    def test_encrypted_roundtrip_public_queries_and_blank_password(self):
        identity = self.add(' Sample@Example.COM ')
        detail = self.detail(identity)
        self.assertEqual(detail['email'],'Sample@Example.COM')
        self.assertNotIn('password',detail)
        self.assertNotIn('opaque-test-data',json.dumps(detail))
        with self.store.connect() as db:
            raw = dict(db.execute(select(accounts)).mappings().one())
        self.assertNotIn('Secret',raw['secrets_ciphertext'])
        result = self.client.post(PREFIX+'/'+identity+'/secrets',headers=self.headers,json={'field':'password'})
        self.assertEqual(result.json['value'],' a&!#Secret 123 ')
        self.assertEqual(result.headers['Cache-Control'],'no-store')
        result = self.client.patch(PREFIX+'/'+identity,headers=self.headers,json={'version':1,'password':'','note':'updated'})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(self.client.post(PREFIX+'/'+identity+'/secrets',headers=self.headers,json={'field':'password'}).json['value'],' a&!#Secret 123 ')
        duplicate = self.client.post(PREFIX,headers=self.headers,json={'email':'sample@example.com','password':'other'})
        self.assertEqual(duplicate.status_code,409)
        with self.store.connect() as db:
            rows = list(db.execute(select(audit)).mappings())
        self.assertEqual(len(rows),4)
        self.assertNotIn('Secret',json.dumps([dict(r) for r in rows]))

    def test_missing_wrong_and_tampered_keys_never_fall_back_to_plaintext(self):
        identity = self.add()
        with patch.dict(os.environ,{'VIDEO_ACCOUNT_ENCRYPTION_KEY':''}):
            self.assertEqual(self.client.post(PREFIX,headers=self.headers,json={'email':'new@example.com','password':'abc'}).status_code,400)
            self.assertEqual(self.client.get(PREFIX).status_code,200)
        with patch.dict(os.environ,{'VIDEO_ACCOUNT_ENCRYPTION_KEY':base64.urlsafe_b64encode(b'x'*32).decode()}):
            self.assertFalse(self.client.get(PREFIX+'/capabilities').json['encryption_ready'])
            self.assertEqual(self.client.post(PREFIX+'/'+identity+'/secrets',headers=self.headers,json={'field':'password'}).status_code,400)
        with self.store.connect() as db:
            db.execute(accounts.update().where(accounts.c.id==identity).values(secrets_ciphertext='broken'))
        self.assertEqual(self.client.post(PREFIX+'/'+identity+'/secrets',headers=self.headers,json={'field':'password'}).status_code,400)

    def test_version_conflict_and_explicit_clearing(self):
        identity = self.add()
        result = self.client.patch(PREFIX+'/'+identity,headers=self.headers,json={'version':1,'extra':''})
        self.assertEqual(result.status_code,200)
        result = self.client.patch(PREFIX+'/'+identity,headers=self.headers,json={'version':1,'note':'stale'})
        self.assertEqual(result.status_code,409)
        self.assertEqual(self.detail(identity)['note'],'')
        self.assertEqual(self.client.post(PREFIX+'/'+identity+'/secrets',headers=self.headers,json={'field':'extra'}).json['value'],'')

    def preview(self, text, **extra):
        response = self.client.post(PREFIX+'/import/preview',headers=self.headers,json={'text':text,**extra})
        self.assertEqual(response.status_code,200,response.json)
        return response.json

    def test_import_preserves_special_characters_and_preview_masks_secrets(self):
        raw = '\ufeffone@example.com---- &!# pass ----opaque----接码网址：http://example.com/\r\n\r\ntwo@example.com----second'
        preview = self.preview(raw)
        self.assertEqual(preview['errors'],0)
        self.assertNotIn('opaque',json.dumps(preview))
        self.assertNotIn('pass',json.dumps(preview).replace('password',''))
        self.assertEqual(self.client.get(PREFIX).json['total'],0)
        result = self.client.post(PREFIX+'/import',headers=self.headers,json={'token':preview['token'],'text':raw})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['created'],2)
        item = next(i for i in self.client.get(PREFIX).json['items'] if i['email']=='one@example.com')
        self.assertEqual(self.client.post(PREFIX+'/'+item['id']+'/secrets',headers=self.headers,json={'field':'password'}).json['value'],' &!# pass ')

    def test_bad_import_reports_line_without_secret_and_does_not_partially_commit(self):
        raw = 'one@example.com----secret\nONE@example.com----secret2\nother@example.com----secret----extra----javascript:alert(1)\nx@example.com----secret----one----http://example.com/----unexpected'
        preview = self.preview(raw)
        self.assertEqual(preview['errors'],3)
        self.assertNotIn('secret',json.dumps(preview))
        result = self.client.post(PREFIX+'/import',headers=self.headers,json={'token':preview['token'],'text':raw})
        self.assertEqual(result.status_code,400)
        self.assertEqual(self.client.get(PREFIX).json['total'],0)
        with self.assertRaises(ValueError): parse_import('x'*(1024*1024+1))

    def test_import_fences_changed_input_and_concurrent_updates(self):
        identity = self.add(instance_id=self.scope)
        raw = 'sample@example.com----changed\nnew@example.com----second'
        preview = self.preview(raw,mode='update')
        changed = self.client.post(PREFIX+'/import',headers=self.headers,json={'token':preview['token'],'text':raw+'x'})
        self.assertEqual(changed.status_code,409)
        self.client.patch(PREFIX+'/'+identity,headers=self.headers,json={'version':1,'note':'other editor'})
        conflict = self.client.post(PREFIX+'/import',headers=self.headers,json={'token':preview['token'],'text':raw})
        self.assertEqual(conflict.status_code,409)
        self.assertEqual(self.client.get(PREFIX).json['total'],1)
        preview = self.preview(raw,mode='update')
        result = self.client.post(PREFIX+'/import',headers=self.headers,json={'token':preview['token'],'text':raw})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(self.detail(identity)['instance_id'],self.scope)
        self.assertEqual(self.detail(identity)['note'],'other editor')
        self.assertEqual(self.client.post(PREFIX+'/'+identity+'/secrets',headers=self.headers,json={'field':'extra'}).json['value'],'opaque-test-data')

    def test_link_requires_confirmation_and_is_exclusive(self):
        first, second = self.add(), self.add('other@example.com')
        profile = self.cookie()
        self.assertEqual(self.link(first,[profile],confirmed=False).status_code,400)
        self.assertEqual(self.link(first,[profile]).status_code,200)
        self.assertEqual(self.link(second,[profile]).status_code,409)
        self.assertEqual(self.detail(first)['cookie_count'],1)
        self.assertEqual(self.detail(second)['cookie_count'],0)
        version=self.detail(first)['version']
        self.assertEqual(self.client.patch(PREFIX+'/'+first,headers=self.headers,json={'version':version,'instance_id':''}).status_code,409)

    def test_upload_does_not_select_or_join_pool_and_preserves_config(self):
        identity=self.add(instance_id=self.scope)
        before=self.config.read_bytes()
        result=self.client.post(PREFIX+'/'+identity+'/upload',headers=self.headers,data={
            'version':'1','confirmed':'true','cookie':(io.BytesIO(COOKIE),'sample.txt')})
        self.assertEqual(result.status_code,201,result.json)
        self.assertEqual(self.config.read_bytes(),before)
        self.assertEqual(self.detail(identity)['cookie_count'],1)
        self.assertFalse(self.detail(identity)['cookies'][0]['pooled'])

    def test_login_state_records_automatic_checks_and_invalidates_on_cookie_or_exit_change(self):
        identity=self.add(instance_id=self.scope); profile=self.cookie()
        self.assertEqual(self.link(identity,[profile]).status_code,200)
        from webapp.youtube_auth import YouTubeAuth
        from Utils.config import load_config
        config,base=load_config(self.config)
        with patch('webapp.youtube_auth.probe',return_value=('valid','Fixture login')):
            YouTubeAuth(self.store).check({**config,'youtube_cookie':str(self.root/'Cookies/profiles'/f'{profile}.txt')},base)
        self.assertEqual(self.detail(identity)['auth_state'],'valid')
        with self.store.connect() as db:
            row=db.execute(select(youtube_profile_auth)).mappings().one()
            value=json.loads(row['auth']);value['expires_at']=time.time()-1
            db.execute(youtube_profile_auth.update().values(auth=json.dumps(value)))
        self.assertEqual(self.detail(identity)['auth_state'],'expired')
        with self.store.connect() as db:
            db.execute(cookie_profiles.update().values(content=COOKIE.replace(b'fixture',b'new-cookie').decode()))
        self.assertEqual(self.detail(identity)['auth_state'],'unchecked')

    def test_queries_batch_400_accounts_without_files_or_probes_and_paginate(self):
        raw='\n'.join(f'user{i}@example.com----dummy-{i}' for i in range(400))
        preview=self.preview(raw)
        result=self.client.post(PREFIX+'/import',headers=self.headers,json={'token':preview['token'],'text':raw})
        self.assertEqual(result.status_code,200,result.json)
        calls=[]
        def observe(*args): calls.append(args[2])
        event.listen(self.store.db.engine,'before_cursor_execute',observe)
        try:
            with (patch('webapp.cookie_profiles.CookieProfiles.require',side_effect=AssertionError('file read')),
                    patch('webapp.youtube_auth.probe',side_effect=AssertionError('network'))):
                response=self.client.get(PREFIX+'?page=2&size=50')
        finally: event.remove(self.store.db.engine,'before_cursor_execute',observe)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['total'],400)
        self.assertEqual(len(response.json['items']),50)
        self.assertLess(len(calls),15)
        self.assertEqual(self.client.get(PREFIX+'?q=user100').json['total'],1)

    def test_delete_erases_ciphertext_but_preserves_cookie_and_audit(self):
        identity=self.add();profile=self.cookie()
        self.assertEqual(self.link(identity,[profile]).status_code,200)
        result=self.client.delete(PREFIX+'/'+identity,headers=self.headers,json={'version':self.detail(identity)['version']})
        self.assertEqual(result.status_code,200,result.json)
        with self.store.connect() as db:
            self.assertFalse(db.execute(select(accounts)).first())
            self.assertIsNotNone(db.execute(select(cookie_profiles)).first())
            self.assertIn('delete',list(db.execute(select(audit.c.action)).scalars()))

    def test_cookie_pagination_search_and_link_filters(self):
        identity = self.add(instance_id=self.scope)
        with self.store.connect() as db:
            db.execute(cookie_profiles.insert(), [dict(scope=self.scope, id=f'{i:032x}',
                name=f'profile-{i}', content=COOKIE.decode(), created_at=str(i), deleted=0) for i in range(120)])
        self.assertEqual(self.link(identity,[f'{0:032x}']).status_code,200)
        result = self.client.get(PREFIX+'/cookies?page=2&size=50').json
        self.assertEqual(result['total'],120)
        self.assertEqual(len(result['items']),50)
        self.assertEqual(self.client.get(PREFIX+'/cookies?linked=1').json['total'],1)
        self.assertEqual(self.client.get(PREFIX+'/cookies?linked=0').json['total'],119)
        self.assertEqual(self.client.get(PREFIX+'/cookies?q=sample@example.com').json['total'],1)
        self.assertEqual(self.client.get(PREFIX+'/cookies?q=%25').json['total'],0)
        self.assertEqual(self.client.get(PREFIX+'/cookies?auth=unchecked&size=100').json['total'],120)
        self.assertEqual(self.client.get(PREFIX+'?auth=unchecked').json['total'],1)

    def test_late_login_result_cannot_validate_replaced_cookie(self):
        profile = self.cookie()
        from webapp.account_auth import publish_profile_auth
        from Utils.config import load_config
        config,base = load_config(self.config)
        config = {**config, '_cookie_pool_selected':profile}
        result = dict(state='valid', checked_at=time.time(), expires_at=time.time()+300)
        with self.store.connect() as db:
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id==profile).values(content=COOKIE.replace(b'fixture',b'replaced').decode()))
        publish_profile_auth(self.store,config,base,result,'',COOKIE)
        with self.store.connect() as db:
            self.assertFalse(db.execute(select(youtube_profile_auth)).first())

    def test_pool_member_blocks_account_deletion(self):
        identity = self.add(); profile = self.cookie()
        self.assertEqual(self.link(identity,[profile]).status_code,200)
        from webapp.account_library import AccountLibrary
        from Utils.config import load_config
        config,base = load_config(self.config)
        library = AccountLibrary(self.store,base,{**config,'youtube_cookie_pool':[profile]})
        from werkzeug.exceptions import Conflict
        with self.assertRaises(Conflict): library.delete(identity,self.detail(identity)['version'],'admin')
        self.assertEqual(self.detail(identity)['cookie_count'],1)

    def test_viewer_operator_csrf_and_node_rpc_cannot_read_or_modify_secrets(self):
        identity=self.add()
        for role in ('viewer','operator'):
            self.client.post('/api/auth/users',headers=self.headers,json={'username':role,'role':role,'password':'test-account-password'})
            client=self.app.test_client()
            token=re.search(r'name="csrf-token" content="([^"]+)"',client.get('/login').text)[1]
            response=client.post('/api/auth/login',headers={'X-CSRF-Token':token},json={'username':role,'password':'test-account-password'})
            self.assertEqual(response.status_code,200,response.json)
            token=re.search(r'name="csrf-token" content="([^"]+)"',client.get('/').text)[1]
            self.assertEqual(client.get(PREFIX).status_code,200)
            self.assertEqual(client.post(PREFIX+'/'+identity+'/secrets',headers={'X-CSRF-Token':token},json={'field':'password'}).status_code,403)
        self.assertEqual(self.client.post(PREFIX+'/'+identity+'/secrets',json={'field':'password'}).status_code,403)


if __name__ == '__main__': unittest.main()
