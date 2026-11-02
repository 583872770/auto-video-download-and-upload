"""Persistent adaptive admission across workers, identity exports and restarts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from webapp.channel_scheduler import ChannelScheduler
from webapp.database import tasks
from webapp.downloads import DownloadLeaseLost, YouTubeAccountLimited
from webapp.settings import validate_settings, validate_method_urls, DOWNLOAD_FIELDS
from webapp.store import TaskStore
from webapp.throughput import PipelineBusy
from webapp.youtube_auth import YouTubeAuthRequired
from webapp.youtube_pacing import summary


class YouTubePacingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / 'state')
        self.now = time.time()
        self.cookie = self.root / 'cookie.txt'
        self.cookie.write_bytes(b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-only\n')
        self.config = dict(download_method='yt_dlp', youtube_cookie=str(self.cookie),
                           youtube_pacing_mode='adaptive', youtube_start_interval=45,
                           youtube_hourly_start_limit=80, youtube_daily_start_limit=1200)

    def scheduler(self, **config):
        store = TaskStore(self.root / 'other', self.store.db.engine.url.render_as_string(hide_password=False))
        task = store.create('download', {'base': str(self.root), 'urls': ['https://youtu.be/00000000001']})
        with store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status='running', worker_id=store.owner, lease_until=self.now + 10 * 86400))
        router = ChannelScheduler(store, {**self.config, **config}, task['id'], base=self.root)
        router.clock = lambda db: self.now
        return router

    def reserve(self, router):
        return router.transaction_call(lambda db, stamp: router.youtube_pacing.reserve(db, stamp, router.channels[0]))

    def state(self, router, session=False):
        scope = router.youtube_pacing.session_scope if session else router.youtube_pacing.instance_scope
        return router.transaction_call(lambda db, stamp: router._state(db, scope))

    def feedback(self, router, **kwargs):
        router.outcome(router.channels[0], **kwargs)

    def test_parallel_workers_reexports_and_restarts_share_one_ticket(self):
        alias = self.root / 'alias.txt'
        alias.write_bytes(self.cookie.read_bytes() + b'.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tchanged\n')
        first, second = self.scheduler(), self.scheduler(youtube_cookie=str(alias), youtube_source_address='::')
        # Simulate independent instance IDs while preserving the core session.
        second.youtube_pacing.instance_scope = 'youtube-pacing:instance:second'
        second.youtube_pacing.scopes[0] = second.youtube_pacing.instance_scope
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.reserve, (first, second)))
        self.assertEqual(sorted(delay for delay, _ in results), [0, 60])
        self.assertEqual(len(self.state(first, session=True)['starts']), 1)
        restarted = self.scheduler(_cookie_pool_selected='legacy')
        with self.assertRaises(PipelineBusy) as busy:
            restarted.wait_youtube_start(restarted.channels[0])
        self.assertEqual(busy.exception.retry_after, 60)
        self.now += 60
        self.assertEqual(self.reserve(restarted)[0], 0)
        self.assertEqual(len(self.state(restarted, session=True)['starts']), 2)

    def test_hour_budget_rolls_at_boundary_and_increasing_budget_unblocks(self):
        router = self.scheduler(youtube_hourly_start_limit=2)
        start = self.now
        self.reserve(router)
        self.now += 60
        self.reserve(router)
        self.now += 60
        self.assertEqual(self.reserve(router)[0], 3480)
        self.assertEqual(len(self.state(router)['starts']), 2)
        router.config['youtube_hourly_start_limit'] = 3
        self.assertEqual(self.reserve(router)[0], 0)
        self.now = start + 3600
        self.assertEqual(self.reserve(router)[0], 0)

    def test_daily_budget_persists_and_rolls_exactly(self):
        router = self.scheduler(youtube_daily_start_limit=1)
        self.reserve(router)
        self.now += 60
        other = self.scheduler(youtube_daily_start_limit=1)
        self.assertEqual(self.reserve(other)[0], 86340)
        self.now += 86340
        self.assertEqual(self.reserve(other)[0], 0)
        self.assertEqual(len(self.state(other)['starts']), 1)

    def test_expired_lease_cannot_consume_budget(self):
        router = self.scheduler()
        with router.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == router.task_id).values(lease_until=0))
        with self.assertRaises(DownloadLeaseLost):
            self.reserve(router)
        self.assertEqual(self.state(router), {})

    def test_only_sustained_verified_success_warms_and_late_success_keeps_cooldown(self):
        router = self.scheduler()
        self.reserve(router)
        for _ in range(100):
            self.feedback(router, success=True, size=100)
        self.assertEqual(self.state(router)['interval'], 60)
        self.now += 6 * 3600
        self.feedback(router, success=True, size=0)
        self.assertEqual(self.state(router)['interval'], 60)
        self.feedback(router, success=True, size=100)
        self.assertEqual(self.state(router)['interval'], 55)
        self.feedback(router, success=True, size=100)
        self.assertEqual(self.state(router)['interval'], 55)
        for expected in (50, 45, 45):
            self.now += 7200
            self.feedback(router, success=True, size=100)
            self.assertEqual(self.state(router)['interval'], expected)
        error = YouTubeAccountLimited('Your account has been rate-limited')
        self.feedback(router, success=False, error=error)
        before = self.state(router)
        self.feedback(router, success=True, size=100)
        after = self.state(router)
        self.assertEqual(after['cooldown_until'], before['cooldown_until'])
        self.assertEqual(after['clean_successes'], 0)

    def test_repeated_limit_backoff_and_concurrent_incident_dedup(self):
        router = self.scheduler()
        self.reserve(router)
        for expected in (3600, 7200, 14400, 21600):
            error = YouTubeAccountLimited('Your account has been rate-limited')
            self.feedback(router, success=False, error=error)
            self.assertEqual(error.retry_after, expected)
            state = self.state(router)
            self.feedback(router, success=False, error=error)
            again = self.state(router)
            self.assertEqual(again['strikes'], state['strikes'])
            self.assertEqual(again['interval'], state['interval'])
            self.assertEqual(self.reserve(router)[0], expected)
            self.now += expected + 1

    def test_both_scopes_keep_strongest_retry_after(self):
        router = self.scheduler()
        self.reserve(router)
        def seed(db, stamp):
            state = router._state(db, router.youtube_pacing.session_scope)
            state.update(strikes=3, last_limit_at=stamp-4000)
            router._save(db, router.youtube_pacing.session_scope, state)
        router.transaction_call(seed)
        error = YouTubeAccountLimited('Your account has been rate-limited')
        self.feedback(router, success=False, error=error)
        self.assertEqual(error.retry_after, 21600)
        self.assertEqual(self.state(router)['cooldown_until'], self.state(router, True)['cooldown_until'])

    def test_newer_legacy_limit_is_adopted_without_network(self):
        router = self.scheduler()
        self.reserve(router)
        self.now += 100
        def seed(db, stamp):
            scope = 'quota:' + router.channels[0].quota_group
            router._state(db, scope)
            router._save(db, scope, dict(last_throttle_kind='account', last_throttle_at=stamp,
                                        cooldown_until=stamp+4000))
        router.transaction_call(seed)
        self.assertEqual(self.reserve(router)[0], 4000)
        self.assertEqual(self.state(router)['interval'], 120)

    def test_timeout_slows_but_content_and_local_errors_do_not(self):
        router = self.scheduler()
        self.reserve(router)
        for error in (PipelineBusy('capacity'),):
            self.feedback(router, success=False, error=error)
        self.assertEqual(self.state(router)['interval'], 60)
        self.feedback(router, success=False, error=TimeoutError('connection timed out'))
        self.assertEqual(self.state(router)['interval'], 90)
        self.assertEqual(summary(self.config, self.state(router), self.now)['interval_reason_code'], 'transient_error')
        self.assertGreater(self.reserve(router)[0], 0)

    def test_summary_explains_legacy_account_backoff_separately_from_wait(self):
        state = dict(interval=120, last_limit_at=self.now - 7200,
                     wait_reason='等待下一次视频启动', next_at=self.now + 20)
        before = dict(state)
        value = summary(self.config, state, self.now)
        self.assertEqual((value['minimum_interval'], value['interval']), (45, 120))
        self.assertEqual(value['interval_reason_code'], 'account_limit')
        self.assertIn('账号限流', value['interval_reason'])
        self.assertEqual(value['reason'], state['wait_reason'])
        self.assertEqual(state, before)  # Reading the UI must not reset backoff.

    def test_summary_distinguishes_initial_state_and_saved_minimum(self):
        initial = summary(self.config, {}, self.now)
        self.assertFalse(initial['initialized'])
        self.assertEqual(initial['interval'], 60)
        floor = summary({**self.config, 'youtube_start_interval': 180},
                        dict(interval=120, interval_reason_code='account_limit'), self.now)
        self.assertEqual(floor['interval'], 180)
        self.assertEqual(floor['interval_reason_code'], 'configured_minimum')

    def test_stable_recovery_explanation_replaces_prior_limit_reason(self):
        router = self.scheduler()
        self.reserve(router)
        self.feedback(router, success=False, error=YouTubeAccountLimited('Your account has been rate-limited'))
        self.assertEqual(summary(self.config, self.state(router), self.now)['interval_reason_code'], 'account_limit')
        self.now += 6 * 3600
        for _ in range(100):
            self.feedback(router, success=True, size=100)
        value = summary(self.config, self.state(router), self.now)
        self.assertEqual(value['interval'], 115)
        self.assertEqual(value['interval_reason_code'], 'stable_success')

    def test_invalid_cookie_is_auth_failure_and_settings_are_validated(self):
        with self.assertRaises(YouTubeAuthRequired):
            self.scheduler(youtube_cookie='missing.txt')
        for field in ('youtube_hourly_start_limit', 'youtube_daily_start_limit'):
            self.assertIn(field, DOWNLOAD_FIELDS)
            with self.assertRaises(ValueError):
                validate_settings({field: 0})
        self.assertEqual(validate_settings({'youtube_start_interval': 120})['youtube_start_interval'], 120)
        with self.assertRaises(ValueError):
            validate_settings({'youtube_pacing_mode': 'invalid'})
        with self.assertRaisesRegex(ValueError, '逐个视频'):
            validate_method_urls(self.config, ['https://www.youtube.com/playlist?list=fixture'])

    def test_adaptive_manual_transfers_use_separate_extraction_and_stop_on_account_limit(self):
        import yt_dlp
        from web_worker import execute
        payload = dict(base=str(self.root), urls=['https://youtu.be/00000000001'],
                       config={**self.config, 'path': 'videos', 'throughput_mode': 'manual'})
        with patch.object(yt_dlp.YoutubeDL, 'extract_info', return_value={'id': '00000000001', 'title': 'fixture', 'ext': 'mp4'}) as extract, \
                patch.object(yt_dlp.YoutubeDL, 'process_ie_result') as transfer, \
                patch.object(yt_dlp.YoutubeDL, 'download') as legacy:
            execute('download', payload)
        extract.assert_called_once()
        transfer.assert_called_once()
        legacy.assert_not_called()
        def fail(downloader, *args, **kwargs):
            downloader.report_error('Your account has been rate-limited by YouTube for up to an hour.')
        with patch.object(yt_dlp.YoutubeDL, 'extract_info', autospec=True, side_effect=fail) as request, \
                patch.object(yt_dlp.YoutubeDL, 'process_ie_result') as transfer:
            with self.assertRaises(YouTubeAccountLimited):
                execute('download', payload)
        request.assert_called_once()
        transfer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
