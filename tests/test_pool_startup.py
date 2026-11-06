"""Pool startup must not turn one account's cooldown into an instance outage."""
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests import test_cookie_pool
from webapp.database import download_permits
from webapp.downloads import YouTubeAccountLimited
from webapp.fleet_routes import check_instance
from webapp.settings import DEFAULTS
from webapp.throughput import PipelineBusy
from webapp.youtube_auth import ProbeResult, YouTubeAuth


class PoolStartupTests(unittest.TestCase):
    setUp = test_cookie_pool.CookiePoolTests.setUp
    pool = test_cookie_pool.CookiePoolTests.pool

    def startup(self, config=None):
        config = {**DEFAULTS, **(config or self.config), 'proxy_mode': 'direct', 'min_free_gb': 0}
        fleet = SimpleNamespace(store=self.store, id='fixture',
            local_settings=lambda: {'config': config, 'base': str(self.base)})
        with patch('webapp.environment.require_native_environment'):
            return check_instance(SimpleNamespace(extensions={}), fleet, {})

    def test_selected_cooling_account_does_not_block_healthy_pool_member(self):
        pool = self.pool()
        identity, path, scope = pool.accounts[0]
        pool.outcome(scope, YouTubeAccountLimited('limited'))
        before = pool.status()['accounts'][0]
        config = {**self.config, 'youtube_cookie': str(path)}
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'fixture')) as probe:
            value, status = self.startup(config)
        self.assertEqual(status, 200, value)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.args[0], self.profiles.require(self.ids[1]).read_bytes())
        self.assertEqual(config['youtube_cookie'], str(path))
        after = pool.status()['accounts'][0]
        for field in ('cooldown_until', 'limited_count', 'recovery_mode', 'interval'):
            self.assertEqual(before[field], after[field])
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(download_permits)).all(), [])

    def test_failed_login_of_first_available_member_does_not_hide_valid_member(self):
        self.pool()
        with patch('webapp.youtube_auth.probe', side_effect=[
                ('not_logged_in', 'fixture rejected'), ('valid', 'fixture accepted')]) as probe:
            result = YouTubeAuth(self.store).check_startup(self.config, self.base, force=True)
        self.assertEqual(result['state'], 'valid')
        self.assertEqual(result['profile_id'], self.ids[1])
        self.assertEqual(probe.call_count, 2)

    def test_all_cooling_starts_in_waiting_state_without_network_or_resetting_state(self):
        pool = self.pool()
        for _, _, scope in pool.accounts:
            pool.outcome(scope, YouTubeAccountLimited('limited'))
        before = pool.status()
        with patch('webapp.youtube_auth.probe') as probe, \
                patch.object(YouTubeAuth, 'clear_block') as clear:
            value, status = self.startup()
            result = YouTubeAuth(self.store).check_startup(self.config, self.base, force=True)
        self.assertEqual(status, 200, value)
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(result['cooldown_until'], min(a['cooldown_until'] for a in before['accounts']))
        probe.assert_not_called()
        clear.assert_not_called()
        self.assertEqual(pool.status()['accounts'], before['accounts'])
        with self.assertRaises(PipelineBusy):
            with pool.assignment():
                self.fail('Startup bypassed cooldown')
        pool.router.clock = lambda db: max(a['cooldown_until'] for a in before['accounts']) + 1
        with pool.assignment() as selected:
            self.assertIn(selected['_cookie_pool_selected'], self.ids)
            session = next(s for s in pool.status()['session_states']
                           if s['id'] == selected['_cookie_pool_selected'])
            self.assertEqual(session['state'], 'recovery')
            self.assertEqual(session['limit'], 1)
            self.assertGreaterEqual(session['interval'], 180)

    def test_manual_pool_halt_still_blocks_startup_without_login_requests(self):
        pool = self.pool()
        def halt(db, stamp):
            state = pool.router._state(db, pool.global_scope)
            state.update(halted=True, halted_at=stamp, halt_kind='verification_required')
            pool.router._save(db, pool.global_scope, state)
        pool.router.transaction_call(halt)
        with patch('webapp.youtube_auth.probe') as probe:
            value, status = self.startup()
        self.assertEqual(status, 409, value)
        self.assertIn('额外验证', value['error'])
        probe.assert_not_called()
        self.assertTrue(pool.status()['halted'])

    def test_pool_wide_cooldown_waits_even_when_member_is_not_cooling(self):
        pool = self.pool()
        until = time.time() + 600
        def cool(db, stamp):
            state = pool.router._state(db, pool.global_scope)
            state['cooldown_until'] = until
            pool.router._save(db, pool.global_scope, state)
        pool.router.transaction_call(cool)
        with patch('webapp.youtube_auth.probe') as probe:
            value, status = self.startup()
            result = YouTubeAuth(self.store).check_startup(self.config, self.base)
        self.assertEqual(status, 200, value)
        self.assertEqual(result['cooldown_until'], until)
        probe.assert_not_called()

    def test_racing_cooldown_skips_account_and_checks_next_member(self):
        pool = self.pool()
        auth = YouTubeAuth(self.store)
        check = auth.check_profile
        def race(identity, *args, **kwargs):
            if identity == self.ids[0]:
                pool.outcome(pool.accounts[0][2], YouTubeAccountLimited('limited'))
            return check(identity, *args, **kwargs)
        with patch.object(auth, 'check_profile', side_effect=race), \
                patch('webapp.youtube_auth.probe', return_value=('valid', 'fixture')) as probe:
            result = auth.check_startup(self.config, self.base, force=True)
        self.assertEqual(result['profile_id'], self.ids[1])
        self.assertEqual(probe.call_count, 1)

    def test_no_verified_available_account_still_rejects_startup(self):
        with patch('webapp.youtube_auth.probe', return_value=('not_logged_in', 'fixture rejected')):
            value, status = self.startup()
        self.assertEqual(status, 409, value)

    def test_manual_login_check_still_refuses_the_cooling_account(self):
        pool = self.pool()
        pool.outcome(pool.accounts[0][2], YouTubeAccountLimited('limited'))
        with patch('webapp.youtube_auth.probe') as probe:
            with self.assertRaises(PipelineBusy):
                YouTubeAuth(self.store).check_profile(self.ids[0], self.config, self.base, force=True)
        probe.assert_not_called()

    def test_login_429_cools_each_account_then_allows_waiting_startup(self):
        with patch('webapp.youtube_auth.probe', return_value=ProbeResult(
                'rate_limited', 'fixture 429', http_status=429, retry_after=600)) as probe:
            value, status = self.startup()
        self.assertEqual(status, 200, value)
        self.assertEqual(probe.call_count, len(self.ids))
        self.assertTrue(all(s['state'] == 'cooldown' for s in self.pool().status()['session_states']))

    def test_duplicate_credentials_are_checked_once(self):
        duplicate = self.profiles.add('copy', self.profiles.require(self.ids[0]).read_bytes())['id']
        config = {**self.config, 'youtube_cookie_pool': [self.ids[0], duplicate]}
        with patch('webapp.youtube_auth.probe', return_value=('not_logged_in', 'fixture rejected')) as probe:
            result = YouTubeAuth(self.store).check_startup(config, self.base, force=True)
        self.assertEqual(result['state'], 'not_logged_in')
        self.assertEqual(probe.call_count, 1)

    def test_single_cookie_startup_keeps_existing_check_behavior(self):
        auth = YouTubeAuth(self.store)
        result = {'state': 'not_logged_in', 'message': 'fixture'}
        config = {**self.config, 'youtube_cookie_pool': []}
        with patch.object(auth, 'check', return_value=result) as check:
            self.assertIs(auth.check_startup(config, self.base, force=True), result)
        check.assert_called_once_with(config, self.base, force=True)


if __name__ == '__main__':
    unittest.main()
