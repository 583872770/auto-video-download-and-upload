"""Real FFmpeg/HTTP output contracts, recovery and billing isolation."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import itertools
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
from webapp.delivery.contracts import normalize, KINDS, variant
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
        from PIL import Image
        Image.new('RGB', (320, 180), 'blue').save(self.media/'cover.webp')
        self.storage={}
        self.hits=[]
        hits=self.hits
        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path == '/limited.vtt':
                    self.send_response(429);self.send_header('Retry-After','2400');self.end_headers()
                    return
                super().do_GET()
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(self.media)))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        from tests.account_cookie_fixture import AccountCookieProfiles
        self.profile = AccountCookieProfiles(self.root, self.store).add('Delivery fixture',
            b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tdelivery_fixture\n')['id']
        # Delivery fixtures exercise actual account ownership and permits. Time
        # budgets and remote login are isolated here; cookie_pool tests cover them.
        for target in ('webapp.cookie_pool.account_interval', 'webapp.cookie_policy.account_interval'):
            mocked = patch(target, return_value=0);mocked.start();self.addCleanup(mocked.stop)
        mocked = patch('webapp.cookie_pool.random.uniform', return_value=0)
        mocked.start();self.addCleanup(mocked.stop)
        mocked = patch('webapp.youtube_auth.YouTubeAuth.require', return_value={'state':'valid'})
        mocked.start();self.addCleanup(mocked.stop)
        response=self.client.put('/api/config',json={'youtube_cookie_pool':[self.profile],
            'youtube_cookie_pool_start_interval':0,'youtube_hourly_start_limit':0,'youtube_daily_start_limit':0,
            'youtube_sleep_preset':False,'youtube_start_interval':0,'proxy_mode':'direct'},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)

    def info(self,subtitles=True):
        formats=[dict(format_id='a',url=self.url+'/audio.m4a',ext='m4a',acodec='aac',vcodec='none',protocol='http',http_headers={}),
                 dict(format_id='v',url=self.url+'/video.mp4',ext='mp4',vcodec='h264',acodec='none',height=120,width=160,protocol='http',http_headers={})]
        return dict(id='npNnbj46_ik',title='fixture',extractor='youtube',extractor_key='Youtube',webpage_url='https://www.youtube.com/watch?v=npNnbj46_ik',
                    formats=formats,thumbnails=[{'url':self.url+'/cover.webp'}],
                    subtitles={'en':[{'url':self.url+'/en.vtt','ext':'vtt'}]} if subtitles else {},automatic_captions={})

    def create(self,kinds,options=None):
        spec=normalize({'outputs':list(kinds),'subtitles':{'languages':['en']},'storage':self.storage,**(options or {})})
        response=self.client.post('/api/jobs',json={'customer':'Delivery client','name':'Outputs','urls':'npNnbj46_ik','delivery':spec},headers=self.headers)
        self.assertEqual(response.status_code,201,response.json)
        identity=response.json['id']
        # The worker tests isolate provider/auth preflight from actual media handling.
        with self.store.connect() as db:
            from webapp.database import download_lists
            db.execute(download_lists.update().where(download_lists.c.id==identity).values(released=1))
        return identity

    def execute(self,info=None,report=None):
        task=self.store.claim(self.store.owner, processing=True) or self.store.claim(self.store.owner)
        self.assertIsNotNone(task)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=info or self.info()):
            result=self.run_task(task, report=report)
            if result.get('network_pending'):
                self.assertTrue(self.store.finish(task['id'],result=result,worker_id=self.store.owner))
                self.cleanup_result(result)
                task=self.store.claim(self.store.owner)
                self.assertIsNotNone(task)
                result=self.run_task(task, report=report)
        self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        self.cleanup_result(result)
        if result.get('_processing_workspace') and result.get('test_mode'):
            self.assertFalse(Path(result['_processing_workspace']['workspace']).parent.exists())
        return task,result

    def cleanup_result(self, result):
        if result.get('_processing_workspace'):
            from webapp.delivery.processing import cleanup_completed
            deleted = cleanup_completed(self.store, result['_processing_workspace'])
            if result.get('test_mode'):
                result['files_deleted'] = deleted

    def run_task(self, task, report=None):
        from web_worker import execute
        result=execute('download',task['payload'],report_progress=report,store=self.store,task_id=task['id'])
        if result.get('processing_pending'):
            self.assertTrue(self.store.finish(task['id'],result=result,worker_id=self.store.owner))
            local=self.store.claim(self.store.owner, processing=True)
            self.assertIsNotNone(local)
            self.assertEqual(local['id'], task['id'])
            result=execute('download',local['payload'],report_progress=report,store=self.store,task_id=task['id'],worker_slot=-2)
        return result

    def test_subtitle_http_429_uses_shared_transfer_recovery_and_honors_retry_after(self):
        from web_worker import execute
        from webapp.request_scheduler import RequestScheduler
        from webapp.download_policy import failure_evidence
        self.create(['subtitle'])
        task=self.store.claim(self.store.owner)
        info=self.info();info['subtitles']['en'][0]['url']=self.url+'/limited.vtt'
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=info):
            with self.assertRaises(RuntimeError) as caught:
                self.run_task(task)
        evidence=failure_evidence(caught.exception)
        self.assertEqual((evidence.phase,evidence.http_status),('transfer',429))
        self.assertGreaterEqual(failure_evidence(caught.exception).retry_after,2390)
        self.assertEqual(self.hits,['/limited.vtt'])
        from webapp.cookie_pool import CookiePool
        pool=CookiePool(self.store,self.store.runtime(),self.root,task['id'])
        states,_,stamp=pool.router.snapshot([pool.accounts[0][2],'source:youtube'])
        self.assertGreaterEqual(states[pool.accounts[0][2]]['cooldown_until'],stamp+2390)
        self.assertFalse(states.get('source:youtube',{}).get('cooldown_until'))

    def test_account_cooldown_blocks_subtitle_metadata_requests(self):
        from web_worker import execute
        from webapp.request_scheduler import RequestScheduler
        from webapp.throughput import PipelineBusy
        self.create(['subtitle'])
        task=self.store.claim(self.store.owner)
        from webapp.cookie_pool import CookiePool
        pool=CookiePool(self.store,self.store.runtime(),self.root,task['id'])
        with pool.router.transaction() as (db,stamp):
            pool.router._state(db,pool.accounts[0][2])
            pool.router._save(db,pool.accounts[0][2],{'cooldown_until':stamp+3600})
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info') as extract:
            with self.assertRaises(PipelineBusy):
                self.run_task(task)
        extract.assert_not_called()
        self.assertEqual(self.hits,[])

    def drain_role(self, role):
        from web_worker import execute
        from webapp.worker.queue import drain_queue
        # Finite fixture drains skip the live process's idle reuse grace period.
        with patch.dict(os.environ, {'VIDEO_WORKER_PROCESS':'true'}), \
             patch('webapp.worker.idle.PROCESSING_IDLE_SECONDS', 0):
            drain_queue(self.store.directory, slot=-2 if role == 'processing' else 0, role=role,
                        execute_task=execute, store_factory=lambda *args: self.store)

    def test_failed_caption_delivers_acquired_tracks_and_preserves_terminal_failure(self):
        from webapp.database import delivery_work
        from webapp.disk_budget import DiskBudget
        job=self.create(['video_only','audio_only','subtitle'])
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()), \
             patch('webapp.delivery.executor.ParallelYoutubeDL.process_ie_result',return_value=None):
            self.drain_role('download')
        task=self.client.get('/api/jobs/'+job+'/videos').json['items'][0]
        pending=self.store.get(task['id'])
        self.assertEqual((pending['status'],pending['progress']['stage']),('queued','waiting_processing'))
        self.assertEqual(pending['error'],'字幕请求未生成可用文件。')
        with self.store.connect() as db:
            work=dict(db.execute(select(delivery_work)).mappings().one())
        self.assertEqual((work['attempts'],work['source_attempts']),(0,1))
        self.assertFalse(json.loads(work['plan'])['source_failure']['retryable'])
        self.hits.clear()
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',side_effect=AssertionError('source requested')), \
             patch('webapp.youtube_auth.YouTubeAuth.require',side_effect=AssertionError('account acquired')):
            self.drain_role('processing')
        done=self.store.get(task['id'])
        self.assertEqual((done['status'],done['attempts']),('failed',1))
        self.assertEqual(done['error'],'字幕请求未生成可用文件。')
        self.assertEqual(done['result']['file_count'],2)
        self.assertNotEqual(done['result']['delivery_status'],'complete')
        self.assertNotIn('source_failed',done['result'])
        self.assertEqual(self.hits,[])
        issue=self.client.get('/api/notifications').json['items'][0]['detail']
        self.assertEqual(set(issue['completed']),{'无声视频','纯音频'})
        self.assertEqual(issue['missing'],['字幕 · en'])
        self.assertEqual(issue['phase'],'下载传输')
        with self.store.connect() as db:
            outputs=list(db.execute(select(delivery_artifacts)).mappings())
            self.assertEqual({r['kind'] for r in outputs},{'video_only','audio_only'})
            self.assertTrue(all(r['available'] and Path(r['path']).is_file() for r in outputs))
            self.assertFalse(db.execute(select(delivery_work)).first())
        self.assertFalse(Path(work['workspace']).exists())
        with DiskBudget(self.store.directory,self.store.runtime()).connect(write=False) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
        self.assertIsNone(self.store.claim(self.store.owner))

    def test_caption_rate_limit_delivers_tracks_then_retries_only_missing_caption(self):
        import time
        from webapp.database import tasks, delivery_work
        job=self.create(['video_only','audio_only','subtitle'])
        info=self.info();info['subtitles']['en'][0]['url']=self.url+'/limited.vtt'
        before=time.time()
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=info):
            self.drain_role('download')
        task=self.client.get('/api/jobs/'+job+'/videos').json['items'][0]
        self.drain_role('processing')
        pending=self.store.get(task['id'])
        self.assertEqual((pending['status'],pending['attempts']),('queued',1))
        self.assertGreaterEqual(pending['available_at'],before+2390)
        self.assertEqual(pending['result']['file_count'],2)
        self.assertEqual(pending['progress']['last_failure']['http_status'],429)
        self.assertIsNone(self.store.claim(self.store.owner))
        with self.store.connect() as db:
            self.assertFalse(db.execute(select(delivery_work)).first())
            db.execute(tasks.update().where(tasks.c.id==task['id']).values(available_at=0))
        # Elapse the affected account's cooldown as well as the task deadline.
        from webapp.cookie_pool import CookiePool
        pool=CookiePool(self.store,self.store.runtime(),self.root,task['id'])
        with pool.router.transaction() as (db,stamp):
            for _,_,scope in pool.accounts:
                state=pool.router._state(db,scope)
                state.update(cooldown_until=0,recover_until=0)
                pool.router._save(db,scope,state)
        self.hits.clear()
        _,result=self.execute()
        self.assertEqual(self.hits,['/en.vtt'])
        self.assertEqual((result['delivery_status'],result['file_count']),('complete',3))
        self.assertEqual(self.store.get(task['id'])['status'],'succeeded')

    def test_partial_delivery_keeps_source_retry_limit_across_local_retry(self):
        from webapp.database import tasks, delivery_work
        from webapp.delivery.verify import LocalDeliveryError
        job=self.create(['video_only','audio_only','subtitle'])
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.batch_id==job).values(attempts=2,max_attempts=3))
        info=self.info();info['subtitles']['en'][0]['url']=self.url+'/limited.vtt'
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=info):
            self.drain_role('download')
        task=self.client.get('/api/jobs/'+job+'/videos').json['items'][0]
        with patch('webapp.delivery.executor.publish',side_effect=LocalDeliveryError('temporary local failure')):
            self.drain_role('processing')
        with self.store.connect() as db:
            work=dict(db.execute(select(delivery_work)).mappings().one())
            self.assertEqual((work['attempts'],work['source_attempts']),(1,3))
            db.execute(tasks.update().where(tasks.c.id==task['id']).values(available_at=0))
        self.drain_role('processing')
        done=self.store.get(task['id'])
        self.assertEqual((done['status'],done['attempts']),('failed',3))
        self.assertIn('429',done['error'])
        self.assertEqual(done['result']['file_count'],2)
        self.assertEqual(done['progress']['last_failure']['http_status'],429)
        self.assertIsNone(self.store.claim(self.store.owner))

    def test_explicit_redelivery_clears_deferred_failure_and_preserves_acquired_files(self):
        from webapp.database import delivery_work
        job=self.create(['video_only','audio_only','subtitle'])
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()), \
             patch('webapp.delivery.executor.ParallelYoutubeDL.process_ie_result',return_value=None):
            self.drain_role('download')
        task=self.client.get('/api/jobs/'+job+'/videos').json['items'][0]
        response=self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']]},headers=self.headers)
        self.assertEqual(response.status_code,202,response.json)
        with self.store.connect() as db:
            work=db.execute(select(delivery_work)).mappings().one()
        self.assertEqual((work['source_attempts'],work['attempts']),(0,0))
        self.assertNotIn('source_failure',json.loads(work['plan']))
        self.hits.clear()
        _,result=self.execute()
        self.assertEqual(self.hits,['/en.vtt'])
        self.assertEqual((result['delivery_status'],result['file_count']),('complete',3))
        self.assertEqual(self.store.get(task['id'])['status'],'succeeded')

    def test_failed_caption_in_test_mode_records_tracks_and_releases_temporary_space(self):
        from webapp.database import delivery_work
        from webapp.disk_budget import DiskBudget
        self.storage={'storage_mode':'test'}
        job=self.create(['video_only','audio_only','subtitle'])
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()), \
             patch('webapp.delivery.executor.ParallelYoutubeDL.process_ie_result',return_value=None):
            self.drain_role('download')
        self.drain_role('processing')
        task=self.client.get('/api/jobs/'+job+'/videos').json['items'][0]
        done=self.store.get(task['id'])
        self.assertEqual(done['status'],'failed')
        self.assertEqual(done['result']['file_count'],2)
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_count'],2)
        self.assertFalse(any((self.store.directory/'test-downloads').rglob('*.mp4')))
        with self.store.connect() as db:
            self.assertFalse(db.execute(select(delivery_work)).first())
        with DiskBudget(self.store.directory,self.store.runtime()).connect(write=False) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_sweep_retires_terminal_test_plan_only_after_planned_outputs_are_recorded(self):
        from webapp.database import delivery_work
        from webapp.disk_budget import DiskBudget
        self.storage={'storage_mode':'test'}
        job=self.create(['audio_only'])
        task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            result=self.run_task(task)
        work=result['_processing_workspace']
        # All outputs are durable, but the final local attempt fails before it
        # removes the plan. Sweeping may retire it without losing statistics.
        self.store.finish(task['id'],error='local settlement interrupted',worker_id=self.store.owner)
        before=self.client.get('/api/jobs/'+job).json['test_file_bytes']
        budget=DiskBudget(self.store.directory,self.store.runtime())
        cleaned=budget.cleanup(self.store,grace=0)
        self.assertFalse(Path(work['workspace']).exists())
        self.assertEqual(cleaned['last_cleanup']['removed'],1)
        self.assertEqual(self.store.get(task['id'])['status'],'failed')
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_bytes'],before)
        with self.store.connect() as db:
            self.assertFalse(db.execute(select(delivery_work)).first())
        self.assertEqual(budget.cleanup(self.store,grace=0)['last_cleanup']['removed'],0)

    def test_sweep_preserves_failed_test_plan_with_unprocessed_media(self):
        from web_worker import execute
        from webapp.database import delivery_work, tasks
        from webapp.disk_budget import DiskBudget
        self.storage={'storage_mode':'test'}
        job=self.create(['metadata','audio_only'])
        task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            result=execute('download',task['payload'],store=self.store,task_id=task['id'])
        self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        local=self.store.claim(self.store.owner,processing=True)
        self.store.finish(local['id'],error='conversion failed',worker_id=self.store.owner)
        with self.store.connect() as db:
            work=dict(db.execute(select(delivery_work)).mappings().one())
            db.execute(tasks.update().where(tasks.c.id==task['id']).values(finished_at='2020-01-01T00:00:00+00:00'))
        budget=DiskBudget(self.store.directory,self.store.runtime())
        cleaned=budget.cleanup(self.store,grace=0)
        self.assertTrue(Path(work['workspace']).exists())
        self.assertEqual(cleaned['last_cleanup']['pending_results'],1)
        self.assertEqual(cleaned['diagnostic']['retention']['pending_processing_count'],1)
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_count'],1)
        with self.store.connect() as db:
            self.assertTrue(db.execute(select(delivery_work)).first())

    def test_sweep_preserves_queued_local_retry_even_with_all_outputs_recorded(self):
        from webapp.database import delivery_work
        from webapp.disk_budget import DiskBudget
        self.storage={'storage_mode':'test'}
        self.create(['audio_only'])
        task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            result=self.run_task(task)
        self.store.release(task['id'],self.store.owner,'retry settlement',delay=0)
        budget=DiskBudget(self.store.directory,self.store.runtime())
        cleaned=budget.cleanup(self.store,grace=0)
        self.assertTrue(Path(result['_processing_workspace']['workspace']).exists())
        self.assertEqual(cleaned['last_cleanup']['pending_results'],1)
        with self.store.connect() as db:
            self.assertTrue(db.execute(select(delivery_work)).first())

    def test_multiple_subtitle_languages_share_one_task_start_and_keep_metadata(self):
        from web_worker import execute
        from webapp.request_scheduler import RequestScheduler
        job=self.create(['subtitle'],{'subtitles':{'languages':['en','fr']}})
        info=self.info();info['subtitles']['fr']=[{'url':self.url+'/en.vtt','ext':'vtt'}]
        task=self.store.claim(self.store.owner)
        updates=[]
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=info) as extract, \
             patch('webapp.cookie_pool.CookiePool.outcome', autospec=True) as start:
            result=self.run_task(task, report=updates.append)
            self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        self.assertEqual(extract.call_count,1)
        self.assertEqual(start.call_count,1)
        self.assertEqual(result['delivery_status'],'complete')
        self.assertEqual(self.hits,['/en.vtt','/en.vtt'])
        self.assertEqual(self.client.get('/api/jobs/'+job).json['file_count'],2)
        for language in ('en','fr'):
            self.assertTrue(any(progress.get('output_kind')=='subtitle' and
                progress.get('output_language')==language and progress['stage']=='downloading'
                for progress in updates),language)
        files=self.client.get('/api/media/assets?type=subtitle').json['artifacts']['items']
        self.assertEqual({(row['language'],row['subtitle_source']) for row in files},
                         {('en','manual'),('fr','manual')})

    def test_mixed_delivery_starts_once_and_uses_common_transfer_admission_for_every_output(self):
        from webapp.worker.pipeline import SourceExecution
        from webapp.request_scheduler import RequestScheduler
        self.create(['av','subtitle'])
        reserve=SourceExecution.transmitting
        admitted=[]
        updates=[]
        def transmitting(executor):
            admitted.append((executor.session.progress.state.get('output_kind'),1))
            return reserve(executor)
        with patch.object(SourceExecution,'transmitting',transmitting), \
             patch('webapp.cookie_pool.CookiePool.outcome', autospec=True) as start:
            task,result=self.execute(report=updates.append)
        self.assertEqual(start.call_count,1)
        self.assertEqual([kind for kind,_ in admitted],['video_only','audio_only','subtitle'])
        self.assertEqual(admitted[-1][1],1)
        self.assertEqual(result['delivery_status'],'complete')
        expected=sum((self.media/name).stat().st_size for name in ('video.mp4','audio.m4a','en.vtt'))
        self.assertEqual(updates[-1]['downloaded_bytes'],expected)
        for kind in ('video_only','audio_only','subtitle'):
            self.assertTrue(any(progress.get('output_kind')==kind and progress['stage']=='downloading'
                for progress in updates),kind)

    def test_original_automatic_alias_keeps_requested_language_and_avoids_translated_url(self):
        job=self.create(['subtitle'])
        info=self.info(False)
        info['automatic_captions']={'en-orig':[{'url':self.url+'/en.vtt','ext':'vtt'}],
                                    'en':[{'url':self.url+'/limited.vtt','ext':'vtt'}]}
        _,result=self.execute(info)
        self.assertEqual(result['delivery_status'],'complete')
        self.assertEqual(self.hits,['/en.vtt'])
        files=self.client.get('/api/jobs/'+job+'/artifacts').json['items']
        self.assertEqual(files[0]['language'],'en')

    def test_default_original_mode_discovers_unconfigured_language_without_translation(self):
        job=self.create(['subtitle'],{'subtitles':{'mode':'original'}})
        info=self.info(False)
        info['automatic_captions']={'ja-orig':[{'url':self.url+'/en.vtt','ext':'vtt'}],
            'zh-Hans':[{'url':'https://www.youtube.com/api/timedtext?lang=ja&tlang=zh-Hans','ext':'vtt'}]}
        _,result=self.execute(info)
        self.assertEqual(result['delivery_status'],'complete')
        self.assertEqual(self.hits,['/en.vtt'])
        files=self.client.get('/api/jobs/'+job+'/artifacts').json['items']
        self.assertEqual([row['language'] for row in files],['ja'])
        self.assertEqual(files[0]['subtitle_source'],'original_auto')

    def test_live_chat_is_not_requested_or_registered_as_a_subtitle_requirement(self):
        job=self.create(['subtitle'])
        info=self.info()
        info['subtitles']['live_chat']=[{'url':self.url+'/chat.json','ext':'json',
                                        'protocol':'youtube_live_chat_replay'}]
        task,result=self.execute(info)
        self.assertEqual(result['delivery_status'],'complete')
        self.assertEqual(self.hits,['/en.vtt'])
        with self.store.connect() as db:
            languages=db.execute(select(delivery_requirements.c.language).where(
                delivery_requirements.c.task_id==task['id'],delivery_requirements.c.kind=='subtitle')).scalars().all()
        self.assertEqual(languages,['en'])
        files=self.client.get('/api/jobs/'+job+'/artifacts').json['items']
        self.assertEqual([(row['kind'],row['language']) for row in files],[('subtitle','en')])

    def test_chat_only_video_delivers_media_and_reports_missing_captions(self):
        job=self.create(['video_only','subtitle'])
        info=self.info(False)
        info['subtitles']['live_chat']=[{'url':self.url+'/chat.json','ext':'json',
                                        'protocol':'youtube_live_chat_replay'}]
        task,result=self.execute(info)
        self.assertEqual(result['delivery_status'],'review')
        self.assertEqual(self.hits,['/video.mp4'])
        with self.store.connect() as db:
            missing=db.execute(select(delivery_requirements.c.language,delivery_requirements.c.status).where(
                delivery_requirements.c.task_id==task['id'],delivery_requirements.c.kind=='subtitle')).all()
        self.assertEqual(missing,[('','missing')])
        files=self.client.get('/api/jobs/'+job+'/artifacts').json['items']
        self.assertEqual([row['kind'] for row in files],['video_only'])

    def test_translated_subtitle_source_is_preserved_in_library(self):
        self.create(['subtitle'],{'subtitles':{'mode':'original_and_translated','languages':['fr']}})
        info=self.info(False)
        info['automatic_captions']={'fr':[{'url':self.url+'/en.vtt','ext':'vtt'}]}
        with patch('webapp.subtitle_tracks.translated',return_value=True), \
             patch('webapp.delivery.executor.translated',return_value=True):
            self.execute(info)
        files=self.client.get('/api/media/assets?type=subtitle').json['artifacts']['items']
        self.assertEqual([(row['language'],row['subtitle_source']) for row in files],[('fr','translated')])

    def test_subtitle_source_survives_verified_file_retry(self):
        from web_worker import execute
        job=self.create(['subtitle']);task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()), \
             patch.object(self.store.statistics,'record_file',side_effect=RuntimeError('interrupted before receipt')):
            with self.assertRaises(RuntimeError):
                self.run_task(task)
        self.retry_failed(job,task);self.hits.clear()
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info') as extract:
            self.execute();extract.assert_not_called()
        files=self.client.get('/api/media/assets?type=subtitle').json['artifacts']['items']
        self.assertEqual([(row['language'],row['subtitle_source']) for row in files],[('en','manual')])
        self.assertEqual(self.hits,[])

    def test_opt_in_targets_download_alongside_original_and_missing_targets_need_review(self):
        for targets, policy, status in [(['fr'],'available_selected','complete'),
                                        (['ja'],'available_selected','review'),
                                        (['fr','ja'],'all_selected','review')]:
            with self.subTest(targets=targets):
                self.hits.clear()
                job=self.create(['subtitle'],{'subtitles':{'mode':'original_and_translated',
                    'languages':targets,'language_policy':policy}})
                info=self.info();info['automatic_captions']={'fr':[{'url':self.url+'/en.vtt','ext':'vtt'}]}
                _,result=self.execute(info)
                self.assertEqual(result['delivery_status'],status)
                files=self.client.get('/api/jobs/'+job+'/artifacts').json['items']
                # The fixture exposes English and French originals; selected
                # translation targets add to that set when available.
                self.assertEqual({row['language'] for row in files}, {'en', 'fr'})
                self.assertEqual(len(self.hits), 2)

    def test_subtitle_http_request_uses_the_cookie_created_during_metadata_extraction(self):
        from web_worker import execute
        self.create(['subtitle'])
        task=self.store.claim(self.store.owner)
        cookies=[]
        original=self.server.RequestHandlerClass.func.do_GET
        def download(handler):
            cookies.append(handler.headers.get('Cookie'))
            return original(handler)
        def extract(downloader,*args,**kwargs):
            downloader._load_cookies('caption_session=from_metadata',autoscope=self.url)
            return self.info()
        with patch.object(self.server.RequestHandlerClass.func,'do_GET',download), \
             patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',extract):
            result=self.run_task(task)
            self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        self.assertEqual(cookies,['caption_session=from_metadata'])

    def test_original_tracks_use_one_probe_each_and_never_ffmpeg_or_file_copy(self):
        from webapp.delivery.verify import inspect
        self.create(['video_only','audio_only','subtitle','thumbnail','metadata'],
                    {'video':{'container':'mp4'}, 'subtitles':{'languages':['en'],'format':'vtt'}})
        commands=[];run=subprocess.run;copy=shutil.copyfile
        def track(command, *args, **kwargs):
            commands.append(Path(command[0]).stem)
            return run(command, *args, **kwargs)
        with patch('webapp.delivery.verify.subprocess.run', side_effect=track), \
             patch('webapp.delivery.processing.inspect', wraps=inspect) as probe, \
             patch('webapp.delivery.files.shutil.copyfile', side_effect=lambda source,target: (_ for _ in ()).throw(AssertionError('full-file media copy')) if Path(source).suffix != '.txt' else copy(source,target)):
            _, result=self.execute()
        self.assertEqual(result['file_count'],5)
        self.assertEqual(probe.call_count,2)
        self.assertNotIn('ffmpeg', commands)
        with self.store.connect() as db:
            rows=list(db.execute(select(delivery_artifacts)).mappings())
        for row in rows:
            original={'video_only':'video.mp4','audio_only':'audio.m4a','subtitle':'en.vtt'}.get(row['kind'])
            if original:
                self.assertEqual(Path(row['path']).read_bytes(), (self.media/original).read_bytes())

    def test_local_claims_have_separate_capacity_and_bypass_network_disk_gate(self):
        from web_worker import execute
        from webapp.database import delivery_work
        self.store.set_runtime({**self.store.runtime(),'postprocess_limit':1})
        first_job=self.create(['audio_only']);second_job=self.create(['audio_only'])
        first=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            result=execute('download',first['payload'],store=self.store,task_id=first['id'])
        self.assertTrue(result['processing_pending'])
        self.assertTrue(self.store.finish(first['id'],result=result,worker_id=self.store.owner))
        self.assertEqual(self.store.get(first['id'])['progress']['stage'],'waiting_processing')
        self.assertEqual(self.store.runnable_counts().get('processing'),1)
        second=self.store.claim(self.store.owner)
        self.assertNotEqual(first['id'],second['id'])
        with patch('webapp.disk_budget.resource_blocked',return_value=True):
            local=self.store.claim(self.store.owner,processing=True)
        self.assertEqual(local['id'],first['id'])
        from webapp.database import session_tasks
        with self.store.connect() as db:
            activity=db.execute(select(session_tasks).where(session_tasks.c.task_id==first['id'])).mappings().one()
        self.assertEqual(activity['status'],'running')
        self.assertIsNone(activity['finished_at'])
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            result=execute('download',second['payload'],store=self.store,task_id=second['id'])
        self.store.finish(second['id'],result=result,worker_id=self.store.owner)
        self.assertIsNone(self.store.claim(self.store.owner,processing=True),'local pool limit must be authoritative')
        with patch('webapp.youtube_auth.YouTubeAuth.require',side_effect=AssertionError('account acquired')), \
             patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',side_effect=AssertionError('network requested')):
            result=execute('download',local['payload'],store=self.store,task_id=local['id'],worker_slot=-2)
        self.store.finish(local['id'],result=result,worker_id=self.store.owner)
        self.cleanup_result(result)
        with self.store.connect() as db:
            work=dict(db.execute(select(delivery_work)).mappings().one())
            db.execute(delivery_work.update().values(instance_id='another-instance'))
        self.assertIsNone(self.store.claim(self.store.owner,processing=True),'local source files cannot migrate to another instance')
        self.assertIsNone(self.store.claim(self.store.owner),'network workers cannot consume processing plans')

    def test_atomic_move_crash_resumes_without_source_requests_or_duplicate_receipts(self):
        from webapp.delivery import artifacts
        from webapp.database import delivery_work
        job=self.create(['audio_only']);task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()), \
             patch.object(artifacts,'pending',side_effect=RuntimeError('crash after move')):
            with self.assertRaisesRegex(RuntimeError,'crash after move'):
                self.run_task(task)
        with self.store.connect() as db:
            work=dict(db.execute(select(delivery_work)).mappings().one())
        plan=json.loads(work['plan']);pending=plan['jobs'][0]
        self.assertTrue(Path(pending['destination']).is_file())
        self.assertFalse(Path(pending['inputs'][0]).exists())
        self.retry_failed(job,task);self.hits.clear()
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',side_effect=AssertionError('re-download')):
            _, result=self.execute()
        self.assertEqual(self.hits,[])
        self.assertEqual((result['file_count'],result['delivery_status']),(1,'complete'))
        self.assertEqual(self.client.get('/api/jobs/'+job).json['file_count'],1)

    def test_pending_local_files_survive_sweep_and_crash_before_settlement(self):
        from web_worker import execute
        from webapp.database import delivery_work, tasks
        from webapp.disk_budget import DiskBudget
        self.storage={'storage_mode':'test'}
        job=self.create(['video_only','audio_only']);task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            result=execute('download',task['payload'],store=self.store,task_id=task['id'])
        self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        with self.store.connect() as db:
            work=dict(db.execute(select(delivery_work)).mappings().one())
        budget=DiskBudget(self.store.directory,self.store.runtime())
        budget.cleanup(self.store,grace=0)
        self.assertTrue(Path(work['workspace']).exists(),'queued local sources must be protected from cleanup')
        local=self.store.claim(self.store.owner,processing=True)
        result=execute('download',local['payload'],store=self.store,task_id=task['id'],worker_slot=-2)
        self.assertFalse(__import__('webapp.delivery.processing',fromlist=['cleanup_completed']).cleanup_completed(self.store,work))
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id==task['id']).values(lease_until=0))
        self.store.recover()
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id==task['id']).values(available_at=0))
        self.hits.clear();_,result=self.execute()
        self.assertEqual(self.hits,[])
        self.assertEqual(result['file_count'],2)
        self.assertTrue(result['files_deleted'])
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_count'],2)
        with budget.connect(write=False) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)

    def test_processing_pool_continues_after_local_failure_without_redownloading(self):
        from web_worker import execute
        from webapp.worker.queue import drain_queue
        from webapp.database import delivery_work
        pending=[]
        for _ in range(2):
            self.create(['audio_only']);task=self.store.claim(self.store.owner)
            with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
                result=execute('download',task['payload'],store=self.store,task_id=task['id'])
            self.store.finish(task['id'],result=result,worker_id=self.store.owner)
            pending.append(task['id'])
        calls=[]
        def processing(*args, **kwargs):
            calls.append(kwargs['task_id'])
            if len(calls)==1:raise RuntimeError('temporary local failure')
            return execute(*args, **kwargs)
        self.hits.clear()
        with patch('webapp.worker.idle.PROCESSING_IDLE_SECONDS',0), \
             patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',side_effect=AssertionError('source requested')):
            drain_queue(self.store.directory,slot=-2,role='processing',execute_task=processing,
                        store_factory=lambda *args:self.store)
        self.assertEqual(set(calls),set(pending))
        failed,finished=map(self.store.get,calls)
        self.assertEqual(failed['status'],'queued')
        self.assertEqual(failed['progress']['stage'],'retrying')
        self.assertEqual(finished['status'],'succeeded')
        with self.store.connect() as db:
            work=db.execute(select(delivery_work).where(delivery_work.c.task_id==calls[0])).mappings().one()
        self.assertTrue(Path(work['workspace']).exists())
        self.assertEqual(self.hits,[])

    def test_processing_role_completes_in_fresh_process_without_source_or_start_gate(self):
        import sys
        import time
        import web_worker
        from web_worker import execute
        from webapp.database import delivery_work
        self.storage={'storage_mode':'test'}
        self.create(['video_only','audio_only']);task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            result=execute('download',task['payload'],store=self.store,task_id=task['id'])
        self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        acquisition_seconds=dict(self.store.get(task['id'])['progress']['stage_seconds'])
        self.hits.clear()
        (self.store.directory/'throughput.json').write_text(json.dumps(dict(mode='cookie_pool',time=time.time(),
            cookie_pool={'halted':True,'next_start_at':time.time()+3600})))
        worker=Path(web_worker.__file__).resolve()
        process=subprocess.Popen([sys.executable,str(worker),'--state',str(self.store.directory),
            '--slot','-2','--role','processing'],cwd=worker.parent,
            env=dict(os.environ,VIDEO_WORKER_DATABASE_URL=self.store.db.url,PYTHONUTF8='1'),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        try:
            def await_done(identity):
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    if self.store.get(identity)['status']=='succeeded':return
                    self.assertIsNone(process.poll(),'processing worker exited early')
                    time.sleep(.05)
                self.fail('processing task did not complete')
            await_done(task['id'])
            self.assertIsNone(process.poll(),'empty queue must retain the worker')
            # Commit a second real source handoff after the queue was empty.
            self.create(['audio_only']);second=self.store.claim(self.store.owner)
            with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
                result=execute('download',second['payload'],store=self.store,task_id=second['id'])
            self.store.finish(second['id'],result=result,worker_id=self.store.owner)
            self.hits.clear()
            await_done(second['id'])
            self.assertIsNone(process.poll(),'the same PID must handle both tasks')
            self.store.set_downloads_paused(True)
            out,err=process.communicate(timeout=6)
            self.assertEqual(process.returncode,0,out+err)
        finally:
            if process.poll() is None:process.kill();process.communicate()
        done=self.store.get(task['id'])
        self.assertEqual(done['status'],'succeeded',done['error'])
        self.assertEqual(done['result']['file_count'],2)
        self.assertNotIn('r',done['progress']['stage_seconds'])
        for stage,seconds in acquisition_seconds.items():
            self.assertGreaterEqual(done['progress']['stage_seconds'].get(stage,0),seconds)
        self.assertEqual(self.hits,[])
        with self.store.connect() as db:
            self.assertFalse(db.execute(select(delivery_work)).first())
        self.assertFalse(any((self.store.directory/'test-downloads').rglob('*.mp4')))

    def test_delivery_space_reserves_original_tracks_without_copy_multiplier(self):
        from webapp.disk_budget import DiskLease
        self.storage={'storage_mode':'test'}
        self.create(['video_only','audio_only'])
        check=DiskLease.check;requested=[]
        def checked(lease,estimate=0,**kwargs):
            requested.append(estimate)
            return check(lease,estimate,**kwargs)
        with patch.object(DiskLease,'check',checked):
            _,result=self.execute()
        self.assertGreater(max(requested),0)
        self.assertLessEqual(max(requested),result['file_bytes'])
        self.assertTrue(result['files_deleted'])

    def test_progressive_av_already_matching_container_never_invokes_ffmpeg(self):
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(self.media/'video.mp4'),
            '-i',str(self.media/'audio.m4a'),'-c','copy',str(self.media/'combined.mp4')],check=True,capture_output=True)
        info=self.info(False);info['formats']=[dict(format_id='combined',url=self.url+'/combined.mp4',
            ext='mp4',vcodec='h264',acodec='aac',height=120,width=160,protocol='http',http_headers={})]
        job=self.create(['av']);run=subprocess.run;commands=[]
        def track(command,*args,**kwargs):
            commands.append(Path(command[0]).stem)
            return run(command,*args,**kwargs)
        with patch('webapp.delivery.verify.subprocess.run',side_effect=track):
            _,result=self.execute(info)
        self.assertEqual(result['file_count'],1)
        self.assertNotIn('ffmpeg',commands)
        with self.store.connect() as db:
            path=db.execute(select(delivery_artifacts.c.path).where(delivery_artifacts.c.job_id==job)).scalar_one()
        self.assertEqual(Path(path).read_bytes(),(self.media/'combined.mp4').read_bytes())

    def test_all_fifteen_combinations_and_track_reuse(self):
        for length in range(1,5):
            for kinds in itertools.combinations(('subtitle', 'video_only', 'audio_only', 'av'),length):
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

    def test_cover_and_metadata_use_one_extraction_shared_admission_and_no_media_tools(self):
        from web_worker import execute
        from webapp.worker.pipeline import SourceExecution
        from webapp.request_scheduler import RequestScheduler
        job = self.create(['thumbnail', 'metadata'])
        task = self.store.claim(self.store.owner)
        info = self.info()
        info.update(description='中文简介', channel='测试频道', duration=12.5, tags=['汽车'],
                    http_headers={'Authorization': 'private-token'}, cookies='private-cookie',
                    chapters=[dict(title='开场', start_time=0, end_time=12.5, url='secret')])
        reserve = SourceExecution.transmitting
        admitted = []
        def transmitting(executor):
            admitted.append((executor.session.progress.state['output_kind'], 1))
            return reserve(executor)
        updates = []
        find_tool = shutil.which
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info', return_value=info) as extract, \
             patch('webapp.delivery.executor.shutil.which', side_effect=lambda name: None if name in ('ffmpeg', 'ffprobe') else find_tool(name)), \
             patch.object(SourceExecution, 'transmitting', transmitting), \
             patch('webapp.cookie_pool.CookiePool.outcome', autospec=True) as start:
            result = self.run_task(task, report=updates.append)
        self.store.finish(task['id'], result=result, worker_id=self.store.owner)
        self.assertEqual((extract.call_count, start.call_count), (1, 1))
        self.assertEqual(admitted, [('thumbnail', 1)])
        self.assertEqual(self.hits, ['/cover.webp'])
        self.assertEqual((result['delivery_status'], result['file_count']), ('complete', 2))
        self.assertTrue(any(p.get('output_kind') == 'thumbnail' and p['stage'] == 'downloading' for p in updates))
        rows = self.client.get('/api/jobs/' + job + '/artifacts').json['items']
        for row in rows:
            with self.client.get(row['content_url']) as response:
                self.assertEqual(response.status_code, 200)
                if row['kind'] == 'metadata':
                    self.assertEqual(response.mimetype, 'application/json')
                    self.assertEqual(response.json['description'], '中文简介')
                    self.assertEqual(response.json['chapters'], [dict(title='开场', start_time=0, end_time=12.5)])
                    for private in ('private-token', 'private-cookie', 'secret', self.url):
                        self.assertNotIn(private, response.text)
                else:
                    self.assertEqual(response.mimetype, 'image/webp')
            with self.client.get(row['content_url'] + '?download=1') as response:
                self.assertIn('attachment', response.headers['Content-Disposition'])
        group = self.client.get('/api/media/groups?type=thumbnail').json
        self.assertEqual(group['items'][0]['kind_counts'], {'thumbnail': 1, 'metadata': 1})
        self.assertEqual(group['counts']['metadata'], 1)

    def test_metadata_only_never_downloads_remote_files(self):
        self.create(['metadata'])
        _, result = self.execute()
        self.assertEqual(result['delivery_status'], 'complete')
        self.assertEqual(self.hits, [])

    def test_cover_falls_back_from_missing_resolution_and_honors_rate_limits(self):
        self.create(['thumbnail'])
        info = self.info();info['thumbnails'].append({'url': self.url + '/absent.jpg'})
        _, result = self.execute(info)
        self.assertEqual(result['delivery_status'], 'complete')
        self.assertEqual(self.hits, ['/absent.jpg', '/cover.webp'])
        from web_worker import execute
        from webapp.download_policy import failure_evidence
        job = self.create(['thumbnail'])
        task = self.store.claim(self.store.owner)
        info['thumbnails'][-1]['url'] = self.url + '/limited.vtt'
        self.hits.clear()
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info', return_value=info):
            with self.assertRaises(RuntimeError) as caught:
                self.run_task(task)
        self.assertEqual(failure_evidence(caught.exception).http_status, 429)
        self.assertGreaterEqual(failure_evidence(caught.exception).retry_after, 2390)
        self.assertEqual(self.hits, ['/limited.vtt'])
        self.assertEqual(self.client.get('/api/jobs/' + job + '/artifacts').json['items'], [])

    def test_missing_cover_is_review_and_invalid_image_is_never_published(self):
        job = self.create(['thumbnail', 'metadata'])
        info = self.info();info.pop('thumbnails')
        _, result = self.execute(info)
        self.assertEqual((result['delivery_status'], result['file_count']), ('review', 1))
        self.assertEqual(self.hits, [])
        self.assertEqual(self.client.get('/api/jobs/' + job + '/artifacts').json['items'][0]['kind'], 'metadata')
        from web_worker import execute
        from webapp.delivery.verify import LocalDeliveryError
        job = self.create(['thumbnail'])
        info = self.info();info['thumbnails'] = [{'url': self.url + '/en.vtt'}]
        task = self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info', return_value=info):
            with self.assertRaises(LocalDeliveryError):
                self.run_task(task)
        self.assertEqual(self.client.get('/api/jobs/' + job + '/artifacts').json['items'], [])

    def test_sidecar_variants_do_not_depend_on_media_or_subtitle_options(self):
        original = normalize({'outputs': ['thumbnail', 'metadata']})
        changed = normalize({'outputs': list(KINDS), 'video': {'max_height': 2160},
                             'audio': {'format': 'mp3'}, 'subtitles': {'format': 'vtt'}})
        for kind in ('thumbnail', 'metadata'):
            self.assertEqual(variant(original, kind), variant(changed, kind))

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
        self.assertEqual(after['file_bytes'],before['file_bytes']);self.assertEqual(after['file_count'],len(KINDS));self.assertEqual(self.hits,[])

    def test_two_tasks_publish_into_independent_directories_and_library_sees_both(self):
        for directory in ('customer-a', 'customer-b'):
            job = self.create(['audio_only'], {'storage': {'path': directory}})
            self.execute()
            with self.store.connect() as db:
                path = Path(db.execute(select(delivery_artifacts.c.path).where(delivery_artifacts.c.job_id == job)).scalar_one())
            self.assertEqual(path.parent, self.root / directory / 'npNnbj46_ik')
            self.assertTrue(path.is_file())
        result = self.client.get('/api/media/groups').json
        self.assertEqual(result['file_count'], 2)
        self.assertEqual(result['total'], 1)

    def test_old_oss_preview_uses_original_bucket_after_latest_repair(self):
        from webapp.delivery.artifacts import save_contract
        job = self.create(['audio_only']);task,_ = self.execute()
        with self.store.connect() as db:
            row = db.execute(select(delivery_artifacts).where(delivery_artifacts.c.job_id == job)).mappings().one()
            db.execute(delivery_artifacts.update().where(delivery_artifacts.c.id == row['id']).values(storage='oss',object_key='old/audio'))
            save_contract(db, job, {'storage': {'oss_bucket': 'new-bucket'}})
        response = self.client.post('/api/jobs/' + job + '/redeliver',json={'ids':[task['id']], 'use_latest':True},headers=self.headers)
        self.assertEqual(response.status_code,202,response.json)
        with patch('Utils.oss_storage.OSSStorage') as storage:
            response = self.client.head('/api/delivery/files/' + row['id'])
            self.assertEqual(response.status_code,200)
            self.assertEqual(storage.call_args.args[0]['oss_bucket'], '')

    def test_contract_pin_and_latest_repair_only_fetch_added_output(self):
        job=self.create(['audio_only']);task,_=self.execute();old=self.client.get('/api/jobs/'+job).json
        response=self.client.put('/api/jobs/'+job,json={'edit_version':old['edit_version'],'delivery':{'outputs':['audio_only','video_only']}},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.hits.clear();self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']],'use_latest':True},headers=self.headers)
        self.execute();self.assertNotIn('/audio.m4a',self.hits);self.assertEqual(self.hits.count('/video.mp4'),1)

    def test_test_mode_has_no_formal_files_or_bill(self):
        self.storage={'storage_mode':'test'}
        job=self.create(KINDS);_,result=self.execute()
        self.assertTrue(result['files_deleted']);self.assertEqual(result['file_count'],len(KINDS))
        detail=self.client.get('/api/jobs/'+job).json
        self.assertEqual(detail['file_count'],0);self.assertEqual(detail['test_file_count'],len(KINDS))
        self.assertEqual(self.client.get('/api/jobs/'+job+'/artifacts').json['items'],[])

    def test_test_retry_reuses_registered_outputs_after_workspace_deletion(self):
        from web_worker import execute
        from webapp.delivery import executor
        from webapp.disk_budget import DiskPressure, DiskBudget
        self.storage={'storage_mode':'test'}
        job=self.create(['metadata','thumbnail','video_only','audio_only','subtitle'])
        task=self.store.claim(self.store.owner)
        publish=executor.publish
        def interrupt(session, source, kind, *args, **kwargs):
            if kind=='audio_only':
                raise DiskPressure('interrupt before audio delivery')
            return publish(session, source, kind, *args, **kwargs)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()), \
             patch.object(executor,'publish',side_effect=interrupt):
            with self.assertRaises(DiskPressure):
                self.run_task(task)
        budget=DiskBudget(self.store.directory,self.store.runtime())
        self.assertLess(budget.snapshot(force=True)['categories']['test']['used_bytes'],64*1024)
        before=self.client.get('/api/jobs/'+job).json
        self.assertEqual(before['test_file_count'],3)
        self.retry_failed(job,task);self.hits.clear()
        _,result=self.execute()
        self.assertEqual(self.hits,[],'local retry must reuse all acquired tracks and captions')
        self.assertEqual((result['file_count'],result['delivery_status']),(5,'complete'))
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_count'],5)
        self.assertTrue(result['files_deleted'])

    def test_partial_crash_recovery_registers_once_and_removes_queued_workspace(self):
        from web_worker import execute
        from webapp.disk_budget import DiskBudget
        from webapp.delivery.verify import LocalDeliveryError
        self.storage={'storage_mode':'test'}
        job=self.create(['metadata','audio_only'])
        task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()), \
             patch.object(self.store.statistics,'record_file',side_effect=LocalDeliveryError('crash before registration')):
            with self.assertRaises(LocalDeliveryError):
                self.run_task(task)
        receipt=next((self.store.directory/'test-downloads'/task['id']).glob('*/.completed-files.json'))
        self.store.release(task['id'],self.store.owner,'retry',delay=0)
        task=self.store.claim(self.store.owner)
        budget=DiskBudget(self.store.directory,self.store.runtime())
        self.assertIsNone(budget.recover_test_result(self.store,task['id']))
        self.assertFalse(receipt.parent.exists(),'accounted partial test files must not accumulate')
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_count'],1)
        self.store.release(task['id'],self.store.owner,'continue',delay=0)
        self.hits.clear();_,result=self.execute()
        self.assertEqual(self.hits,['/audio.m4a'])
        self.assertEqual(result['delivery_status'],'complete')
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_count'],2)

    def test_last_crash_receipt_reports_all_previously_completed_outputs(self):
        from web_worker import execute
        from webapp.delivery import executor
        from webapp.delivery.verify import LocalDeliveryError
        from webapp.disk_budget import DiskBudget
        self.storage={'storage_mode':'test'}
        job=self.create(['metadata','audio_only']);task=self.store.claim(self.store.owner)
        record=self.store.statistics.record_file
        def interrupted(*args,**kwargs):
            if kwargs.get('artifact',{}).get('kind')=='audio_only':
                raise LocalDeliveryError('last output pending registration')
            return record(*args,**kwargs)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()), \
             patch.object(self.store.statistics,'record_file',side_effect=interrupted):
            with self.assertRaises(LocalDeliveryError):
                self.run_task(task)
        receipt=next((self.store.directory/'test-downloads'/task['id']).glob('*/.completed-files.json'))
        # A crash workspace need only contain the last output. Earlier durable
        # test receipts remain valid after their local files have been removed.
        items=[r for r in json.loads(receipt.read_text()) if r['artifact']['kind']=='audio_only']
        receipt.write_text(json.dumps(items))
        self.store.release(task['id'],self.store.owner,'retry',delay=0)
        task=self.store.claim(self.store.owner, processing=True)
        self.assertIsNone(DiskBudget(self.store.directory,self.store.runtime()).recover_test_result(self.store,task['id']))
        result=self.run_task(task)
        self.store.finish(task['id'],result=result,worker_id=self.store.owner)
        self.cleanup_result(result)
        self.assertEqual(result['file_count'],2)
        self.assertEqual(result['file_bytes'],self.client.get('/api/jobs/'+job).json['test_file_bytes'])
        self.assertFalse(receipt.parent.exists())

    def test_superseded_test_receipt_cleans_queued_and_completed_workspaces(self):
        from web_worker import execute
        from webapp.disk_budget import DiskBudget
        from webapp.delivery.executor import recover_test_outputs
        from webapp.delivery.verify import LocalDeliveryError
        self.storage={'storage_mode':'test'}
        job=self.create(['metadata','audio_only']);task=self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()), \
             patch.object(self.store.statistics,'record_file',side_effect=LocalDeliveryError('crash')):
            with self.assertRaises(LocalDeliveryError):
                self.run_task(task)
        receipt=next((self.store.directory/'test-downloads'/task['id']).glob('*/.completed-files.json'))
        receipts=json.loads(receipt.read_text());item=receipts[0]
        self.assertIsNone(recover_test_outputs(self.store,task['id'],receipts,receipt.parent))
        replacement=self.root/'replacement.json';replacement.write_text('{"title":"changed metadata size"}')
        self.store.statistics.record_file(task['id'],replacement,task['video_id'],'fixture',test_mode=True,artifact=item['artifact'])
        self.assertNotEqual(replacement.stat().st_size,item['size'])
        before=self.client.get('/api/jobs/'+job).json['test_file_bytes']
        # Recovery must not overwrite the newer artifact with the stale file.
        self.assertIsNone(recover_test_outputs(self.store,task['id'],receipts,receipt.parent))
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(delivery_artifacts.c.path).where(
                delivery_artifacts.c.id==item['artifact']['id'])).scalar_one(),str(replacement.resolve()))
        self.store.release(task['id'],self.store.owner,'retry',delay=0)
        budget=DiskBudget(self.store.directory,self.store.runtime())
        snapshot=budget.cleanup(self.store,grace=0)
        self.assertFalse(receipt.parent.exists())
        self.assertEqual(snapshot['last_cleanup']['pending_results'],0)
        self.assertEqual(self.client.get('/api/jobs/'+job).json['test_file_bytes'],before)
        # A second retired workspace remains safe to collect after task success.
        with budget.lease('test',task=task['id']) as old:
            stale=old.path/'.completed-files.json';stale.write_text(json.dumps(receipts));old.preserve=True
        task=self.store.claim(self.store.owner)
        self.store.finish(task['id'],result={'test_mode':True},worker_id=self.store.owner)
        budget.cleanup(self.store,grace=0)
        self.assertFalse(old.path.exists())

    def test_language_policy_and_delivery_filter_without_task_filter(self):
        job=self.create(['subtitle'],{'subtitles':{'mode':'original_and_translated','languages':['en','fr'],'language_policy':'all_selected'}})
        task,result=self.execute()
        self.assertEqual(result['delivery_status'],'review')
        rows=self.client.get('/api/jobs/'+job+'/videos?delivery_status=review').json['items']
        self.assertEqual(len(rows),1)
        self.assertEqual({(r['language'],r['status']) for r in rows[0]['delivery']['requirements']},{('en','done'),('fr','missing')})
        self.assertEqual(self.client.get('/api/jobs/'+job+'/videos?delivery_status=complete').json['items'],[])
        job=self.create(['subtitle'],{'subtitles':{'mode':'original_and_translated','languages':['en','fr'],'language_policy':'available_selected','format':'vtt'}})
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

    def test_file_organization_setting_applies_to_video_audio_and_each_subtitle(self):
        for sharded in (True,False):
            with self.subTest(shard_downloads=sharded):
                self.storage={'shard_downloads':sharded}
                job=self.create(KINDS,{'subtitles':{'mode':'original'}})
                info=self.info();info['subtitles']['fr']=[{'url':self.url+'/en.vtt','ext':'vtt'}]
                task,result=self.execute(info)
                self.assertEqual(result['file_count'],7)
                with self.store.connect() as db:
                    rows=list(db.execute(select(delivery_artifacts).where(delivery_artifacts.c.job_id==job)).mappings())
                self.assertEqual({(row['kind'],row['language']) for row in rows},
                    {('av',''),('video_only',''),('audio_only',''),('subtitle','en'),('subtitle','fr'),('thumbnail',''),('metadata','')})
                paths=[Path(row['path']) for row in rows]
                self.assertEqual(len(set(paths)),7)
                self.assertEqual(len({path.parent for path in paths}),1)
                for path in paths:
                    relative=path.relative_to(self.root/'videos')
                    self.assertEqual(relative.parts[:-1],('npNnbj46_ik',) if sharded else ())
                    self.assertTrue(path.is_file())
                for row in rows:
                    with self.client.get('/api/delivery/files/'+row['id']) as response:
                        self.assertEqual(response.status_code,200)

    def test_sharded_oss_retry_preserves_video_directory_and_object_key(self):
        from Utils.oss_storage import OSSStorage,OSSUploadError
        from web_worker import execute
        self.storage={'storage_mode':'oss','shard_downloads':True,
            'oss_endpoint':'https://oss-cn-hangzhou.aliyuncs.com','oss_bucket':'fixture-bucket',
            'oss_prefix':'exports','oss_access_key_id':'fixture-id','oss_access_key_secret':'fixture-secret'}
        job=self.create(['subtitle']);task=self.store.claim(self.store.owner)
        uploads=[]
        def upload(storage,path,key,report):
            uploads.append((Path(path),key))
            if len(uploads)==1:
                raise OSSUploadError('fixture upload failure')
        with patch('oss2.Bucket'),patch.object(OSSStorage,'_upload',autospec=True,side_effect=upload), \
             patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()):
            with self.assertRaises(OSSUploadError):
                self.run_task(task)
            self.retry_failed(job,task);self.hits.clear()
            with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info') as extract:
                self.execute();extract.assert_not_called()
        self.assertEqual(self.hits,[])
        self.assertEqual(len(uploads),2)
        self.assertEqual(uploads[0],uploads[1])
        path,key=uploads[1]
        self.assertEqual(path.parent.name,'npNnbj46_ik')
        self.assertEqual(key,f'exports/npNnbj46_ik/{path.name}')
        with self.store.connect() as db:
            row=db.execute(select(delivery_artifacts).where(delivery_artifacts.c.job_id==job)).mappings().one()
        self.assertEqual((row['path'],row['object_key'],row['available']),(str(path),key,1))
        self.assertEqual(row['subtitle_source'],'manual')

    def retry_failed(self,job,task):
        self.store.finish(task['id'],error='interrupted',worker_id=self.store.owner)
        response=self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']]},headers=self.headers)
        self.assertEqual(response.status_code,202,response.json)

    def test_upload_failure_reuses_verified_file_without_source_health_penalty(self):
        from Utils.oss_storage import OSSUploadError
        from web_worker import execute
        self.storage={'storage_mode':'oss','oss_endpoint':'https://oss-cn-hangzhou.aliyuncs.com',
            'oss_bucket':'fixture-bucket','oss_access_key_id':'fixture-id','oss_access_key_secret':'fixture-secret'}
        job=self.create(['audio_only']);task=self.store.claim(self.store.owner)
        cloud=MagicMock();cloud.upload_files.side_effect=OSSUploadError('fixture upload failure')
        with patch('Utils.oss_storage.OSSStorage',return_value=cloud),patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()),patch('webapp.request_scheduler.RequestScheduler.outcome') as outcome:
            with self.assertRaises(OSSUploadError):self.run_task(task)
            outcome.assert_called_once()
            self.assertTrue(outcome.call_args.kwargs['success'],'network success is independent of local upload failure')
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

    def test_shared_transfer_busy_preserves_delivered_media_for_subtitle_retry(self):
        from web_worker import execute
        from webapp.throughput import PipelineBusy
        job=self.create(['av','subtitle']);task=self.store.claim(self.store.owner)
        from webapp.worker.pipeline import SourceExecution
        transmitting=SourceExecution.transmitting
        def reserve(executor,*args,**kwargs):
            if executor.session.progress.state.get('output_kind')=='subtitle':
                raise PipelineBusy('shared transfer busy')
            return transmitting(executor,*args,**kwargs)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()),patch.object(SourceExecution,'transmitting',reserve):
            with self.assertRaises(PipelineBusy):self.run_task(task)
        self.assertEqual(self.client.get('/api/jobs/'+job).json['file_count'],0)
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
        self.storage={'storage_mode':'test'}
        job=self.create(['audio_only']);task,_=self.execute()
        detail=self.client.get('/api/jobs/'+job).json
        spec=detail['delivery']['contract']['spec'];spec['storage']['storage_mode']='local'
        response=self.client.put('/api/jobs/'+job,json={'edit_version':detail['edit_version'],'delivery':spec},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.client.post('/api/jobs/'+job+'/redeliver',json={'ids':[task['id']],'use_latest':True},headers=self.headers)
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
        self.storage={'storage_mode':'test'}
        for kinds,complete in [(['audio_only'],True),(['audio_only','av'],False)]:
            job=self.create(kinds);task=self.store.claim(self.store.owner)
            with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',return_value=self.info()),patch.object(self.store.statistics,'record_file',side_effect=LocalDeliveryError('simulated interruption')):
                with self.assertRaises(LocalDeliveryError):self.run_task(task)
            receipt=next((self.store.directory/'test-downloads'/task['id']).glob('*/.completed-files.json'))
            result=recover_test_outputs(self.store,task['id'],json.loads(receipt.read_text()),receipt.parent)
            self.assertEqual(bool(result),complete)
            self.store.finish(task['id'],result=result or {},worker_id=self.store.owner)

    def test_only_task_defaults_and_stale_contract_edit(self):
        response=self.client.put('/api/delivery/defaults',json={'outputs':['subtitle']},headers=self.headers)
        self.assertEqual(response.status_code,404,response.json)
        self.assertEqual(self.client.get('/api/delivery/defaults').status_code,404)
        job=self.create(['av']);old=self.client.get('/api/jobs/'+job).json['edit_version']
        self.assertEqual(self.client.put('/api/jobs/'+job,json={'edit_version':old,'delivery':{'outputs':['subtitle']}},headers=self.headers).status_code,200)
        self.assertEqual(self.client.put('/api/jobs/'+job,json={'edit_version':old,'delivery':{'outputs':['av']}},headers=self.headers).status_code,409)

    def test_source_failures_preserve_account_and_http_classification(self):
        import yt_dlp
        from web_worker import execute
        from webapp.download_policy import failure_evidence
        for message,kind in [('HTTP Error 403: Forbidden','access_denied'),('HTTP Error 429: Too Many Requests','request_rate_limit'),('Your account has been rate-limited','account_rate_limit')]:
            job=self.create(['audio_only']);task=self.store.claim(self.store.owner)
            with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info',side_effect=yt_dlp.utils.DownloadError(message)),patch('webapp.request_scheduler.RequestScheduler.outcome'):
                with self.assertRaises(RuntimeError) as caught:self.run_task(task)
            self.assertEqual(failure_evidence(caught.exception).kind,kind)
            self.store.finish(task['id'],error=message,worker_id=self.store.owner)
            from webapp.cookie_pool import CookiePool
            pool=CookiePool(self.store,self.store.runtime(),self.root,None)
            pool.router.transaction_call(lambda db,stamp:[pool.router._save(db,scope,{}) for _,_,scope in pool.accounts])


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
        self.assertEqual(self.client.put('/api/delivery/defaults',json={'outputs':['av']},headers=self.headers).status_code,404)
        created = operator.post('/api/jobs',json={'customer':'C','name':'Operator task'},headers=oh)
        self.assertEqual(created.status_code,201,created.json)
        identity = created.json['id']
        detail = operator.get('/api/jobs/' + identity).json
        response = operator.put('/api/jobs/' + identity,json={'edit_version':detail['edit_version'],
            'delivery':{'storage':{'path':'admin-only-folder'}}},headers=oh)
        self.assertEqual(response.status_code,403,response.json)
        response = operator.put('/api/jobs/' + identity,json={'edit_version':detail['edit_version'],
            'delivery':{'outputs':['subtitle']}},headers=oh)
        self.assertEqual(response.status_code,200,response.json)
        from webapp.console_auth import rpc_allowed
        for path,method,allowed in [('/api/delivery/files/'+'a'*64,'GET',True),('/api/delivery/files/'+'a'*64,'HEAD',True),('/api/delivery/files/'+'a'*64,'POST',False),('/api/delivery/artifacts','GET',False),('/api/jobs/any/redeliver','POST',False)]:
            with self.app.test_request_context(path,method=method):self.assertEqual(rpc_allowed(),allowed)


class DeliveryFleetTests(unittest.TestCase):
    from tests import test_fleet as fleet_tests
    setUp=fleet_tests.FleetTests.setUp
    setup_fleet=fleet_tests.FleetTests.setup_fleet
