"""Fenced account admission, duplicate credentials and conservative feedback."""
from pathlib import Path
from contextlib import ExitStack
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
        self.config = {'download_method': 'yt_dlp', 'youtube_cookie_pool': self.ids,
                       'youtube_start_interval': 60, 'worker_count': 3}

    def pool(self, config=None):
        task = self.store.create('download', {'base': str(self.base), 'urls': ['https://youtu.be/00000000001']})
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status='running', worker_id=self.store.owner, lease_until=time.time() + 86400))
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

    def test_configured_shared_spacing_controls_admission_and_start_hint(self):
        config = {**self.config, 'youtube_cookie_pool_start_interval': 3}
        first, second = self.pool(config), self.pool(config)
        stamp = time.time()
        first.router.clock = second.router.clock = lambda db: stamp
        with first.assignment(count_success=False) as selected:
            self.assertEqual(selected['_cookie_pool_next_at'], stamp + 3)
            gate = first.router.transaction_call(lambda db, now: first.router._state(db, first.global_scope))
            self.assertEqual(gate['next_at'], stamp + 3)
            stamp += 2
            with self.assertRaises(PipelineBusy):
                with second.assignment(count_success=False):
                    self.fail('Shared interval bypassed')
            stamp += 1
            with second.assignment(count_success=False) as other:
                self.assertNotEqual(other['_cookie_pool_selected'], selected['_cookie_pool_selected'])

    def test_zero_shared_spacing_preserves_account_spacing_and_cooldown(self):
        config = {**self.config, 'youtube_cookie_pool_start_interval': 0}
        first, second = self.pool(config), self.pool(config)
        stamp = time.time()
        first.router.clock = second.router.clock = lambda db: stamp
        with first.assignment(count_success=False) as selected:
            identity = selected['_cookie_pool_selected']
            self.assertEqual(selected['_cookie_pool_next_at'], stamp)
            with second.assignment(count_success=False) as other:
                self.assertNotEqual(other['_cookie_pool_selected'], identity)
        with self.assertRaises(PipelineBusy):
            with second.assignment(profile_id=identity, count_success=False):
                self.fail('Account interval bypassed')
        scope = next(scope for account, _, scope in first.accounts if account == identity)
        first.outcome(scope, YouTubeAccountLimited('Your account has been rate-limited'))
        stamp += 120
        with self.assertRaises(PipelineBusy):
            with second.assignment(profile_id=identity, count_success=False):
                self.fail('Account cooldown bypassed')

    def test_reducing_shared_interval_preserves_reserved_deadline(self):
        first = self.pool({**self.config, 'youtube_cookie_pool_start_interval': 10})
        second = self.pool({**self.config, 'youtube_cookie_pool_start_interval': 0})
        stamp = time.time()
        first.router.clock = second.router.clock = lambda db: stamp
        with first.assignment(count_success=False):
            pass
        stamp += 3
        with self.assertRaises(PipelineBusy):
            with second.assignment(count_success=False):
                self.fail('Saved configuration erased the reserved window')
        stamp += 7
        with second.assignment(count_success=False) as selected:
            self.assertEqual(selected['_cookie_pool_next_at'], stamp)


    def test_duplicate_credentials_share_one_account(self):
        duplicate = self.profiles.add('copy', self.profiles.require(self.ids[0]).read_bytes() +
            b'.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tDIFFERENT_AUXILIARY_COOKIE\n')['id']
        pool = self.pool({**self.config, 'worker_count': 1, 'youtube_cookie_pool': [self.ids[0], duplicate]})
        self.assertEqual(len(pool.accounts), 1)
        self.assertEqual(pool.status()['effective_accounts'], 1)
        self.assertEqual(next(a for a in pool.status()['accounts'] if a['id']==duplicate)['shared_with'], self.ids[0])
        with pool.assignment():
            self.ungate(pool, accounts=True)
            second = self.pool({**self.config, 'worker_count': 1, 'youtube_cookie_pool': [duplicate]})
            with self.assertRaises(PipelineBusy):
                with second.assignment():
                    self.fail('Duplicate Cookie bypassed per-account cap')

    def test_public_profile_snapshot_contains_safe_idle_evidence_for_each_cookie(self):
        duplicate = self.profiles.add('copy', self.profiles.require(self.ids[0]).read_bytes())['id']
        pool = self.pool({**self.config, 'youtube_cookie_pool': self.ids + [duplicate]})
        with pool.assignment() as selected:
            status = pool.status()
            public = status['capacity_summary']['profiles']
            self.assertEqual(len(public), 4)
            account = next(a for a in public if a['id'] == selected['_cookie_pool_selected'])
            self.assertEqual(account['active'], 1)
            self.assertGreater(account['next_start_at'], time.time())
            self.assertEqual(next(a for a in public if a['id'] == duplicate)['shared_with'], self.ids[0])
            for value in public:
                self.assertFalse({'policies', 'denied_tasks', 'credential', 'content', 'cookies'} & value.keys())

    def test_pool_ignores_instance_concurrency_and_mode_but_preserves_warmup(self):
        config = {**self.config, 'youtube_cookie_pool': self.ids[:2],
                  'worker_count': 1, 'throughput_mode': 'auto', 'youtube_cookie_session_limit': 3}
        pool = self.pool(config)
        before = pool.status()
        self.assertEqual(before['session_limit'], 3)
        self.assertEqual(before['capacity_summary']['capacity'], 2)
        self.assertEqual([a['limit'] for a in before['accounts']], [1, 1])
        def warm(db, stamp):
            for _, _, scope in pool.accounts:
                state = pool.router._state(db, scope)
                state.update(limit=2, streak=5, first_success=stamp - 601)
                pool.router._save(db, scope, state)
        pool.router.transaction_call(warm)
        for mode in ('auto', 'manual'):
            for workers in (1, 64):
                status = self.pool({**config, 'worker_count': workers, 'throughput_mode': mode}).status()
                self.assertEqual(status['capacity_summary']['capacity'], 6)
                self.assertEqual([a['limit'] for a in status['accounts']], [3, 3])
        with ExitStack() as stack:
            for _ in range(6):
                stack.enter_context(self.pool(config).assignment())
                self.ungate(pool, accounts=True)
            self.assertEqual(sorted(a['active'] for a in pool.status()['accounts']), [3, 3])
            with self.assertRaises(PipelineBusy):
                with self.pool(config).assignment():
                    self.fail('Exceeded pool capacity')
        self.assertEqual(sum(a['active'] for a in pool.status()['accounts']), 0)
        with self.store.connect() as db:
            self.assertEqual(len(db.execute(select(download_permits)).all()), 0)

    def test_configured_session_limit_applies_after_warmup_independent_of_instance_total(self):
        config = {**self.config, 'worker_count': 6, 'youtube_cookie_session_limit': 4}
        pool = self.pool(config)
        def warmed(db, stamp):
            for _, _, scope in pool.accounts:
                state = pool.router._state(db, scope)
                state['limit'] = 2  # Persisted warmup marker from existing deployments.
                pool.router._save(db, scope, state)
        pool.router.transaction_call(warmed)
        status = pool.status()
        self.assertEqual([a['limit'] for a in status['accounts']], [4, 4, 4])
        self.assertEqual(status['capacity_summary']['capacity'], 12)
        reduced = self.pool({**config, 'worker_count': 2}).status()
        self.assertEqual([a['limit'] for a in reduced['accounts']], [4, 4, 4])
        self.assertEqual(reduced['capacity_summary']['capacity'], 12)

    def test_cooling_session_lowers_pool_capacity_without_inflating_healthy_sessions(self):
        config = {**self.config, 'worker_count': 6, 'youtube_cookie_session_limit': 1}
        pool = self.pool(config)
        pool.outcome(pool.accounts[0][2], RuntimeError('Read timed out'))
        status = pool.status()
        self.assertFalse(status['halted'])
        self.assertEqual(status['capacity_summary']['capacity'], 2)
        self.assertEqual([a['limit'] for a in status['accounts'][1:]], [1, 1])

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
        self.assertTrue(result['warming'])
        self.assertEqual(result['limit'], 1)
        self.assertEqual(result['interval'], 120)

    def test_account_limit_cools_only_selected_account(self):
        pool = self.pool()
        with self.assertRaises(YouTubeAccountLimited):
            with pool.assignment() as selected:
                limited_id = selected['_cookie_pool_selected']
                raise YouTubeAccountLimited('Your account has been rate-limited')
        status = pool.status()
        self.assertFalse(status['halted'])
        self.assertEqual(status['capacity_summary']['capacity'], 2)
        session = next(s for s in status['session_states'] if s['id'] == limited_id)
        self.assertEqual(session['cooldown_kind'], 'account_rate_limit')
        self.assertGreater(session['cooldown_until'], time.time() + 3500)
        self.ungate(pool)
        with self.pool().assignment() as healthy:
            self.assertNotEqual(healthy['_cookie_pool_selected'], limited_id)

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
        self.assertFalse(status['halted'])
        self.assertIsNone(status['halt_kind'])
        self.assertFalse(status['capacity_summary']['cooling'])
        self.assertEqual(status['capacity_summary']['capacity'], 2)
        session = status['session_states'][0]
        self.assertEqual(session['cooldown_kind'], 'request_rate_limit')
        decision = status['accounts'][0]['decisions'][-1]
        self.assertEqual(decision['failure']['http_status'], 429)
        self.assertFalse(decision['manual'])
        with self.pool().assignment() as healthy:
            self.assertNotEqual(healthy['_cookie_pool_selected'], session['id'])

    def test_content_error_does_not_poison_other_accounts(self):
        pool = self.pool()
        scope = pool.accounts[0][2]
        pool.outcome(scope, RuntimeError('Requested format is not available'))
        self.assertFalse(pool.status()['halted'])
        self.assertNotIn('cooldown_until', pool.status()['accounts'][0])

    def denied(self, pool, number, *, admitted_at=None):
        from webapp.download_policy import failure_evidence
        error = RuntimeError('HTTP Error 403: Forbidden')
        failure_evidence(error, source='youtube', phase='transfer', task_id=f'{number:032x}')
        pool.outcome(pool.accounts[0][2], error, admitted_at=admitted_at)
        return error

    def test_three_distinct_transfer_403s_cool_only_one_session_and_survive_restart(self):
        pool = self.pool()
        now = time.time()
        pool.router.clock = lambda db: now
        for number in (1, 1, 2):
            self.denied(pool, number)
        self.assertEqual(pool.status()['capacity_summary']['cooling_sessions'], 0)
        self.denied(pool, 3)
        status = self.pool().status()
        self.assertFalse(status['halted'])
        self.assertEqual(status['capacity_summary']['capacity'], 2)
        affected = status['session_states'][0]
        self.assertEqual(affected['cooldown_kind'], 'repeated_http_403')
        self.assertEqual(affected['denied_count'], 3)
        self.assertAlmostEqual(affected['cooldown_until'], now + 900)
        self.assertEqual(affected['last_failure_at'], now)
        self.denied(pool, 3)  # Different wrapper/incident, same failed task.
        self.assertEqual(pool.status()['accounts'][0]['denied_cooldowns'], 1)
        with self.pool().assignment() as selected:
            self.assertNotEqual(selected['_cookie_pool_selected'], self.ids[0])

    def test_403_window_success_and_late_results_do_not_spuriously_trip_or_clear(self):
        pool = self.pool()
        now = time.time()
        pool.router.clock = lambda db: now
        self.denied(pool, 1)
        now += 901
        self.denied(pool, 2)
        self.assertEqual(pool.status()['session_states'][0]['denied_count'], 1)
        pool.outcome(pool.accounts[0][2], None, admitted_at=now + 1)
        self.denied(pool, 3)
        self.denied(pool, 4)
        self.assertEqual(pool.status()['capacity_summary']['cooling_sessions'], 0)
        self.denied(pool, 5)
        protected = pool.status()['accounts'][0]
        pool.outcome(pool.accounts[0][2], None, admitted_at=now - 1)
        self.assertEqual(pool.status()['accounts'][0], protected)
        self.denied(pool, 6, admitted_at=now - 1)
        after = pool.status()['accounts'][0]
        self.assertEqual(after['cooldown_until'], protected['cooldown_until'])
        self.assertEqual(after['denied_cooldowns'], 1)

    def test_recovery_403_extends_pause_and_never_changes_other_sessions(self):
        pool = self.pool()
        now = time.time()
        pool.router.clock = lambda db: now
        for number in (1, 2, 3):
            self.denied(pool, number)
        healthy = pool.status()['accounts'][1:]
        for number, wait in ((4, 1800), (5, 3600), (6, 3600)):
            now = pool.status()['accounts'][0]['cooldown_until'] + 1
            with pool.assignment(self.ids[0], count_success=False):
                view = pool.status()['session_states'][0]
                self.assertEqual((view['state'], view['limit']), ('recovery', 1))
                self.assertGreaterEqual(view['interval'], 180)
            self.denied(pool, number, admitted_at=now)
            state = pool.status()['accounts'][0]
            self.assertEqual(state['cooldown_until'], now + wait)
            self.assertEqual(state['recovery_successes'], 0)
            self.assertEqual(pool.status()['accounts'][1:], healthy)
        self.assertFalse(pool.status()['halted'])

    def test_403_guard_ignores_other_sources_and_phases(self):
        from webapp.download_policy import failure_evidence
        pool = self.pool()
        for source, phase in (('oss', 'transfer'), ('youtube', 'resolve')):
            for number in range(3):
                error = RuntimeError('HTTP Error 403: Forbidden')
                failure_evidence(error, source=source, phase=phase, task_id=f'{number:032x}')
                pool.outcome(pool.accounts[0][2], error)
        self.assertEqual(pool.status()['capacity_summary']['cooling_sessions'], 0)

    def test_403_protection_respects_longer_server_retry_after_header(self):
        from webapp.download_policy import failure_evidence
        pool = self.pool()
        now = time.time()
        pool.router.clock = lambda db: now
        self.denied(pool, 1)
        self.denied(pool, 2)
        error = RuntimeError('HTTP Error 403: Forbidden')
        error.headers = {'Retry-After': '7200'}
        failure_evidence(error, source='youtube', phase='transfer', task_id=f'{3:032x}')
        pool.outcome(pool.accounts[0][2], error)
        self.assertEqual(pool.status()['accounts'][0]['cooldown_until'], now + 7200)

    def test_preclaim_hint_is_released_at_admission_but_database_gate_still_applies(self):
        from unittest.mock import Mock
        from web_worker import execute
        pool = self.pool({**self.config, 'path': 'videos', 'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        ticket = Mock()
        def work(*args):
            ticket.finish.assert_called_once_with(10)
            with self.assertRaises(PipelineBusy):
                with self.pool().assignment():
                    self.fail('Local pre-claim hint replaced database pacing')
            return {'skipped': True}
        with patch('web_worker._execute', side_effect=work):
            execute('download', {'base': str(self.base), 'config': pool.config,
                'urls': ['https://youtu.be/00000000001']}, store=self.store,
                task_id=pool.router.task_id, start_ticket=ticket)

    def test_all_cooling_waits_and_recovers_after_restart_without_manual_resume(self):
        pool = self.pool()
        now = time.time()
        pool.router.clock = lambda db: now
        for _, _, scope in pool.accounts:
            pool.outcome(scope, YouTubeAccountLimited('limited'))
        snapshot = pool.status()
        self.assertFalse(snapshot['halted'])
        self.assertEqual(snapshot['capacity_summary']['capacity'], 0)
        self.assertEqual(snapshot['capacity_summary']['cooling_sessions'], 3)
        self.assertIn('自动等待', snapshot['capacity_summary']['reason'])
        with self.assertRaises(PipelineBusy):
            with pool.assignment():
                self.fail('Cooling account admitted')
        restarted = self.pool()
        now += 3601
        restarted.router.clock = lambda db: now
        with restarted.assignment() as selected:
            self.assertIn(selected['_cookie_pool_selected'], self.ids)
        self.assertFalse(restarted.status()['halted'])

    def test_automatic_recovery_requires_success_count_and_observation_windows(self):
        pool = self.pool({**self.config, 'youtube_cookie_session_limit': 4})
        now = time.time()
        pool.router.clock = lambda db: now
        identity, _, scope = pool.accounts[0]
        pool.outcome(scope, YouTubeAccountLimited('limited'))
        now += 3601
        first = now
        for offset in (0, 200, 601):
            now = first + offset
            with pool.assignment(identity):
                state = next(s for s in pool.status()['session_states'] if s['id'] == identity)
                self.assertEqual(state['limit'], 1)
                self.assertGreaterEqual(state['interval'], 180)
        state = pool.status()['session_states'][0]
        self.assertEqual((state['state'], state['interval'], state['recovery_successes']), ('recovery', 120, 3))
        now = first + 800
        with pool.assignment(identity):
            pass
        self.assertEqual(pool.status()['session_states'][0]['limit'], 1)
        now = first + 1801
        with pool.assignment(identity):
            pass
        state = pool.status()['session_states'][0]
        self.assertEqual((state['state'], state['limit'], state['interval']), ('ready', 4, 120))
        # Normal success cannot subsequently remove the post-limit floor.
        for _ in range(25):
            now += 150
            pool.outcome(scope, None)
        self.assertEqual(pool.status()['session_states'][0]['interval'], 120)

    def test_late_success_and_transient_error_cannot_skip_recovery(self):
        pool = self.pool()
        now = time.time()
        pool.router.clock = lambda db: now
        identity, _, scope = pool.accounts[0]
        pool.outcome(scope, YouTubeAccountLimited('limited'))
        original = pool.status()['accounts'][0]
        now += 3601
        pool.outcome(scope, None, admitted_at=now - 4000)
        self.assertEqual(pool.status()['accounts'][0], original)
        with pool.assignment(identity):
            pass
        self.assertEqual(pool.status()['accounts'][0]['recovery_successes'], 1)
        now += 200
        pool.outcome(scope, RuntimeError('Read timed out'))
        state = pool.status()['accounts'][0]
        self.assertEqual((state['recovery_successes'], state['recovery_started_at']), (0, 0))
        now += 3601
        pool.outcome(scope, YouTubeAccountLimited('limited again'))
        state = pool.status()['accounts'][0]
        self.assertEqual(state['limited_count'], 2)
        self.assertGreaterEqual(state['cooldown_until'], now + 3600)

    def test_renamed_duplicate_cannot_bypass_account_cooldown_or_login_check(self):
        from webapp.youtube_auth import YouTubeAuth
        pool = self.pool()
        identity, _, scope = pool.accounts[0]
        duplicate = self.profiles.add('Other name', self.profiles.require(identity).read_bytes())['id']
        pool.outcome(scope, YouTubeAccountLimited('limited'))
        config = {**self.config, 'youtube_cookie_pool': [duplicate]}
        renamed = self.pool(config)
        self.assertEqual(renamed.accounts[0][2], scope)
        with self.assertRaises(PipelineBusy):
            with renamed.assignment():
                self.fail('Renamed account bypassed cooldown')
        with patch('webapp.youtube_auth.probe') as probe:
            with self.assertRaises(PipelineBusy):
                YouTubeAuth(self.store).check_profile(duplicate, config, self.base, force=True)
            probe.assert_not_called()

    def test_admitted_task_checks_new_cooldown_before_starting_next_phase(self):
        for pacing_mode in ('manual', 'adaptive'):
            with self.subTest(pacing_mode=pacing_mode):
                pool = self.pool({**self.config, 'youtube_pacing_mode': pacing_mode})
                self.ungate(pool, accounts=True)
                with pool.assignment(count_success=False) as selected:
                    router = CookiePool(self.store, selected, self.base, pool.router.task_id).router
                    # Another in-flight task reports the account limit while
                    # this task is waiting for its next request/transfer phase.
                    pool.outcome(selected['_cookie_pool_scope'], YouTubeAccountLimited('limited'))
                    with self.assertRaises(PipelineBusy):
                        router.wait_youtube_start(router.channels[0])
                    with self.assertRaises(PipelineBusy):
                        with router.permit(router.channels[0], 'resolve', timeout=0):
                            self.fail('Cooling task started resolution')
                    with self.assertRaises(PipelineBusy):
                        with router.permit(router.channels[0], 'transfer', timeout=0):
                            self.fail('Cooling task started a new transfer')

    def test_skipped_video_does_not_count_towards_recovery(self):
        from web_worker import execute
        pool = self.pool({**self.config, 'path': 'videos', 'disk_min_free_gb': 0, 'disk_resume_free_gb': 0})
        def recovering(db, stamp):
            for _, _, scope in pool.accounts:
                state = pool.router._state(db, scope)
                state.update(recovery_mode=True, recovery_started_at=stamp - 2000,
                             recovery_successes=4, interval=180)
                pool.router._save(db, scope, state)
        pool.router.transaction_call(recovering)
        with patch('web_worker._execute', return_value={'skipped': True}):
            execute('download', {'base': str(self.base), 'config': pool.config,
                'urls': ['https://youtu.be/00000000001']}, store=self.store, task_id=pool.router.task_id)
        self.assertTrue(all(a['recovery_successes'] == 4 for a in pool.status()['accounts']))

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

    def test_plain_transfer_timeout_resets_warmup_and_cools_session(self):
        pool = self.pool()
        scope = pool.accounts[0][2]
        pool.outcome(scope, RuntimeError('Read timed out'))
        account = pool.status()['accounts'][0]
        self.assertTrue(account['warming'])
        self.assertGreater(account['cooldown_until'], time.time())
        self.assertEqual(account['interval'], 120)

    def test_recovery_preserves_cooldown_then_restarts_warmup(self):
        pool = self.pool()
        scope = pool.accounts[0][2]
        pool.outcome(scope, YouTubeAccountLimited('Your account has been rate-limited'))
        with self.assertRaises(ValueError):
            pool.clear_halt_after_checks()
        self.assertFalse(pool.status()['halted'])
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
        for bad in (0, 65, True, 1.5, 'invalid'):
            with self.assertRaises(ValueError):
                validate_settings({**self.config, 'youtube_cookie_session_limit': bad})
        self.assertEqual(validate_settings({**self.config, 'youtube_cookie_session_limit': '8'})[
            'youtube_cookie_session_limit'], 8)
        for bad in ([None], ['../secret'], [self.ids[0], self.ids[0]], 'all'):
            with self.assertRaises(ValueError):
                validate_settings({**self.config, 'youtube_cookie_pool': bad})
        with self.assertRaises(ValueError):
            validate_settings({**self.config, 'download_methods': ['yt_dlp', 'yt5s']})


if __name__ == '__main__':
    unittest.main()
