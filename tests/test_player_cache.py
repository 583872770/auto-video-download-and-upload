"""Public-code reuse across independent accounts and processes, with bounded disk use."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, PropertyMock

from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.extractor.youtube.jsc._builtin.node import NodeJCP
from yt_dlp.extractor.youtube.jsc.provider import (JsChallengeRequest, JsChallengeType, NChallengeInput,
    JsChallengeProviderError)
from webapp.downloader import build_ydl_options
from webapp.parallel_http import ParallelYoutubeDL
from webapp.player_cache import PlayerCache, TTL, MAX_ENTRY_BYTES, public_player
from webapp.disk_budget import MIB, size_of

URL = 'https://www.youtube.com/s/player/1234abcd/player_ias.vflset/en_US/base.js'
SOURCE = 'public player source'
PREPARED = 'public prepared code'


def fake_stdin(stdin):
    marker = 'console.log(JSON.stringify(jsc('
    return json.loads(stdin[stdin.rfind(marker)+len(marker):].split(')));', 1)[0])


def fixtures(counts, *, fail_prepared=False, fail_all=False):
    stack = ExitStack()
    def source(ie, *args, **kwargs):
        counts.append('source')
        return SOURCE
    def solve(provider, stdin):
        data = fake_stdin(stdin)
        kind = 'prepared' if data['type'] == 'preprocessed' else 'raw'
        counts.append(kind)
        if fail_all or fail_prepared and kind == 'prepared':
            raise JsChallengeProviderError('fixture solver failure')
        return json.dumps({'type':'result', 'preprocessed_player':PREPARED,
            'responses':[{'type':'result','data':{c:hashlib.sha256(c.encode()).hexdigest() for c in r['challenges']}}
                for r in data['requests']]})
    stack.enter_context(patch.object(YoutubeIE, '_download_webpage', source))
    stack.enter_context(patch.object(NodeJCP, '_run_js_runtime', solve))
    stack.enter_context(patch.object(NodeJCP, 'runtime_info', new_callable=PropertyMock,
        return_value=SimpleNamespace(version_tuple=(22,23,4), path='node')))
    for name in ('_lib_script','_core_script'):
        stack.enter_context(patch.object(NodeJCP,name,new_callable=PropertyMock,return_value=SimpleNamespace(code='')))
    return stack


def solve_task(state, identity='a'):
    options = build_ydl_options({'_disk_state':str(state),'youtube_sleep_preset':False,'disk_min_free_gb':0}, Path(state))
    options['http_headers']={'X-Fixture-Account':identity}
    with ParallelYoutubeDL(options) as ydl:
        ie = ydl.get_info_extractor('Youtube');ie.initialize()
        request=JsChallengeRequest(type=JsChallengeType.N,video_id='fixture',
            input=NChallengeInput(player_url=URL,challenges=['private-challenge-'+identity]))
        result=ie._jsc_director.bulk_solve([request])
        assert len(result)==1, result
        assert result[0][1].output.results['private-challenge-'+identity]==hashlib.sha256(('private-challenge-'+identity).encode()).hexdigest()
        return result[0][1].output.results


def concurrent_task(state, identity, barrier, queue):
    counts=[]
    try:
        with fixtures(counts):
            barrier.wait(timeout=30)
            result=solve_task(state, identity)
        queue.put({'counts':counts,'result':result})
    except BaseException as e:
        queue.put({'error':type(e).__name__+':'+str(e)})


class PlayerCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)
        self.cache=PlayerCache(self.state,{'disk_min_free_gb':0,'disk_cache_budget_mb':1})
        self.events=[]
        self.logging=patch('webapp.player_cache.note',side_effect=lambda action,**kw:self.events.append((action,kw)))
        self.logging.start();self.addCleanup(self.logging.stop)

    def put(self,url=URL,source=SOURCE,prepared=None,compat=None):
        with self.cache.lock(url) as owned:
            self.assertTrue(owned)
            return self.cache.write(url,source,prepared,compat)

    def test_fresh_downloaders_reuse_public_source_and_prepared_but_solve_each_challenge(self):
        calls=[]
        with fixtures(calls):
            first=solve_task(self.state,'account-a')
            second=solve_task(self.state,'account-b')
        self.assertEqual(calls,['source','raw','prepared'])
        self.assertNotEqual(first,second)
        content=self.cache.path(URL).read_text()
        for private in ('private-challenge','account-a','account-b','answer-'):
            self.assertNotIn(private,content)
        options=build_ydl_options({'_disk_state':str(self.state)},self.state)
        with ParallelYoutubeDL(options) as a,ParallelYoutubeDL(options) as b:
            self.assertIsNot(a.cookiejar,b.cookiejar)
            self.assertIsNot(a.get_info_extractor('Youtube'),b.get_info_extractor('Youtube'))
        self.assertTrue(any(a=='hit' and v.get('kind')=='prepared' for a,v in self.events))

    def test_many_processes_build_same_player_once(self):
        ctx=multiprocessing.get_context('spawn');queue=ctx.Queue();barrier=ctx.Barrier(4)
        processes=[ctx.Process(target=concurrent_task,args=(str(self.state),str(i),barrier,queue)) for i in range(4)]
        for p in processes:p.start()
        try:
            results=[queue.get(timeout=60) for p in processes]
        finally:
            for p in processes:
                p.join(timeout=10)
                if p.is_alive():p.terminate();p.join()
        self.assertTrue(all('error' not in r for r in results),results)
        counts=[c for r in results for c in r['counts']]
        self.assertEqual(counts.count('source'),1,results)
        self.assertEqual(counts.count('raw'),1,results)
        self.assertEqual(counts.count('prepared'),3,results)
        self.assertEqual(len({json.dumps(r['result']) for r in results}),4)

    def test_warm_cache_releases_lock_before_parallel_task_solving(self):
        import threading
        calls=[]
        with fixtures(calls):
            solve_task(self.state,'cold')
            barrier=threading.Barrier(2)
            original=NodeJCP._run_js_runtime
            def simultaneous(provider,stdin):
                self.assertEqual(fake_stdin(stdin)['type'],'preprocessed')
                barrier.wait(timeout=10)
                return original(provider,stdin)
            with patch.object(NodeJCP,'_run_js_runtime',simultaneous),ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(solve_task,self.state,'warm-a')
                b=pool.submit(solve_task,self.state,'warm-b')
                self.assertNotEqual(a.result(timeout=20),b.result(timeout=20))
        self.assertEqual(calls,['source','raw','prepared','prepared'])

    def test_cache_compatibility_change_reuses_source_and_rebuilds_prepared(self):
        calls=[]
        with fixtures(calls):
            solve_task(self.state)
            path=self.cache.path(URL);value=json.loads(path.read_text());value['prepared']['compat']='old-version'
            path.write_text(json.dumps(value))
            solve_task(self.state)
        self.assertEqual(calls,['source','raw','raw'])

    def test_corrupt_hash_refetches_and_failed_cached_solver_rebuilds_only_once(self):
        calls=[]
        with fixtures(calls):solve_task(self.state)
        value=json.loads(self.cache.path(URL).read_text());value['prepared']['code']='damaged'
        self.cache.path(URL).write_text(json.dumps(value))
        with fixtures(calls):solve_task(self.state)
        self.assertEqual(calls,['source','raw','source','raw'])
        with fixtures(calls,fail_prepared=True):solve_task(self.state)
        self.assertEqual(calls[-3:],['prepared','source','raw'])
        with fixtures(calls,fail_all=True):
            with self.assertRaises(AssertionError):solve_task(self.state)
        self.assertEqual(calls[-3:],['prepared','source','raw'])

    def test_disk_write_failure_keeps_original_solver_and_cleans_staging(self):
        calls=[]
        with fixtures(calls),patch('webapp.player_cache.os.replace',side_effect=OSError('full')):
            solve_task(self.state)
        self.assertEqual(calls,['source','raw'])
        self.assertFalse(list(self.cache.root.glob('write-*.tmp')))
        with self.assertRaisesRegex(OSError,'body error'):
            with self.cache.lock(URL):raise OSError('body error')

    def test_lru_count_limit_expiry_and_orphan_temp_cleanup(self):
        urls=[URL.replace('1234abcd',f'{i:08x}') for i in range(3)]
        with patch('webapp.player_cache.MAX_ENTRIES',2):
            for u in urls[:2]:self.assertTrue(self.put(u))
            import os
            os.utime(self.cache.path(urls[0]),(time.time()+1,)*2)
            self.assertTrue(self.put(urls[2]))
            self.assertTrue(self.cache.path(urls[0]).exists())
            self.assertFalse(self.cache.path(urls[1]).exists())
        value=json.loads(self.cache.path(urls[0]).read_text());value['created']=time.time()-TTL-1
        self.cache.path(urls[0]).write_text(json.dumps(value))
        (self.cache.root/'write-crashed.tmp').write_bytes(b'orphan')
        result=self.cache.prune()
        self.assertGreaterEqual(result['removed'],2)
        self.assertFalse(self.cache.path(urls[0]).exists())
        self.assertFalse(list(self.cache.root.glob('write-*.tmp')))

    def test_capacity_accounts_for_other_cache_and_physical_pressure(self):
        self.cache.root.parent.mkdir(parents=True)
        other=self.cache.root.parent/'other.bin';other.write_bytes(b'x'*(700*1024))
        self.assertFalse(self.put(source='x'*(400*1024)))
        self.assertEqual(other.stat().st_size,700*1024)
        self.assertTrue(self.put())
        self.assertLessEqual(size_of(self.cache.root.parent),MIB)
        with patch('webapp.player_cache.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            self.assertFalse(self.put())
        self.assertTrue(self.put())
        result=self.cache.prune(pressure=True)
        self.assertGreater(result['removed'],0)
        self.assertEqual(list(self.cache.root.glob('*.json')),[])

    def test_cleanup_does_not_remove_active_entry_or_follow_symlinks(self):
        self.assertTrue(self.put())
        with ThreadPoolExecutor() as pool:
            with self.cache.lock(URL):
                result=pool.submit(self.cache.prune,pressure=True).result(timeout=10)
                self.assertEqual(result['removed'],0)
                self.assertTrue(self.cache.path(URL).exists())
        self.cache.discard(URL)
        outside=self.state/'outside.txt';outside.write_text('retain')
        try:self.cache.path(URL).symlink_to(outside)
        except OSError:self.skipTest('symlinks unavailable')
        self.assertIsNone(self.cache.read(URL))
        self.cache.prune(pressure=True)
        self.assertEqual(outside.read_text(),'retain')

    def test_oversize_entry_and_disallowed_urls_are_never_used(self):
        self.cache.root.mkdir(parents=True)
        with self.cache.path(URL).open('wb') as f:f.truncate(MAX_ENTRY_BYTES+1)
        self.assertIsNone(self.cache.read(URL))
        for url in (URL+'?token=private',URL.replace('www.youtube.com','example.com'),
                    URL.replace('https:','http:'),URL.replace('/player_ias','/../player_ias')):
            self.assertFalse(public_player(url),url)
        self.assertTrue(public_player(URL))


if __name__=='__main__':unittest.main()
