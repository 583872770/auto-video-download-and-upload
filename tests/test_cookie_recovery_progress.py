"""Read-only recovery explanations must agree with actual success settlement."""
from copy import deepcopy
import unittest

from tests import test_cookie_pool
from webapp.cookie_policy import pacing_progress


class PacingProgressTests(unittest.TestCase):
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
