"""Job isolation contracts: queue ownership, imports, cancellation and billing."""
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests import test_scale, test_fleet
from webapp.bulk import run_import
from webapp.database import deliveries


class JobTests(unittest.TestCase):
    def setUp(self):
        test_scale.ScaleTests.setUp(self)
        response = self.client.put('/api/config', json={'download_methods':['downmp3']}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
    def create(self, name='List A', urls='npNnbj46_ik', **extra):
        response = self.client.post('/api/jobs', json={
            'customer': 'Client A', 'name': name, 'urls': urls,
            **extra}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['id']

    def control(self, identity, action):
        with patch('app.require_downmp3_environment'):
            response = self.client.post(f'/api/jobs/{identity}/control', json={'action': action,
                **({'confirmation':self.detail(identity)['name']} if action=='delete' else {})}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['job']

    def detail(self, identity):
        response = self.client.get('/api/jobs/' + identity)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def videos(self, identity):
        return self.client.get(f'/api/jobs/{identity}/videos').json['items']

    def import_file(self, identity, content=b'2Gu7q-KUXCk\n'):
        response = self.client.post(f'/api/jobs/{identity}/imports',
            data={'file': (BytesIO(content), 'more.txt')}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        return self.store.get(response.json['id'], private=True)

    def test_different_customers_can_own_same_video_and_snapshot_policy(self):
        a = self.create()
        b = self.create('List B', customer='Client B')
        self.assertNotEqual(a, b)
        self.assertEqual((self.detail(a)['total'], self.detail(b)['total']), (1, 1))
        self.store.activate_download_lists()
        self.assertIsNone(self.store.claim(self.store.owner))  # Legacy global start cannot release jobs.
        self.control(a, 'start')
        task = self.store.claim(self.store.owner)
        self.assertEqual(task['batch_id'], a)
        self.assertEqual(task['payload']['config']['download_methods'], ['downmp3'])
        self.assertIn(a, task['payload']['config']['dir'])
        self.store.archive_add('youtube npNnbj46_ik', job_id=a)
        self.assertFalse(self.store.archive_has('youtube npNnbj46_ik', job_id=b))
        self.assertFalse(self.store.archive_has('youtube npNnbj46_ik'))

    def test_append_deduplicates_canonical_links_and_retry_keeps_one_row(self):
        a = self.create()
        response = self.client.post(f'/api/jobs/{a}/videos', json={
            'urls': 'https://youtu.be/npNnbj46_ik\n2Gu7q-KUXCk'}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        self.assertEqual((response.json['added'], response.json['duplicates']), (1, 1))
        self.control(a, 'start')
        task = self.store.claim(self.store.owner)
        self.store.finish(task['id'], error='unavailable', worker_id=self.store.owner)
        response = self.client.post(f'/api/jobs/{a}/videos', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
        self.assertEqual(response.json['added'], 0)
        self.control(a, 'retry')
        self.assertEqual(self.detail(a)['total'], 2)
        self.assertEqual(self.detail(a)['failed'], 0)

    def test_pause_stop_and_global_pause_have_distinct_scopes(self):
        a, b = self.create(), self.create('List B')
        self.control(a, 'start')
        first = self.store.claim(self.store.owner)
        self.control(b, 'start')
        self.control(a, 'pause')
        self.assertTrue(self.store.heartbeat(first['id'], self.store.owner))
        self.assertEqual(self.store.claim('other-owner')['batch_id'], b)
        self.control(a, 'stop')
        self.assertFalse(self.store.heartbeat(first['id'], self.store.owner))
        self.assertFalse(self.store.finish(first['id'], worker_id=self.store.owner, result={}))
        self.store.set_downloads_paused(True)
        self.control(a, 'start')
        self.assertTrue(self.store.downloads_paused())
        self.assertIsNone(self.store.claim('third-owner'))

    def test_clear_revokes_execution_preserves_ledger_and_other_job(self):
        a, b = self.create(price='2'), self.create('List B', price='3')
        self.control(a, 'start')
        task = self.store.claim(self.store.owner)
        first = self.root / 'delivered.mp4'
        first.write_bytes(b'x' * 100)
        second = self.root / 'second-attempt.mp4'
        second.write_bytes(b'y' * 200)
        self.assertTrue(self.store.statistics.record_file(task['id'], first))
        self.assertFalse(self.store.statistics.record_file(task['id'], second))
        self.store.finish(task['id'], result={}, worker_id=self.store.owner)
        response = self.client.post(f'/api/jobs/{a}/videos/remove', json={
            'clear': True, 'scope': 'all', 'confirmation': 'List A'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.detail(a)['total'], 0)
        self.assertEqual(self.detail(a)['file_bytes_exact'], '100')
        self.assertEqual(self.client.get('/api/statistics').json['summary']['succeeded'], 1)
        self.assertEqual(self.detail(b)['file_bytes_exact'], '0')
        self.assertTrue(first.exists())
        response = self.client.post(f'/api/jobs/{a}/videos', json={'urls': 'npNnbj46_ik'}, headers=self.headers)
        self.assertEqual(response.json['restored'], 1)
        self.assertEqual(self.videos(a)[0]['id'], task['id'])
        self.assertEqual(self.videos(a)[0]['status'], 'succeeded')
        with self.store.connect() as db:
            self.assertEqual(len(db.execute(select(deliveries)).all()), 1)

    def test_multiple_imports_append_to_same_job_and_clear_fences_old_chunks(self):
        a = self.create(urls='')
        first = self.import_file(a, b'npNnbj46_ik\n2Gu7q-KUXCk\n')
        run_import(self.store, first['payload'], lambda _: None)
        second = self.import_file(a, b'npNnbj46_ik\nWMu6ANTg8mk\n')
        result = run_import(self.store, second['payload'], lambda _: None)
        self.assertEqual((result['added'], result['duplicates']), (1, 1))
        self.assertEqual(self.detail(a)['total'], 3)  # Imports never count as videos.
        old = self.import_file(a, b'00000000001\n00000000002\n00000000003\n')
        cleared = []
        def clear_after_commit(_):
            if not cleared:
                cleared.append(True)
                response = self.client.post(f'/api/jobs/{a}/videos/remove', json={
                    'clear': True, 'scope': 'all', 'confirmation': 'List A'}, headers=self.headers)
                self.assertEqual(response.status_code, 200, response.json)
        with patch('webapp.bulk.CHUNK', 2), self.assertRaises(InterruptedError):
            run_import(self.store, old['payload'], clear_after_commit)
        self.assertEqual(self.detail(a)['total'], 0)
        self.assertEqual(self.detail(a)['importing'], 0)
        self.assertFalse(self.store.heartbeat(old['id'], self.store.owner))

    def test_settings_exports_archive_and_deleted_bills_are_independent(self):
        a, b = self.create(price='1'), self.create('List B', price='2')
        response = self.client.put('/api/jobs/' + a, json={'customer': 'Changed', 'name': 'New label',
            'price': '3.5', 'unit': 'GiB', 'note': 'memo', 'edit_version': self.detail(a)['edit_version']}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.detail(b)['customer'], 'Client A')
        self.assertEqual(self.detail(b)['price'], '2')
        self.assertEqual(self.client.get(f'/api/jobs/{a}/export?kind=bill').status_code, 200)
        self.assertEqual(self.client.get(f'/api/jobs/{a}/statistics').status_code, 200)
        self.control(a, 'archive')
        self.assertNotIn(a, [x['id'] for x in self.client.get('/api/jobs').json['items']])
        self.assertIn(a, [x['id'] for x in self.client.get('/api/jobs?lifecycle=archived').json['items']])
        self.control(a, 'unarchive')
        self.control(a, 'delete')
        self.assertEqual(self.client.get('/api/jobs/' + a).status_code, 404)
        self.assertEqual(self.client.get('/api/jobs/summary').json['total'], 1)

    def test_rejects_invalid_scope_links_and_unsafe_clear(self):
        a = self.create()
        invalid = self.client.post('/api/jobs', json={'customer':'A','name':'B','urls':'https://example.com'},headers=self.headers)
        self.assertEqual(invalid.status_code, 400)
        for values in ({'clear':True,'confirmation':'wrong'}, {'clear':True,'confirmation':'List A','scope':'bad'}):
            response=self.client.post(f'/api/jobs/{a}/videos/remove',json=values,headers=self.headers)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.detail(a)['total'], 1)

    def test_creation_retry_is_idempotent_and_test_mode_locks_after_start(self):
        import uuid
        from concurrent.futures import ThreadPoolExecutor
        from webapp.jobs import Jobs
        from Utils.config import load_config
        request_id=uuid.uuid4().hex
        config,base=load_config(self.config)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:Jobs(self.store).create({'customer':'Client A','name':'List A',
                'request_id':request_id,'urls':'npNnbj46_ik'},config,base),range(2)))
        self.assertEqual(results[0]['id'],results[1]['id'])
        a=results[0]['id']
        repeated=self.create(request_id=request_id)
        self.assertEqual(a,repeated)
        self.assertEqual(self.client.get('/api/jobs/summary').json['total'],1)
        self.control(a,'start')
        self.store.claim(self.store.owner)
        self.control(a,'stop')
        response=self.client.put('/api/jobs/'+a,json={'policy':{'storage_mode':'test'},'edit_version':self.detail(a)['edit_version']},headers=self.headers)
        self.assertEqual(response.status_code,400)

    def test_worker_indexes_two_jobs_under_the_shared_media_root(self):
        import yt_dlp
        from web_worker import execute
        a,b=self.create(),self.create('List B')
        self.control(a,'start')
        self.control(b,'start')
        info={'id':'npNnbj46_ik','title':'fixture','ext':'mp4','extractor_key':'Youtube'}
        def process(downloader,metadata,download=True):
            path=Path(downloader.prepare_filename(metadata))
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b'x'*500)
            for hook in downloader.params['post_hooks']:
                hook(str(path))
            downloader.record_download_archive(metadata)
        with patch('webapp.downmp3.resolve',return_value=info), patch('webapp.yt5s.verify_media'), \
                patch('webapp.downloader.require_download_tools'), patch('webapp.environment.require_downmp3_environment'), \
                patch.object(yt_dlp.YoutubeDL,'process_ie_result',process):
            for _ in range(2):
                task=self.store.claim(self.store.owner)
                result=execute('download',task['payload'],store=self.store,task_id=task['id'])
                self.assertFalse(result.get('skipped'))
                self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        self.assertEqual(self.catalog.root_status(self.root/'videos')['count'],2)
        self.assertEqual(self.detail(a)['file_bytes'],500)
        self.assertEqual(self.detail(b)['file_bytes'],500)

    def test_legacy_import_is_adopted_and_cancelled_without_late_rows(self):
        response=self.client.post('/api/downloads/import',data={'customer':'Legacy',
            'file':(BytesIO(b'00000000001\n00000000002\n00000000003\n'),'legacy.txt')},headers=self.headers)
        self.assertEqual(response.status_code,202,response.json)
        task=self.store.get(response.json['id'],private=True)
        identity=task['payload']['import_id']
        self.assertEqual(len(self.detail(identity)['imports']),1)
        cleared=[]
        def clear(_):
            if not cleared:
                cleared.append(True)
                response=self.client.post(f'/api/jobs/{identity}/videos/remove',json={
                    'clear':True,'scope':'all','confirmation':'legacy.txt'},headers=self.headers)
                self.assertEqual(response.status_code,200,response.json)
        with patch('webapp.bulk.CHUNK',2),self.assertRaises(InterruptedError):
            run_import(self.store,task['payload'],clear)
        self.assertEqual(self.detail(identity)['total'],0)
        self.assertEqual(self.detail(identity)['importing'],0)


class JobFleetTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet

    def test_jobs_keep_worker_paths_and_isolate_remote_cancellation(self):
        self.b.fleet.save_policy({'downmp3_quality':'1080'})
        ids=[]
        for name,quality in [('A','720'),('B','1080')]:
            response=self.client.post('/api/jobs',json={'customer':name,'name':name,'urls':'npNnbj46_ik'},headers=self.headers)
            self.assertEqual(response.status_code,201,response.json)
            ids.append(response.json['id'])
        with patch('webapp.fleet_routes.rpc',return_value={'ready':True}):
            for identity in ids:
                response=self.client.post(f'/api/jobs/{identity}/control',json={'action':'start'},headers=self.headers)
                self.assertEqual(response.status_code,200,response.json)
        a=self.a.claim(self.a.owner)
        b=self.b.claim(self.b.owner)
        self.assertEqual(a['batch_id'],ids[0])
        self.assertEqual(b['batch_id'],ids[1])
        self.assertEqual(a['payload']['config']['path'],'videos-alpha')
        self.assertEqual(b['payload']['config']['path'],'videos-beta')
        self.assertEqual(a['payload']['config']['downmp3_quality'],'720')
        self.assertEqual(b['payload']['config']['downmp3_quality'],'1080')
        response=self.client.post(f'/api/jobs/{ids[0]}/control',json={'action':'stop'},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertFalse(self.a.heartbeat(a['id'],self.a.owner))
        self.assertTrue(self.b.heartbeat(b['id'],self.b.owner))
        with patch('webapp.fleet_routes.rpc',return_value={'ready':True}):
            self.client.post(f'/api/jobs/{ids[0]}/control',json={'action':'start'},headers=self.headers)
        retry=self.b.claim(self.b.owner)
        self.assertEqual(retry['id'],a['id'])
        self.assertNotEqual(retry['payload']['config']['dir'],a['payload']['config']['dir'])
        self.assertFalse(self.a.finish(a['id'],result={},worker_id=self.a.owner))
