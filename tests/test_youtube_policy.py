"""Stable source selection, auth attribution and shared process pacing."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select

from webapp.channel_scheduler import ChannelScheduler
from webapp.database import tasks
from webapp.database import download_resources
from webapp.downloader import build_ydl_options
from webapp.downloads import DownloadLeaseLost, DownloadFailure, YouTubeAccountLimited
from webapp.settings import validate_settings
from webapp.store import TaskStore
from webapp.youtube_auth import YouTubeAuth, ProbeResult, SourceHTTPSHandler
import hashlib
import json

COOKIE = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tFIXTURE_ONLY\n'


class YouTubePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / 'state')
        (self.root / 'cookies.txt').write_bytes(COOKIE)

    def scheduler(self, store=None):
        store = store or self.store
        task = store.create('download', {'base': str(self.root), 'urls': ['https://youtu.be/00000000001']})
        with store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status='running', worker_id=store.owner, lease_until=time.time()+3600))
        return ChannelScheduler(store, {'download_method':'yt_dlp', 'youtube_start_interval':1}, task['id'])

    def test_source_only_applies_to_direct_downloads(self):
        config = {'path':'videos', 'proxy_mode':'direct', 'youtube_source_address':'::'}
        self.assertEqual(build_ydl_options(config, self.root)['source_address'], '::')
        self.assertNotIn('source_address', build_ydl_options({**config,'download_method':'bergung'}, self.root))
        config.update(proxy_mode='manual', proxy='http://127.0.0.1:8080')
        self.assertNotIn('source_address', build_ydl_options(config, self.root))
        for value in ('youtube.com', '127.0.0.1:80', 'invalid'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings({'youtube_source_address':value})

    def test_source_handler_binds_tls_connection(self):
        handler = SourceHTTPSHandler('::')
        with patch('webapp.youtube_auth.HTTPSConnection') as connection:
            handler.do_open = lambda factory, request, **kw: factory('www.youtube.com', timeout=15, **kw)
            handler.https_open(Mock())
        self.assertEqual(connection.call_args.kwargs['source_address'], ('::', 0))

    def test_official_sleep_preset_can_be_saved_and_only_applies_to_native_downloader(self):
        config = {'path':'videos', 'youtube_sleep_preset':True}
        options = build_ydl_options(config, self.root)
        self.assertEqual(options['sleep_interval_requests'], .75)
        self.assertEqual((options['sleep_interval'],options['max_sleep_interval']), (10,20))
        self.assertEqual(options['sleep_interval_subtitles'], 5)
        for updated in ({'youtube_sleep_preset':False}, {'download_method':'bergung'}):
            self.assertNotIn('sleep_interval_requests', build_ydl_options({**config,**updated}, self.root))
        self.assertFalse(validate_settings({'youtube_sleep_preset':'false'})['youtube_sleep_preset'])
        with self.assertRaises(ValueError):
            validate_settings({'youtube_sleep_preset':1})

    def test_auth_cache_isolated_by_source_and_probe_uses_same_source(self):
        config = {'youtube_cookie':'cookies.txt', 'proxy_mode':'direct'}
        auth = YouTubeAuth(self.store)
        with patch('webapp.youtube_auth.probe', return_value=('valid','ok')) as probe:
            auth.check(config, self.root)
            auth.check(config, self.root)
            auth.check({**config,'youtube_source_address':'::'}, self.root)
            auth.check({**config,'youtube_source_address':'::'}, self.root)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(probe.call_args.kwargs, {'source_address':'::'})

    def test_anonymous_challenge_preserves_pause_and_precise_attribution(self):
        auth = YouTubeAuth(self.store)
        result = auth.reject('upstream challenge', request_mode='anonymous_fallback', node='test-node')
        self.assertEqual(result['state'], 'verification_required')
        self.assertEqual(result['source'], 'download')
        self.assertEqual(result['request_mode'], 'anonymous_fallback')
        self.assertIn('不能证明', result['message'])
        auth.block(result)
        self.assertTrue(self.store.downloads_paused())
        self.assertEqual(auth.status()['request_mode'], 'anonymous_fallback')

    def test_preflight_evidence_survives_cache_and_workbench_lookup(self):
        from webapp.risk_diagnostics import read_auth, observed
        auth = YouTubeAuth(self.store)
        for evidence, title in [({'http_status': 403}, 'HTTP 403'),
                                ({'verification_reason': 'redirect'}, '跳转到了其他页面')]:
            with self.subTest(evidence=evidence):
                checked = ProbeResult('verification_required', 'fixture', **evidence)
                with patch('webapp.youtube_auth.probe', return_value=checked):
                    auth.check({'youtube_cookie': 'cookies.txt', 'proxy_mode': 'direct'}, self.root, force=True)
                with self.store.connect() as db:
                    saved = read_auth(db, ['local'], standalone=True)['local']
                for key, value in evidence.items():
                    self.assertEqual(saved[key], value)
                fact = next(f for f in observed({'download_method': 'yt_dlp'}, {}, saved, time.time())
                            if f['code'] == 'bot_check')
                self.assertIn(title, fact['title'])

    def test_independent_workers_share_start_interval_and_wait_outside_transaction(self):
        second = TaskStore(self.root / 'other', self.store.db.engine.url.render_as_string(hide_password=False))
        schedulers = [self.scheduler(), self.scheduler(second)]
        def start(scheduler):
            def waiting():
                # A second writer can commit while this worker waits.
                with second.connect() as db:
                    db.execute(tasks.update().where(tasks.c.id == scheduler.task_id).values(title='waiting'))
            scheduler.wait_youtube_start(scheduler.channels[0], on_wait=waiting)
            return time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            starts = sorted(pool.map(start, schedulers))
        self.assertGreaterEqual(starts[1]-starts[0], .9)

    def test_expired_lease_cannot_reserve_another_start(self):
        scheduler = self.scheduler()
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == scheduler.task_id).values(lease_until=0))
        with self.assertRaises(DownloadLeaseLost):
            scheduler.wait_youtube_start(scheduler.channels[0])

    def test_pool_parser_uses_configured_shared_interval_and_preserves_old_window(self):
        scheduler = self.scheduler()
        scheduler.config.update(_cookie_pool_selected='fixture', youtube_cookie_pool_start_interval=3,
                                youtube_start_interval=60)
        stamp = time.time()
        scheduler.clock = lambda db: stamp
        read = lambda db, now: scheduler._state(db, 'youtube-start:local')
        scheduler.wait_youtube_start(scheduler.channels[0])
        self.assertEqual(scheduler.transaction_call(read)['next_at'], stamp + 3)
        def advance(delay):
            nonlocal stamp
            stamp += delay
        with patch('webapp.channel_scheduler.time.sleep', side_effect=advance) as sleep:
            scheduler.wait_youtube_start(scheduler.channels[0])
        self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), 3)
        reserved = stamp + 3
        scheduler.config['youtube_cookie_pool_start_interval'] = 0
        with patch('webapp.channel_scheduler.time.sleep', side_effect=advance) as sleep:
            scheduler.wait_youtube_start(scheduler.channels[0])
        self.assertEqual(stamp, reserved)
        self.assertEqual(scheduler.transaction_call(read)['next_at'], stamp)
        with patch('webapp.channel_scheduler.time.sleep', side_effect=AssertionError('Zero interval slept')):
            scheduler.wait_youtube_start(scheduler.channels[0])

    def test_shared_pool_setting_does_not_change_nonpool_parser_spacing(self):
        scheduler = self.scheduler()
        scheduler.config['youtube_cookie_pool_start_interval'] = 600
        stamp = time.time()
        scheduler.clock = lambda db: stamp
        scheduler.wait_youtube_start(scheduler.channels[0])
        state = scheduler.transaction_call(lambda db, now: scheduler._state(db, 'youtube-start:local'))
        self.assertEqual(state['next_at'], stamp + 1)

    def test_youtube_429_has_conservative_floor_and_respects_longer_retry_after(self):
        scheduler = self.scheduler()
        for requested, expected in ((5,300),(900,900)):
            with self.subTest(requested=requested):
                error = RuntimeError('HTTP Error 429: Too Many Requests')
                error.http_status = 429
                error.retry_after = requested
                scheduler.outcome(scheduler.channels[0], success=False, error=error, phase='resolve')
                scope = hashlib.sha256(('resolver:'+scheduler.channels[0].resolver_group).encode()).hexdigest()
                with self.store.connect() as db:
                    state = json.loads(db.execute(select(download_resources.c.state).where(
                        download_resources.c.key==scope)).scalar_one())
                self.assertGreaterEqual(state['cooldown_until']-time.time(), expected-2)

    def test_backpressure_delays_worker_instead_of_churning_other_queue_items(self):
        from web_worker import drain
        from webapp.throughput import PipelineBusy
        self.store.set_runtime({'min_free_gb':0})
        self.store.set_downloads_paused(False)
        task = self.store.create('download', {'base':str(self.root), 'urls':['https://youtu.be/00000000001'],
                                            'config':{'path':'videos', 'min_free_gb':0}})
        starts = []
        def run(*args, **kwargs):
            starts.append(time.monotonic())
            if len(starts)==1:
                error=PipelineBusy('fixture capacity'); error.retry_after=2
                raise error
            return {'file_count':0}
        with patch('web_worker.TaskStore', return_value=self.store), patch('web_worker.execute', side_effect=run):
            drain(self.store.directory, 0, 'download')
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1]-starts[0], 1.9)
        saved=self.store.get(task['id'])
        self.assertEqual(saved['status'], 'succeeded')
        self.assertEqual(saved['attempts'], 1)

    def test_explicit_private_video_does_not_cool_healthy_channel(self):
        scheduler = self.scheduler()
        channel = scheduler.channels[0]
        scheduler.outcome(channel, success=True)
        before = scheduler.transaction_call(lambda db, stamp: scheduler._state(db,'channel:'+channel.id))
        failure = DownloadFailure()
        failure.error('[youtube] fixture: Video unavailable. This video is private')
        error = failure.as_error()
        self.assertEqual(error.failure_scope, 'video')
        scheduler.outcome(channel, success=False, error=error)
        after = scheduler.transaction_call(lambda db, stamp: scheduler._state(db,'channel:'+channel.id))
        self.assertEqual(before, after)
        unknown = DownloadFailure()
        unknown.error('Video unavailable')
        self.assertFalse(hasattr(unknown.as_error(), 'failure_scope'))

    def test_account_limit_without_http_429_cools_shared_resolver_for_an_hour(self):
        from webapp.channel_scheduler import throttle_details
        from webapp.risk_diagnostics import classify
        failure = DownloadFailure()
        failure.error("Video unavailable. Your account has been rate-limited by YouTube for up to an hour.")
        error = failure.as_error()
        self.assertTrue(error.retryable)
        self.assertEqual(throttle_details(error), (None, 3600))
        self.assertIn('account_rate_limit', classify(str(error)))
        scheduler = self.scheduler()
        scheduler.outcome(scheduler.channels[0], success=False, error=error, phase='resolve')
        scope = 'resolver:' + scheduler.channels[0].resolver_group
        state = scheduler.transaction_call(lambda db, stamp: scheduler._state(db, scope))
        self.assertGreaterEqual(state['cooldown_until']-time.time(), 3598)
        self.assertEqual(state['cooldown_kind'], 'account')

    def test_account_limit_pauses_worker_and_preserves_queue_without_claiming_next_video(self):
        from web_worker import drain
        self.store.set_runtime({'min_free_gb':0})
        self.store.set_downloads_paused(False)
        payload = {'base':str(self.root), 'urls':['https://youtu.be/00000000001'],
                   'config':{'path':'videos','min_free_gb':0}}
        first = self.store.create('download', payload)
        second = self.store.create('download', {**payload,'urls':['https://youtu.be/00000000002']})
        with patch('web_worker.TaskStore', return_value=self.store), patch('web_worker.execute',
                side_effect=YouTubeAccountLimited('Your account has been rate-limited')) as execute:
            drain(self.store.directory, 0, 'download')
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(self.store.downloads_paused())
        saved = self.store.get(first['id'])
        self.assertEqual(saved['status'], 'queued')
        self.assertEqual(saved['attempts'], 0)
        self.assertGreaterEqual(saved['available_at']-time.time(), 3598)
        self.assertEqual(saved['progress']['retry_reason'], 'youtube_account_rate_limit')
        self.assertEqual(self.store.get(second['id'])['attempts'], 0)


if __name__ == '__main__':
    unittest.main()
