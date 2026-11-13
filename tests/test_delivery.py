"""Real FFmpeg/HTTP output contracts, recovery and billing isolation."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import itertools
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch, MagicMock
from sqlalchemy import select
from tests import test_scale
from webapp.delivery.contracts import normalize, KINDS
from webapp.database import delivery_artifacts, delivery_requirements, delivery_runs


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        test_scale.ScaleTests.setUp(self)
        tools=Path(__file__).resolve().parents[1]/'.cache/validation/tools/bin'
        self.environment=patch.dict(os.environ,{'PATH':str(tools)+os.pathsep+os.environ['PATH']})
        self.environment.start();self.addCleanup(self.environment.stop)
        if not shutil.which('ffmpeg'):
            self.skipTest('FFmpeg required for real media verification')
        self.media=self.root/'source';self.media.mkdir()
        for name,arguments in [('video.mp4',['-f','lavfi','-i','color=c=blue:s=160x120:r=10','-an','-c:v','libx264']),
                               ('audio.m4a',['-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-vn','-c:a','aac'])]:
            subprocess.run(['ffmpeg','-nostdin','-v','error','-y',*arguments,'-t','0.5',str(self.media/name)],check=True,capture_output=True)
        (self.media/'en.vtt').write_text('WEBVTT\n\n00:00:00.000 --> 00:00:00.400\nHello world.\n',encoding='utf-8')
        self.hits=[]
        hits=self.hits
        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path);super().do_GET()
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(self.media)))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        response=self.client.put('/api/config',json={'download_methods':['yt_dlp'],'youtube_cookie_usage':'never',
            'youtube_sleep_preset':False,'youtube_start_interval':0,'proxy_mode':'direct'},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)

    def info(self,subtitles=True):
        formats=[dict(format_id='a',url=self.url+'/audio.m4a',ext='m4a',acodec='aac',vcodec='none',protocol='http',http_headers={}),
                 dict(format_id='v',url=self.url+'/video.mp4',ext='mp4',vcodec='h264',acodec='none',height=120,width=160,protocol='http',http_headers={})]
        return dict(id='npNnbj46_ik',title='fixture',extractor='youtube',extractor_key='Youtube',webpage_url='https://www.youtube.com/watch?v=npNnbj46_ik',
                    formats=formats,subtitles={'en':[{'url':self.url+'/en.vtt','ext':'vtt'}]} if subtitles else {},automatic_captions={})

    def create(self,kinds,options=None):
        spec=normalize({'outputs':list(kinds),'subtitles':{'languages':['en']},**(options or {})})
        response=self.client.post('/api/jobs',json={'customer':'Delivery client','name':'Outputs','urls':'npNnbj46_ik','delivery':spec},headers=self.headers)
        self.assertEqual(response.status_code,201,response.json)
        identity=response.json['id']
        # The worker tests isolate provider/auth preflight from actual media handling.
        with self.store.connect() as db:
            from webapp.database import download_lists
            db.execute(download_lists.update().where(download_lists.c.id==identity).values(released=1))
        return identity

    def execute(self,info=None):
        from web_worker import execute
        task=self.store.claim(self.store.owner)
        self.assertIsNotNone(task)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=info or self.info()), \
                patch('webapp.delivery.executor.subtitle_slot',side_effect=lambda _:nullcontext()):
            result=execute('download',task['payload'],store=self.store,task_id=task['id'])
        self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        return task,result

    def test_all_fifteen_combinations_and_track_reuse(self):
        for length in range(1,5):
            for kinds in itertools.combinations(KINDS,length):
                with self.subTest(outputs=kinds):
                    self.hits.clear();job=self.create(kinds)
                    task,result=self.execute()
                    self.assertEqual(result['delivery_status'],'complete')
                    self.assertEqual(result['file_count'],len(kinds))
                    detail=self.client.get('/api/jobs/'+job).json
                    self.assertEqual(detail['file_count'],len(kinds))
                    self.assertEqual(detail['delivery']['states'].get('complete'),1)
                    for endpoint,expected in [('/video.mp4',any(k in kinds for k in ('video_only','av'))),('/audio.m4a',any(k in kinds for k in ('audio_only','av'))),('/en.vtt','subtitle' in kinds)]:
                        self.assertEqual(self.hits.count(endpoint),int(expected),(kinds,self.hits))
                    files=self.client.get('/api/jobs/'+job+'/artifacts').json['items']
                    self.assertEqual({r['kind'] for r in files},set(kinds))
                    for row in files:
                        response=self.client.get(row['content_url']);self.assertEqual(response.status_code,200);response.close()

    def test_missing_subtitle_keeps_media_and_accepts_explicit_waiver(self):
        job=self.create(['subtitle','av']);task,result=self.execute(self.info(False))
        self.assertEqual(result['delivery_status'],'review');self.assertEqual(result['file_count'],1)
        rows=self.client.get('/api/jobs/'+job+'/videos').json['items'][0]['delivery']['requirements']
        missing=next(r for r in rows if r['status']=='missing')
        response=self.client.post(f'/api/jobs/{job}/requirements/{task["id"]}/{missing["key"]}/waive',json={'reason':'Customer accepted no captions'},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json);self.assertEqual(response.json['status'],'accepted')

    def test_repair_reuses_files_without_extra_network_or_billing(self):
        job=self.create(KINDS);task,_=self.execute();before=self.client.get('/api/jobs/'+job).json
        self.hits.clear()
        response=self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']]},headers=self.headers)
        self.assertEqual(response.status_code,202,response.json)
        self.execute();after=self.client.get('/api/jobs/'+job).json
        self.assertEqual(after['file_bytes'],before['file_bytes']);self.assertEqual(after['file_count'],4);self.assertEqual(self.hits,[])

    def test_contract_pin_and_latest_repair_only_fetch_added_output(self):
        job=self.create(['audio_only']);task,_=self.execute();old=self.client.get('/api/jobs/'+job).json
        response=self.client.put('/api/jobs/'+job,json={'edit_version':old['edit_version'],'delivery':{'outputs':['audio_only','video_only']}},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.hits.clear();self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']],'use_latest':True},headers=self.headers)
        self.execute();self.assertNotIn('/audio.m4a',self.hits);self.assertEqual(self.hits.count('/video.mp4'),1)

    def test_test_mode_has_no_formal_files_or_bill(self):
        self.client.put('/api/config',json={'storage_mode':'test'},headers=self.headers)
        job=self.create(KINDS);_,result=self.execute()
        self.assertTrue(result['files_deleted']);self.assertEqual(result['file_count'],4)
        detail=self.client.get('/api/jobs/'+job).json
        self.assertEqual(detail['file_count'],0);self.assertEqual(detail['test_file_count'],4)
        self.assertEqual(self.client.get('/api/jobs/'+job+'/artifacts').json['items'],[])

    def test_language_policy_and_delivery_filter_without_task_filter(self):
        job=self.create(['subtitle'],{'subtitles':{'languages':['en','fr'],'language_policy':'all_selected'}})
        task,result=self.execute()
        self.assertEqual(result['delivery_status'],'review')
        rows=self.client.get('/api/jobs/'+job+'/videos?delivery_status=review').json['items']
        self.assertEqual(len(rows),1)
        self.assertEqual({(r['language'],r['status']) for r in rows[0]['delivery']['requirements']},{('en','done'),('fr','missing')})
        self.assertEqual(self.client.get('/api/jobs/'+job+'/videos?delivery_status=complete').json['items'],[])
        job=self.create(['subtitle'],{'subtitles':{'languages':['en','fr'],'language_policy':'available_selected','format':'vtt'}})
        _,result=self.execute();self.assertEqual(result['delivery_status'],'complete')
        self.assertEqual(self.hits.count('/video.mp4'),0)

    def test_explicit_mp3_mp4_and_mkv_are_verified(self):
        from webapp.delivery.verify import inspect
        self.create(['audio_only','video_only','av'],{'audio':{'format':'mp3'},'video':{'container':'mp4'},'av':{'container':'mp4'}})
        _,result=self.execute();self.assertEqual(result['file_count'],3)
        with self.store.connect() as db:
            rows=list(db.execute(select(delivery_artifacts)).mappings())
        self.assertEqual(next(r['path'] for r in rows if r['kind']=='audio_only')[-4:],'.mp3')
        self.assertEqual(inspect(next(r['path'] for r in rows if r['kind']=='audio_only'))[1]['streams'][0]['codec_name'],'mp3')
        job=self.create(['av'],{'av':{'container':'mkv'}});self.execute()
        self.assertTrue(self.client.get('/api/jobs/'+job+'/artifacts').json['items'][0]['content_url'])
        with self.store.connect() as db:
            self.assertTrue(db.execute(select(delivery_artifacts.c.path).where(delivery_artifacts.c.job_id==job)).scalar_one().endswith('.mkv'))

    def retry_failed(self,job,task):
        self.store.finish(task['id'],error='interrupted',worker_id=self.store.owner)
        response=self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']]},headers=self.headers)
        self.assertEqual(response.status_code,202,response.json)

    def test_upload_failure_reuses_verified_file_without_source_health_penalty(self):
        from Utils.oss_storage import OSSUploadError
        from web_worker import execute
        response=self.client.put('/api/config',json={'storage_mode':'oss','oss_endpoint':'https://oss-cn-hangzhou.aliyuncs.com',
            'oss_bucket':'fixture-bucket','oss_access_key_id':'fixture-id','oss_access_key_secret':'fixture-secret'},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        job=self.create(['audio_only']);task=self.store.claim(self.store.owner)
        cloud=MagicMock();cloud.upload_files.side_effect=OSSUploadError('fixture upload failure')
        with patch('Utils.oss_storage.OSSStorage',return_value=cloud),patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()),patch('webapp.channel_scheduler.ChannelScheduler.outcome') as outcome:
            with self.assertRaises(OSSUploadError):execute('download',task['payload'],store=self.store,task_id=task['id'])
            outcome.assert_not_called()
        self.assertEqual(self.client.get('/api/jobs/'+job).json['file_count'],0)
        self.assertEqual(self.client.get('/api/jobs/'+job+'/artifacts').json['items'],[])
        self.retry_failed(job,task);self.hits.clear()
        def uploaded(paths,root,*args):
            Path(paths[0]).resolve().relative_to(Path(root).resolve())
            return [{'key':'fixture/'+Path(paths[0]).name}]
        cloud.upload_files.side_effect=uploaded
        with patch('Utils.oss_storage.OSSStorage',return_value=cloud),patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info') as extract:
            self.execute();extract.assert_not_called()
        self.assertEqual(self.hits,[]);self.assertEqual(self.client.get('/api/jobs/'+job).json['file_count'],1)

    def test_progressive_source_is_shared_between_all_media_outputs(self):
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(self.media/'video.mp4'),'-i',str(self.media/'audio.m4a'),'-c','copy',str(self.media/'combined.mp4')],check=True,capture_output=True)
        info=self.info(False);info['formats']=[dict(format_id='combined',url=self.url+'/combined.mp4',ext='mp4',vcodec='h264',acodec='aac',height=120,width=160,protocol='http',http_headers={})]
        self.create(['video_only','audio_only','av']);_,result=self.execute(info)
        self.assertEqual(result['file_count'],3);self.assertEqual(self.hits,['/combined.mp4'])

    def test_subtitle_lane_busy_preserves_delivered_media(self):
        from web_worker import execute
        from webapp.throughput import PipelineBusy
        job=self.create(['av','subtitle']);task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()),patch('webapp.delivery.executor.subtitle_slot',side_effect=PipelineBusy('busy')):
            with self.assertRaises(PipelineBusy):execute('download',task['payload'],store=self.store,task_id=task['id'])
        self.assertEqual(self.client.get('/api/jobs/'+job).json['file_count'],1)
        self.retry_failed(job,task);self.hits.clear();self.execute()
        self.assertEqual(self.hits,['/en.vtt']);self.assertEqual(self.client.get('/api/jobs/'+job).json['file_count'],2)

    def test_deleted_or_invalid_media_is_repaired_without_rebilling(self):
        job=self.create(['audio_only']);task,_=self.execute()
        with self.store.connect() as db:path=db.execute(select(delivery_artifacts.c.path).where(delivery_artifacts.c.job_id==job)).scalar_one()
        Path(path).write_bytes(b'corrupt')
        self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']]},headers=self.headers)
        self.hits.clear();self.execute();self.assertEqual(self.hits,['/audio.m4a'])
        self.assertEqual(self.client.get('/api/jobs/'+job).json['file_count'],1)

    def test_test_to_formal_mode_never_reuses_deleted_test_output(self):
        self.client.put('/api/config',json={'storage_mode':'test'},headers=self.headers)
        job=self.create(['audio_only']);task,_=self.execute()
        self.client.put('/api/config',json={'storage_mode':'local'},headers=self.headers)
        self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']]},headers=self.headers)
        self.hits.clear();self.execute()
        self.assertEqual(self.hits,['/audio.m4a'])
        detail=self.client.get('/api/jobs/'+job).json;self.assertEqual((detail['file_count'],detail['test_file_count']),(1,1))

    def test_lost_lease_cannot_publish_or_bill(self):
        from webapp.delivery import artifacts
        from webapp.database import tasks
        from webapp.downloads import DownloadLeaseLost
        original=artifacts.pending
        def lose(store,task_id,*args):
            with store.connect() as db:db.execute(tasks.update().where(tasks.c.id==task_id).values(lease_until=0))
            return original(store,task_id,*args)
        job=self.create(['audio_only'])
        with patch.object(artifacts,'pending',side_effect=lose),self.assertRaises(DownloadLeaseLost):self.execute()
        self.assertEqual(self.client.get('/api/jobs/'+job).json['file_count'],0)

    def test_typed_crash_receipt_recovers_complete_but_not_partial_delivery(self):
        from web_worker import execute
        from webapp.delivery.executor import recover_test_outputs
        from webapp.delivery.verify import LocalDeliveryError
        self.client.put('/api/config',json={'storage_mode':'test'},headers=self.headers)
        for kinds,complete in [(['audio_only'],True),(['audio_only','av'],False)]:
            job=self.create(kinds);task=self.store.claim(self.store.owner)
            with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()),patch.object(self.store.statistics,'record_file',side_effect=LocalDeliveryError('simulated interruption')):
                with self.assertRaises(LocalDeliveryError):execute('download',task['payload'],store=self.store,task_id=task['id'])
            receipt=next((self.store.directory/'test-downloads'/task['id']).glob('*/.completed-files.json'))
            result=recover_test_outputs(self.store,task['id'],json.loads(receipt.read_text()),receipt.parent)
            self.assertEqual(bool(result),complete)
            self.store.finish(task['id'],result=result or {},worker_id=self.store.owner)

    def test_defaults_save_and_stale_contract_edit(self):
        response=self.client.put('/api/delivery/defaults',json={'outputs':['subtitle']},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.client.get('/api/delivery/defaults').json['spec']['outputs'],['subtitle'])
        job=self.create(['av']);old=self.client.get('/api/jobs/'+job).json['edit_version']
        self.assertEqual(self.client.put('/api/jobs/'+job,json={'edit_version':old,'delivery':{'outputs':['subtitle']}},headers=self.headers).status_code,200)
        self.assertEqual(self.client.put('/api/jobs/'+job,json={'edit_version':old,'delivery':{'outputs':['av']}},headers=self.headers).status_code,409)

    def test_source_failures_preserve_account_and_http_classification(self):
        import yt_dlp
        from web_worker import execute
        from webapp.download_policy import failure_evidence
        for message,kind in [('HTTP Error 403: Forbidden','access_denied'),('HTTP Error 429: Too Many Requests','request_rate_limit'),('Your account has been rate-limited','account_rate_limit')]:
            job=self.create(['audio_only']);task=self.store.claim(self.store.owner)
            with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',side_effect=yt_dlp.utils.DownloadError(message)),patch('webapp.channel_scheduler.ChannelScheduler.outcome'):
                with self.assertRaises(RuntimeError) as caught:execute('download',task['payload'],store=self.store,task_id=task['id'])
            self.assertEqual(failure_evidence(caught.exception).kind,kind)
            self.store.finish(task['id'],error=message,worker_id=self.store.owner)

    def test_started_legacy_item_requires_explicit_latest_adoption(self):
        response=self.client.post('/api/jobs',json={'customer':'Legacy','name':'Legacy','urls':'npNnbj46_ik'},headers=self.headers)
        job=response.json['id']
        from webapp.database import download_lists
        with self.store.connect() as db:db.execute(download_lists.update().where(download_lists.c.id==job).values(released=1))
        task=self.store.claim(self.store.owner)
        self.store.finish(task['id'],error='legacy failure',worker_id=self.store.owner)
        version=self.client.get('/api/jobs/'+job).json['edit_version']
        self.client.put('/api/jobs/'+job,json={'edit_version':version,'delivery':{'outputs':['audio_only']}},headers=self.headers)
        from webapp.delivery.artifacts import pin
        with self.store.connect() as db:self.assertIsNone(pin(self.store,db,task['id'],job))
        response=self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']],'use_latest':True},headers=self.headers)
        self.assertEqual(response.status_code,202,response.json)
        _,result=self.execute();self.assertEqual(result['delivery_status'],'complete')

    def test_rejected_cookie_warning_stops_before_anonymous_fallback(self):
        from types import SimpleNamespace
        from webapp.delivery.executor import reporting_downloader
        from webapp.downloads import DownloadFailure
        from webapp.youtube_auth import YouTubeAuthRequired
        session=SimpleNamespace(failure=DownloadFailure(),progress=SimpleNamespace(state={}),config={'_cookie_pool_selected':'fixture'},store=self.store)
        with patch('webapp.youtube_auth.YouTubeAuth.reject',return_value={'state':'invalid','message':'rejected'}) as reject:
            with reporting_downloader(session)({'quiet':True}) as downloader:
                with self.assertRaises(YouTubeAuthRequired):downloader.report_warning('The provided YouTube account cookies are no longer valid')
            self.assertEqual(reject.call_args.kwargs['profile_id'],'fixture')

    def test_oss_preview_range_and_head(self):
        job=self.create(['audio_only']);self.execute()
        with self.store.connect() as db:
            row=db.execute(select(delivery_artifacts).where(delivery_artifacts.c.job_id==job)).mappings().one()
            db.execute(delivery_artifacts.update().where(delivery_artifacts.c.id==row['id']).values(storage='oss',object_key='fixture/audio'))
        cloud=MagicMock();cloud.bucket.get_object.return_value.read.side_effect=[b'1234',b'']
        with patch('Utils.oss_storage.OSSStorage',return_value=cloud):
            response=self.client.get('/api/delivery/files/'+row['id'],headers={'Range':'bytes=0-3'})
            self.assertEqual(response.status_code,206);self.assertEqual(response.data,b'1234');response.close()
            cloud.bucket.get_object.assert_called_once_with('fixture/audio',byte_range=(0,3))
            response=self.client.head('/api/delivery/files/'+row['id']);self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['Content-Length'],str(row['size']))
            self.assertEqual(self.client.get('/api/delivery/files/'+row['id'],headers={'Range':'bytes=999999-'}).status_code,416)


class ContractValidationTests(unittest.TestCase):
    def test_invalid_or_empty_requirements_rejected(self):
        for invalid in ({'outputs':[]},{'outputs':['other']},{'outputs':[{}]},{'subtitles':{'languages':['../en']}},{'video':{'max_height':True}}):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):normalize(invalid)


class DeliveryPermissionsTests(unittest.TestCase):
    from tests import test_console_auth as auth_tests
    setUp=auth_tests.ConsoleAuthTests.setUp
    token=auth_tests.ConsoleAuthTests.token
    setup_admin=auth_tests.ConsoleAuthTests.setup_admin
    login=auth_tests.ConsoleAuthTests.login

    def test_roles_csrf_and_node_protocol_boundary(self):
        self.setup_admin()
        for name,role in [('reader','viewer'),('operator','operator')]:
            self.client.post('/api/auth/users',json={'username':name,'role':role,'password':'secure-test-password'},headers=self.headers)
        reader,rh=self.login('reader');operator,oh=self.login('operator')
        self.assertEqual(reader.get('/api/delivery/artifacts').status_code,200)
        self.assertEqual(reader.post('/api/jobs/unknown/redeliver',json={'ids':['x']},headers=rh).status_code,403)
        self.assertEqual(operator.put('/api/delivery/defaults',json={'outputs':['av']},headers=oh).status_code,403)
        self.assertEqual(self.client.put('/api/delivery/defaults',json={'outputs':['av']}).status_code,403)
        self.assertEqual(self.client.put('/api/delivery/defaults',json={'outputs':['av']},headers=self.headers).status_code,200)
        from webapp.console_auth import rpc_allowed
        for path,method,allowed in [('/api/delivery/files/'+'a'*64,'GET',True),('/api/delivery/files/'+'a'*64,'HEAD',True),('/api/delivery/files/'+'a'*64,'POST',False),('/api/delivery/artifacts','GET',False),('/api/jobs/any/redeliver','POST',False)]:
            with self.app.test_request_context(path,method=method):self.assertEqual(rpc_allowed(),allowed)


class DeliveryFleetTests(unittest.TestCase):
    from tests import test_fleet as fleet_tests
    setUp=fleet_tests.FleetTests.setUp
    setup_fleet=fleet_tests.FleetTests.setup_fleet

    def test_older_registered_executor_blocks_new_contract(self):
        from webapp.database import instances
        with self.manager.connect() as db:
            config=json.loads(db.execute(select(instances.c.config).where(instances.c.id=='beta')).scalar_one())
            config.pop('delivery_schema_version',None)
            db.execute(instances.update().where(instances.c.id=='beta').values(config=json.dumps(config),heartbeat=0))
        response=self.client.post('/api/jobs',json={'customer':'A','name':'B','delivery':{'outputs':['subtitle']}},headers=self.headers)
        self.assertEqual(response.status_code,409,response.json)
        self.assertIn('beta',response.json['error'])
        config['delivery_schema_version']=1
        with self.manager.connect() as db:db.execute(instances.update().where(instances.c.id=='beta').values(config=json.dumps(config)))
        response=self.client.post('/api/jobs',json={'customer':'A','name':'B','delivery':{'outputs':['subtitle']}},headers=self.headers)
        self.assertEqual(response.status_code,201,response.json)
