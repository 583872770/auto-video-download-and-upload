"""Caption endpoint throttling spans accounts and survives worker restarts."""
from pathlib import Path
import tempfile
import time
import unittest
from urllib.error import HTTPError

from sqlalchemy import select

from webapp.channel_scheduler import ChannelScheduler
from webapp.database import download_permits, tasks
from webapp.download_policy import failure_evidence
from webapp.downloads import DownloadFailure
from webapp.store import TaskStore
from webapp.subtitle_lane import COOLDOWN, INTERVAL, SCOPE, check, failed, slot
from webapp.throughput import PipelineBusy


class SubtitleLaneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = time.time()
        self.url = 'sqlite:///' + (self.root / 'shared.db').as_posix()

    def router(self, account):
        store = TaskStore(self.root / account, self.url)
        self.addCleanup(store.db.engine.dispose)
        task = store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']})
        with store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status='running', worker_id=store.owner, lease_until=self.now + 200000))
        router = ChannelScheduler(store, {'download_method': 'yt_dlp',
            'youtube_cookie_pool': ['a', 'b'], '_cookie_pool_selected': account}, task['id'])
        router.clock = lambda db: self.now
        return router

    def state(self, router, scope=SCOPE):
        return router.snapshot([scope])[0].get(scope, {})

    def test_spacing_persists_across_accounts_and_restarts(self):
        first = self.router('a')
        with slot(first):
            pass
        restarted = self.router('b')
        with self.assertRaises(PipelineBusy) as caught:
            check(restarted)
        self.assertEqual(caught.exception.retry_after, INTERVAL)
        self.assertEqual(caught.exception.start_gate_delay, 0)
        with self.assertRaises(PipelineBusy):
            with slot(restarted):
                self.fail('Another Cookie bypassed the caption interval')
        self.now += INTERVAL + 1
        with slot(restarted):
            pass

    def test_only_one_caption_request_is_admitted_even_after_interval_expires(self):
        first, second = self.router('a'), self.router('b')
        with slot(first):
            self.now += INTERVAL + 1
            with self.assertRaises(PipelineBusy):
                with slot(second):
                    self.fail('Parallel caption request admitted')
        with slot(second):
            pass
        with second.store.connect() as db:
            self.assertEqual(db.execute(select(download_permits)).all(), [])

    def test_429_cools_captions_for_all_accounts_but_leaves_video_capacity_available(self):
        first, second = self.router('a'), self.router('b')
        error = RuntimeError('Unable to download video subtitles for en: HTTP Error 429')
        with self.assertRaises(RuntimeError):
            with slot(first):
                raise error
        state = self.state(first)
        self.assertEqual(state['cooldown_until'], self.now + COOLDOWN)
        self.assertEqual(error.retry_after, COOLDOWN)
        self.assertEqual(failure_evidence(error).phase, 'subtitles')
        with self.assertRaises(PipelineBusy) as waiting:
            check(second)
        self.assertEqual(waiting.exception.retry_after, COOLDOWN)
        with second.permit(second.channels[0], 'transfer', timeout=0):
            pass
        self.assertFalse(self.state(first, 'upstream:youtube-origin').get('cooldown_until'))
        self.assertFalse(self.state(first, 'channel:yt_dlp').get('cooldown_until'))

    def test_repeated_429_increases_backoff_and_retry_after_is_preserved_through_wrappers(self):
        router = self.router('a')
        failed(router, RuntimeError('HTTP Error 429'))
        self.now += COOLDOWN + 1
        error = HTTPError('https://www.youtube.com/api/timedtext', 429, 'Too Many Requests',
                          {'Retry-After': '7200'}, None)
        self.addCleanup(error.close)
        with self.assertRaises(HTTPError):
            with slot(router):
                raise error
        state = self.state(router)
        self.assertEqual(state['throttle_strikes'], 2)
        self.assertEqual(state['cooldown_until'], self.now + 7200)
        wrapped = DownloadFailure().as_error(error)
        wrapped.__cause__ = error
        self.assertEqual(wrapped.retry_after, 7200)
        self.now += 10
        router.outcome(router.channels[0], success=False, error=wrapped, phase='subtitles')
        again = self.state(router)
        self.assertEqual(again['cooldown_until'], state['cooldown_until'])
        self.assertEqual(again['throttle_strikes'], 2)
        self.assertEqual(failure_evidence(wrapped).http_status, 429)

    def test_repeated_limits_double_cooldown_without_a_retry_after_header(self):
        router = self.router('a')
        failed(router, RuntimeError('HTTP Error 429'))
        self.now += COOLDOWN + 1
        failed(router, RuntimeError('HTTP Error 429'))
        self.assertEqual(self.state(router)['cooldown_until'], self.now + COOLDOWN * 2)

    def test_other_failures_do_not_claim_the_caption_endpoint_is_rate_limited(self):
        router = self.router('a')
        for error in (RuntimeError('HTTP Error 403'), RuntimeError('missing subtitles'), PipelineBusy('local wait')):
            failed(router, error)
        self.assertFalse(self.state(router).get('cooldown_until'))
