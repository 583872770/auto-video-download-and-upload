"""Read-only recovery explanations must agree with actual success settlement."""
from copy import deepcopy
import unittest

from tests import test_cookie_pool
from webapp.cookie_policy import account_interval, advance_account_interval, pacing_progress
from webapp.youtube_cookie_history import list_events


class PacingProgressTests(unittest.TestCase):
    def test_stable_settlement_stops_at_configured_floor_across_admissions(self):
        for minimum in (30, 55, 120, 450):
            with self.subTest(minimum=minimum):
                config = {'youtube_start_interval': minimum}
                state = dict(interval=minimum, streak=25, successes=25, first_success=1)
                for stamp in (10000, 10100, 10200):
                    state['interval'] = account_interval(config, state)
                    before = deepcopy(state)
                    expected = pacing_progress(config, state, stamp)['next_interval']
                    self.assertEqual(state, before)
                    advance_account_interval(config, state, stamp)
                    self.assertEqual((state['interval'], expected), (minimum, minimum))

    def test_recovery_requires_both_gates_and_does_not_shorten_interval(self):
        state = dict(interval=450, recovery_mode=True, recovery_started_at=1000,
                     recovery_successes=10, recovery_interval_floor=120)
        before = deepcopy(state)
        early = pacing_progress({'youtube_start_interval': 120}, state, 4599)
        self.assertEqual((early['successes_remaining'], early['seconds_remaining']), (0, 1))
        self.assertEqual((early['next_interval'], early['step']), (450, 0))
        state['recovery_successes'] = 9
        late = pacing_progress({}, state, 5000)
        self.assertEqual((late['successes_remaining'], late['seconds_remaining']), (1, 0))
        state['recovery_successes'] = 10
        self.assertEqual(state, before)  # Rendering did not mutate the scheduler.

    def test_cooldown_without_admission_has_no_observation_deadline(self):
        state = dict(interval=300, recovery_mode=True, cooldown_until=5000,
                     recovery_started_at=0, recovery_successes=0)
        for stamp in (4000, 6000):
            value = pacing_progress({}, state, stamp)
            self.assertEqual((value['observed_since'], value['observation_until']), (0, 0))
            self.assertEqual((value['successes_remaining'], value['seconds_remaining']), (10, 3600))

    def test_stable_prediction_includes_success_about_to_settle(self):
        state = dict(interval=450, streak=4, first_success=1000, successes=18)
        value = pacing_progress({'youtube_start_interval': 120}, state, 2800)
        self.assertEqual((value['successes_remaining'], value['next_interval'], value['step']), (1, 445, 5))
        state['successes'] = 19
        value = pacing_progress({'youtube_start_interval': 120}, state, 2800)
        self.assertEqual((value['next_interval'], value['step']), (435, 15))

    def test_configuration_and_post_limit_floor_hide_internal_decreases(self):
        state = dict(interval=125, streak=20, successes=20, first_success=1000,
                     recovery_interval_floor=120)
        value = pacing_progress({'youtube_start_interval': 450}, state, 3000)
        self.assertEqual((value['interval'], value['saved_interval'], value['next_interval']), (450, 125, 450))
        value = pacing_progress({'youtube_start_interval': 60}, state, 3000)
        self.assertEqual(value['next_interval'], 120)


class PoolProgressIntegrationTests(unittest.TestCase):
    setUp = test_cookie_pool.CookiePoolTests.setUp
    pool = test_cookie_pool.CookiePoolTests.pool

    def save(self, pool, scope, **values):
        def update(db, stamp):
            state = pool.router._state(db, scope)
            state.update(values)
            pool.router._save(db, scope, state)
        pool.router.transaction_call(update)

    def test_successful_admissions_at_floor_do_not_repeat_interval_events(self):
        pool = self.pool({**self.config, 'youtube_start_interval': 55,
                          'youtube_cookie_pool_start_interval': 0})
        now = 10000
        pool.router.clock = lambda db: now
        identity, _, scope = pool.accounts[0]
        self.save(pool, scope, interval=55, streak=25, successes=25, first_success=1)
        for _ in range(3):
            with pool.assignment(profile_id=identity) as selected:
                test_cookie_pool.CookiePoolTests.request_source(self, pool, selected)
            view = next(s for s in pool.status()['session_states'] if s['id'] == identity)
            self.assertEqual((view['interval'], view['pacing']['saved_interval']), (55, 55))
            now += 100
        self.assertEqual(list_events(self.store, identity, event_type='interval_change'), [])

    def test_real_reduction_records_once_and_normalization_below_floor_is_silent(self):
        pool = self.pool({**self.config, 'youtube_start_interval': 55})
        now = 10000
        pool.router.clock = lambda db: now
        identity, _, scope = pool.accounts[0]
        self.save(pool, scope, interval=65, streak=25, successes=25, first_success=1)
        expected = next(s for s in pool.status()['session_states'] if s['id'] == identity)['pacing']['next_interval']
        pool.outcome(scope, None)
        pool.outcome(scope, None)
        events = list_events(self.store, identity, event_type='interval_change')
        self.assertEqual([(e['previous_interval'], e['new_interval']) for e in events], [(65, expected)])
        self.assertEqual(expected, 55)
        self.save(pool, scope, interval=40)
        pool.outcome(scope, None)
        self.assertEqual(len(list_events(self.store, identity, event_type='interval_change')), 1)
        view = next(s for s in pool.status()['session_states'] if s['id'] == identity)
        self.assertEqual((view['interval'], view['pacing']['saved_interval']), (55, 55))

    def test_status_keeps_recorded_interval_separate_from_configuration(self):
        pool = self.pool({**self.config, 'youtube_start_interval': 450})
        identity, _, scope = pool.accounts[0]
        self.save(pool, scope, interval=120)
        status = pool.status()
        session = next(s for s in status['session_states'] if s['id'] == identity)
        self.assertEqual(session['interval'], 450)
        self.assertEqual(session['pacing']['saved_interval'], 120)
        self.assertEqual(session['pacing']['next_interval'], 450)
        public = next(s for s in status['capacity_summary']['sessions'] if s['id'] == identity)
        self.assertEqual(public['pacing'], session['pacing'])

    def test_prediction_matches_exit_then_next_success_reduction(self):
        pool = self.pool({**self.config, 'youtube_start_interval': 120, 'youtube_cookie_session_limit': 4})
        now = 10000
        pool.router.clock = lambda db: now
        identity, _, scope = pool.accounts[0]
        self.save(pool, scope, interval=450, recovery_mode=True, recovery_started_at=6400,
                  recovery_successes=9, recovery_interval_floor=120, successes=19)

        def view():
            return next(s for s in pool.status()['session_states'] if s['id'] == identity)

        self.assertEqual(view()['pacing']['successes_remaining'], 1)
        expected = view()['pacing']['next_interval']
        pool.outcome(scope, None)
        self.assertEqual((view()['state'], view()['limit'], view()['interval']), ('ready', 4, expected))
        self.assertEqual(view()['interval'], 450)
        expected = view()['pacing']['next_interval']
        pool.outcome(scope, None)
        self.assertEqual(view()['interval'], expected)
        self.assertEqual(expected, 435)


if __name__ == '__main__':
    unittest.main()
