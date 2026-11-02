"""Fenced account admission, duplicate credentials and conservative feedback."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from sqlalchemy import select

from webapp.cookie_pool import CookiePool
from webapp.cookie_profiles import CookieProfiles
from webapp.database import tasks, download_permits, cookie_profiles
from webapp.downloads import YouTubeAccountLimited, DownloadLeaseLost
from webapp.risk_diagnostics import classify
from webapp.settings import validate_settings
from webapp.store import TaskStore
from webapp.throughput import PipelineBusy
from webapp.youtube_auth import YouTubeAuthRequired

COOKIE = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tTEST_SECRET\n'


class CookiePoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = TaskStore(self.base / 'state')
        self.profiles = CookieProfiles(self.base, self.store)
        self.ids = [self.profiles.add(str(i), COOKIE.replace(b'TEST_SECRET', str(i).encode()))['id'] for i in range(3)]
        self.config = {'download_method': 'yt_dlp', 'youtube_cookie_pool': self.ids, 'youtube_start_interval': 60}

    def pool(self, config=None):
        task = self.store.create('download', {'base': str(self.base), 'urls': ['https://youtu.be/00000000001']})
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status='running', worker_id=self.store.owner, lease_until=time.time() + 300))
        return CookiePool(self.store, config or self.config, self.base, task['id'])

    def ungate(self, pool, accounts=False):
        def update(db, stamp):
            state = pool.router._state(db, pool.global_scope)
            state['next_at'] = 0
            pool.router._save(db, pool.global_scope, state)
            if accounts:
                for _, _, scope in pool.accounts:
                    state = pool.router._state(db, scope)
                    state['next_at'] = 0
                    pool.router._save(db, scope, state)
        pool.router.transaction_call(update)

    def test_global_pacing_then_distinct_accounts_and_per_account_cap(self):
        first, second = self.pool(), self.pool()
        with first.assignment() as selected:
            self.assertIn(selected['_cookie_pool_selected'], self.ids)
            with self.assertRaises(PipelineBusy):
                with second.assignment():
                    self.fail('Global pacing bypassed')
            self.ungate(first)
            with second.assignment() as other:
                self.assertNotEqual(other['_cookie_pool_selected'], selected['_cookie_pool_selected'])
        with self.store.connect() as db:
            self.assertEqual(len(db.execute(select(download_permits)).all()), 0)

    def test_duplicate_credentials_share_one_account(self):
        duplicate = self.profiles.add('copy', self.profiles.require(self.ids[0]).read_bytes() +
            b'.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tDIFFERENT_AUXILIARY_COOKIE\n')['id']
        pool = self.pool({**self.config, 'youtube_cookie_pool': [self.ids[0], duplicate]})
        self.assertEqual(len(pool.accounts), 1)
        self.assertEqual(pool.status()['effective_accounts'], 1)
        self.assertEqual(next(a for a in pool.status()['accounts'] if a['id']==duplicate)['shared_with'], self.ids[0])
        with pool.assignment():
            self.ungate(pool, accounts=True)
            second = self.pool({**self.config, 'youtube_cookie_pool': [duplicate]})
            with self.assertRaises(PipelineBusy):
                with second.assignment():
                    self.fail('Duplicate Cookie bypassed per-account cap')

    def test_warmup_requires_success_count_and_elapsed_time(self):
        pool = self.pool()
        scope = pool.accounts[0][2]
        for _ in range(5):
            pool.outcome(scope, None)
        self.assertEqual(pool.status()['accounts'][0].get('limit', 1), 1)
        def age(db, stamp):
            state = pool.router._state(db, scope)
            state['first_success'] = stamp - 601
            pool.router._save(db, scope, state)
        pool.router.transaction_call(age)
        pool.outcome(scope, None)
        self.assertEqual(pool.status()['accounts'][0]['limit'], 2)
        error = RuntimeError('timeout'); error.retryable = True
        pool.outcome(scope, error)
        result = pool.status()['accounts'][0]
        self.assertEqual(result['limit'], 1)
        self.assertEqual(result['interval'], 120)

    def test_limit_halts_all_accounts_and_never_switches_on_failure(self):
        pool = self.pool()
        with self.assertRaises(YouTubeAccountLimited):
            with pool.assignment():
                raise YouTubeAccountLimited('Your account has been rate-limited')
        self.assertTrue(pool.status()['halted'])
        self.assertEqual(pool.status()['halt_kind'], 'account_rate_limit')
        with self.assertRaises(YouTubeAuthRequired):
            with self.pool().assignment():
                self.fail('Pool switched identity after account limit')
        self.assertGreater(max(a.get('cooldown_until', 0) for a in pool.status()['accounts']), time.time() + 3500)

    def test_expired_legacy_halt_is_replayed_without_new_admission_or_throttle(self):
        pool = self.pool()
        old_stamp = time.time() - 86400
        def halt(db, stamp):
            pool.router._state(db, pool.global_scope)
            gate = {'halted': True, 'halted_at': old_stamp, 'cooldown_until': old_stamp + 3600,
                    'reason': '账号池检测到限流或登录验证，已停止整个实例。'}
            pool.router._save(db, pool.global_scope, gate)
        pool.router.transaction_call(halt)
        before = pool.status()
        # Recreate the pool just as toggling the mode or restarting a worker does.
        with patch.object(pool.router, '_admit', side_effect=AssertionError('Unexpected admission')):
            with self.assertRaises(YouTubeAuthRequired) as caught:
                with pool.assignment():
                    self.fail('Historical halt reached the downloader')
        result = caught.exception.result
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(result['source'], 'cookie_pool_halted')
        self.assertEqual(result['halted_at'], old_stamp)
        self.assertIn('本次尚未选择 Cookie', str(caught.exception))
        self.assertEqual(classify(str(caught.exception)), ['cookie_pool_halted'])
        after = self.pool().status()
        self.assertEqual({k: v for k, v in after.items() if k != 'capacity_summary'},
                         {k: v for k, v in before.items() if k != 'capacity_summary'})
        self.assertIsNone(before['halt_kind'])
        self.assertIn('旧记录未区分', before['reason'])
        with self.store.connect() as db:
            self.assertEqual(len(db.execute(select(download_permits)).all()), 0)

    def test_http_429_is_distinct_from_explicit_account_limit(self):
        pool = self.pool()
        error = RuntimeError('HTTP Error 429: Too Many Requests')
        pool.outcome(pool.accounts[0][2], error)
        status = pool.status()
        self.assertEqual(status['halt_kind'], 'http_429')
        self.assertIn('http_429', classify(status['reason']))
        self.assertNotIn('account_rate_limit', classify(status['reason']))

    def test_content_error_does_not_poison_other_accounts(self):
        pool = self.pool()
        scope = pool.accounts[0][2]
        pool.outcome(scope, RuntimeError('Requested format is not available'))
        self.assertFalse(pool.status()['halted'])
        self.assertNotIn('cooldown_until', pool.status()['accounts'][0])

    def test_invalid_login_halts_pool_instead_of_trying_another_account(self):
        pool = self.pool()
        with self.assertRaises(YouTubeAuthRequired):
            with pool.assignment():
                raise YouTubeAuthRequired({'state':'not_logged_in', 'message':'Login rejected'})
        self.assertTrue(pool.status()['halted'])
        self.assertEqual(pool.status()['halt_kind'], 'authentication_required')
        self.assertNotIn('account_rate_limit', classify(pool.status()['reason']))
        with self.assertRaises(YouTubeAuthRequired):
            with self.pool().assignment():
                self.fail('Pool selected another account after invalid login')

    def test_plain_transfer_timeout_reduces_capacity(self):
        pool = self.pool()
        scope = pool.accounts[0][2]
        pool.outcome(scope, RuntimeError('Read timed out'))
        account = pool.status()['accounts'][0]
        self.assertEqual(account['limit'], 1)
        self.assertEqual(account['interval'], 120)

    def test_recovery_preserves_cooldown_then_restarts_warmup(self):
        pool = self.pool()
        scope = pool.accounts[0][2]
        pool.outcome(scope, YouTubeAccountLimited('Your account has been rate-limited'))
        with self.assertRaises(ValueError):
            pool.clear_halt_after_checks()
        self.assertTrue(pool.status()['halted'])
        def expire(db, stamp):
            state = pool.router._state(db, scope)
            state['cooldown_until'] = stamp - 1
            pool.router._save(db, scope, state)
            gate = pool.router._state(db, pool.global_scope)
            gate['cooldown_until'] = stamp - 1
            pool.router._save(db, pool.global_scope, gate)
        pool.router.transaction_call(expire)
        pool.clear_halt_after_checks()
        self.assertFalse(pool.status()['halted'])
        self.assertEqual(pool.status()['accounts'][0]['limit'], 1)

    def test_invalid_file_does_not_break_status_but_stops_execution(self):
        with self.store.connect() as db:
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id == self.ids[0]).values(content='invalid'))
        readonly = CookiePool(self.store, self.config, self.base, None, strict=False)
        self.assertTrue(any(a.get('unavailable') for a in readonly.status()['accounts']))
        with self.assertRaises(YouTubeAuthRequired):
            self.pool()

    def test_expired_task_cannot_admit(self):
        pool = self.pool()
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == pool.router.task_id).values(lease_until=0))
        with self.assertRaises(DownloadLeaseLost):
            with pool.assignment():
                self.fail('Expired owner acquired account')

    def test_validation_rejects_unknown_ids_and_provider_fallback(self):
        validate_settings(self.config)
        for bad in ([None], ['../secret'], [self.ids[0], self.ids[0]], 'all'):
            with self.assertRaises(ValueError):
                validate_settings({**self.config, 'youtube_cookie_pool': bad})
        with self.assertRaises(ValueError):
            validate_settings({**self.config, 'download_methods': ['yt_dlp', 'yt5s']})


if __name__ == '__main__':
    unittest.main()
