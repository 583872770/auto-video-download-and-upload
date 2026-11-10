"""Post-limit stages advance from fresh evidence, never lifetime success totals."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from tests import test_cookie_pool
from webapp.cookie_policy import account_interval, advance_account_interval, pacing_progress
from webapp.downloads import YouTubeAccountLimited
from webapp.throughput import PipelineBusy
from webapp.youtube_cookie_history import list_events


class ProtectionStageTests(unittest.TestCase):
    def setUp(self):
        self.config = {'youtube_start_interval': 50}
        self.state = dict(interval=120, recovery_interval_floor=120, streak=500,
                          successes=500, first_success=1, last_start=9900, next_at=10030)
        self.now = 10000

    def settle(self, *, admitted_at=None):
        return advance_account_interval(self.config, self.state, self.now, admitted_at=admitted_at)

    def view(self):
        return pacing_progress(self.config, self.state, self.now)

    def complete_stage(self):
        since = self.state['protection_stage']['started_at']
        for offset in (1, 2, 3, 4, 900):
            self.now = since + offset
            self.settle(admitted_at=since + .1)

    def test_legacy_history_cannot_satisfy_first_stage_and_render_does_not_start_it(self):
        before = deepcopy(self.state)
        value = self.view()
        self.assertEqual(self.state, before)
        self.assertEqual((value['phase'], value['observed_since'], value['success_count']),
                         ('protection_stage', 0, 0))
        self.settle(admitted_at=self.now - 20)
        self.assertEqual((self.view()['observed_since'], self.view()['success_count']), (10000, 0))
        self.assertEqual(self.state['interval'], 120)
        self.assertEqual((self.state['last_start'], self.state['next_at']), (9900, 10030))

    def test_success_count_without_time_cannot_advance_and_time_alone_does_not_write(self):
        self.settle()
        self.now += 1
        for _ in range(20):
            self.settle(admitted_at=10000)
        self.assertEqual(self.state['interval'], 120)
        self.now = 10900
        before = deepcopy(self.state)
        self.assertEqual(self.view()['seconds_remaining'], 0)
        self.assertEqual(self.state, before)
        self.assertEqual(self.state['interval'], 120)
        self.settle(admitted_at=10000)
        self.assertEqual(self.state['interval'], 105)

    def test_time_without_five_successes_cannot_advance(self):
        self.settle()
        self.now += 900
        for _ in range(4):
            self.settle(admitted_at=10000)
        self.assertEqual(self.state['interval'], 120)
        self.assertEqual(self.view()['successes_remaining'], 1)
        expected = self.view()['next_interval']
        self.settle(admitted_at=10000)
        self.assertEqual(self.state['interval'], expected)
        self.assertEqual((self.view()['success_count'], self.view()['seconds_remaining']), (0, 900))

    def test_every_stage_restarts_gates_and_stops_at_configured_target(self):
        self.settle()
        intervals = [self.state['interval']]
        for expected in (105, 90, 75, 60, 50):
            self.complete_stage()
            intervals.append(self.state['interval'])
            self.assertEqual((self.state['interval'], self.state['recovery_interval_floor']), (expected, expected))
            self.assertEqual(self.view()['success_count'], 0)
            self.assertEqual(self.view()['seconds_remaining'], 900)
        self.assertEqual(intervals, [120, 105, 90, 75, 60, 50])
        self.assertTrue(self.view()['target_reached'])
        self.complete_stage()
        self.assertEqual(account_interval(self.config, self.state), 50)

    def test_long_idle_cannot_catch_up_multiple_stages(self):
        self.settle()
        self.state['protection_stage']['successes'] = 100
        self.now += 86400
        self.settle(admitted_at=10000)
        self.assertEqual(self.state['interval'], 105)
        self.assertEqual(self.view()['successes_remaining'], 5)

    def test_task_admitted_before_step_cannot_contribute_to_new_stage(self):
        self.settle()
        self.complete_stage()
        self.now += 900
        for _ in range(10):
            self.settle(admitted_at=10899)
        self.assertEqual((self.state['interval'], self.view()['success_count']), (105, 0))

    def test_old_inflight_completion_cannot_trigger_ready_stage(self):
        self.settle()
        self.state['protection_stage']['successes'] = 5
        self.now += 900
        self.settle(admitted_at=9999)
        self.assertEqual((self.state['interval'], self.view()['success_count']), (120, 5))
        self.settle(admitted_at=10000)
        self.assertEqual(self.state['interval'], 105)

    def test_target_change_starts_fresh_stage_in_both_directions(self):
        self.settle()
        self.complete_stage()
        self.state['protection_stage']['successes'] = 100
        self.now += 900
        self.config['youtube_start_interval'] = 60
        self.settle(admitted_at=self.now - 100)
        self.assertEqual((self.state['interval'], self.view()['success_count']), (105, 0))
        self.config['youtube_start_interval'] = 110
        self.settle()
        self.assertEqual(account_interval(self.config, self.state), 110)
        self.assertTrue(self.view()['target_reached'])
        self.config['youtube_start_interval'] = 50
        self.settle()
        self.assertEqual((self.state['interval'], self.view()['success_count']), (110, 0))
        self.assertFalse(self.view()['target_reached'])

    def test_arriving_from_high_backoff_starts_fresh_120_stage(self):
        self.state['interval'] = 130
        self.assertEqual(self.view()['phase'], 'stable')
        self.settle()
        self.assertEqual((self.state['interval'], self.view()['success_count']), (120, 0))
        self.assertEqual(self.view()['seconds_remaining'], 900)

    def test_render_and_settlement_agree_on_next_protected_interval(self):
        self.settle()
        self.state['protection_stage']['successes'] = 4
        self.now += 900
        expected = self.view()['next_interval']
        self.settle(admitted_at=10000)
        self.assertEqual(expected, self.view()['interval'])


class ProtectionStageIntegrationTests(unittest.TestCase):
    setUp = test_cookie_pool.CookiePoolTests.setUp
    pool = test_cookie_pool.CookiePoolTests.pool

    def prepare(self):
        self.config.update(youtube_start_interval=50, youtube_task_rest_interval=25)
        self.now = 10000
        pool = self.pool()
        pool.router.clock = lambda db: self.now
        self.identity, _, self.scope = pool.accounts[0]
        def seed(db, stamp):
            state = pool.router._state(db, self.scope)
            state.update(interval=120, recovery_interval_floor=120, first_success=1, streak=500, successes=500)
            pool.router._save(db, self.scope, state)
        pool.router.transaction_call(seed)
        return pool

    def view(self, pool):
        return next(s for s in pool.status()['session_states'] if s['id'] == self.identity)

    def advance(self, pool):
        for offset in (1, 2, 3, 4, 900):
            self.now = 10000 + offset
            pool.outcome(self.scope, None, admitted_at=10000)

    def test_restart_preserves_stage_and_real_admission_honors_new_interval_and_rest(self):
        pool = self.prepare()
        pool.outcome(self.scope, None)
        self.advance(pool)
        self.assertEqual(self.view(pool)['interval'], 105)
        restarted = self.pool()
        restarted.router.clock = lambda db: self.now
        self.assertEqual(self.view(restarted)['pacing']['success_count'], 0)
        self.assertEqual(self.view(restarted)['pacing']['seconds_remaining'], 900)
        def pending(db, stamp):
            state = restarted.router._state(db, self.scope)
            state.update(last_start=self.now - 100, next_at=self.now + 30, last_finished_at=self.now - 10)
            restarted.router._save(db, self.scope, state)
        restarted.router.transaction_call(pending)
        with self.assertRaises(PipelineBusy):
            with restarted.assignment(profile_id=self.identity):
                self.fail('Previously reserved window was cancelled')
        self.now += 30
        with restarted.assignment(profile_id=self.identity, count_success=False):
            self.assertEqual(self.view(restarted)['interval'], 105)
        self.assertEqual([s['interval'] for s in restarted.status()['session_states'] if s['id'] != self.identity], [60, 60])
        events = list_events(self.store, self.identity, event_type='interval_change')
        self.assertEqual((events[0]['previous_interval'], events[0]['new_interval'], events[0]['kind']),
                         (120, 105, 'protection_stage'))

    def test_content_storage_and_local_waits_preserve_stage_but_limit_resets_it(self):
        pool = self.prepare()
        pool.outcome(self.scope, None)
        self.advance(pool)
        before = self.view(pool)['pacing']
        for failure in (RuntimeError('Video has been removed by the uploader'), PipelineBusy('Waiting for local resources')):
            pool.outcome(self.scope, failure)
            self.assertEqual(self.view(pool)['pacing'], before)
        from Utils.oss_storage import OSSUploadError
        pool.outcome(self.scope, OSSUploadError('OSS upload failed'))
        self.assertEqual(self.view(pool)['pacing'], before)
        pool.outcome(self.scope, YouTubeAccountLimited('Your account has been rate-limited'), admitted_at=self.now)
        view = self.view(pool)
        self.assertEqual((view['state'], view['pacing']['phase'], view['pacing']['protection_minimum']),
                         ('cooldown', 'recovery', 120))
        self.assertGreaterEqual(view['interval'], 300)
        self.assertEqual(view['pacing']['success_count'], 0)

    def test_transport_failure_preserves_stage_and_skipped_assignment_does_not_count(self):
        pool = self.prepare()
        pool.outcome(self.scope, None)
        self.advance(pool)
        with pool.assignment(profile_id=self.identity, count_success=False):
            pass  # No source permit or successful file: no stage evidence.
        self.assertEqual(self.view(pool)['pacing']['success_count'], 0)
        before = deepcopy(self.view(pool)['pacing'])
        pool.outcome(self.scope, RuntimeError('Read timed out'))
        view = self.view(pool)
        self.assertEqual(view['pacing'], before)


@unittest.skipUnless(shutil.which('node'), 'Node.js required for UI rendering check')
class ProtectionStageRenderingTests(unittest.TestCase):
    def test_actual_frontend_renders_stage_gates_target_and_legacy_sampling(self):
        state = dict(interval=120, recovery_interval_floor=120,
                     protection_stage=dict(interval=120, target=50, started_at=10000, successes=4))
        active = pacing_progress({'youtube_start_interval': 50}, state, 10899)
        target = pacing_progress({'youtube_start_interval': 120}, state, 10899)
        code = r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const context = vm.createContext({document:{addEventListener(){},getElementById(){return null;}},
  escapeHTML:s=>String(s), concurrencyCountdown:n=>String(n)});
vm.runInContext(fs.readFileSync('static/cookie_recovery.js','utf8'),context);
const render = p=>context.cookieRecoveryProgressHTML({configured_limit:1,pacing:p},{time:10899},true);
const active = render(DATA.active);
assert(active.includes('逐档提速观察'));
assert(active.includes('120 秒降至 105 秒'));
assert(active.includes('还差 1 次'));
assert(active.includes('时间门槛还需 1'));
assert(active.includes('重新计时和计数'));
const target = render(DATA.target);
assert(target.includes('已达到设置目标'));
assert(!target.includes('下一步调整需要同时满足'));
assert(render(DATA.legacy).includes('尚未开始本档观察'));
'''
        legacy = pacing_progress({'youtube_start_interval': 50}, dict(interval=120, recovery_interval_floor=120), 10899)
        code = code.replace('DATA', json.dumps(dict(active=active, target=target, legacy=legacy)))
        result = subprocess.run(['node', '-'], input=code, capture_output=True, text=True,
                                encoding='utf-8', cwd=Path(__file__).resolve().parents[1], timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
