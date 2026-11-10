"""Account protection is isolated from transport faults and channel outages."""
import time
import subprocess
import unittest
from unittest.mock import patch

from tests import test_cookie_pool
from webapp.cookie_pool import CookiePool
from webapp.download_policy import failure_evidence, recovery_policy
from webapp.throughput import PipelineBusy
from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired


class AccountRecoveryPolicyTests(unittest.TestCase):
    setUp = test_cookie_pool.CookiePoolTests.setUp
    pool = test_cookie_pool.CookiePoolTests.pool
    ungate = test_cookie_pool.CookiePoolTests.ungate

    def test_transport_and_server_failures_preserve_all_account_recovery_state(self):
        pool = self.pool()
        scope = pool.accounts[0][2]
        def seed(db, stamp):
            pool.router._state(db, scope)
            pool.router._save(db, scope, dict(interval=105, limit=2, streak=4, successes=30,
                first_success=stamp - 1000, recovery_interval_floor=105,
                protection_stage={'interval': 105, 'target': 50, 'started_at': stamp - 700, 'successes': 4}))
        pool.router.transaction_call(seed)
        before = pool.router.snapshot([scope])[0][scope]
        for message in ('Read timed out', 'Connection reset by peer', 'Did not get any data blocks',
                        '[download] Got error: 215403 bytes read, 9758503 more expected',
                        'HTTP Error 408', 'HTTP Error 500', 'HTTP Error 502', 'HTTP Error 503', 'HTTP Error 504'):
            with self.subTest(message=message):
                error = RuntimeError(message)
                pool.outcome(scope, error)
                self.assertEqual(pool.router.snapshot([scope])[0][scope], before)
                policy = recovery_policy(failure_evidence(error), pool.config)
                self.assertFalse(set(policy.targets) & {'session', 'pool', 'pacing', 'shared'})

    def test_manual_halt_persists_and_requires_fresh_successful_check(self):
        pool = self.pool()
        identity, _, scope = pool.accounts[0]
        auth = YouTubeAuth(self.store)
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'cached login')):
            auth.check_profile(identity, self.config, self.base, force=True)
        pool.outcome(scope, YouTubeAuthRequired({'state': 'verification_required', 'message': 'Login rejected'}))
        manual = pool.status()['session_states'][0]
        self.assertEqual(manual['state'], 'manual')
        pool.outcome(scope, None)
        with patch('webapp.youtube_auth.probe') as probe:
            result = auth.check_profile(identity, self.config, self.base)  # Old positive cache cannot resume.
            self.assertEqual(result['state'], 'valid')
            probe.assert_not_called()
        self.assertEqual(pool.status()['session_states'][0]['state'], 'manual')
        config = {**self.config, '_cookie_pool_selected': identity,
                  'youtube_cookie': str(self.profiles.require(identity))}
        with patch('webapp.youtube_auth.probe') as probe:
            with self.assertRaises(PipelineBusy):
                auth.require(config, self.base, force=True)
            probe.assert_not_called()
        with patch('webapp.youtube_auth.probe', return_value=('network_error', 'Read timed out')):
            auth.check_profile(identity, self.config, self.base, force=True)
        self.assertEqual(pool.status()['session_states'][0]['state'], 'manual')
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'fresh login')):
            result = auth.check_profile(identity, self.config, self.base, force=True)
        self.assertTrue(result['account_resumed'])
        self.assertEqual(pool.status()['session_states'][0]['state'], 'ready')
        self.assertFalse(self.store.downloads_paused())

    def test_probe_cannot_clear_a_newer_halt_or_erase_existing_cooldown(self):
        pool = self.pool()
        identity, _, scope = pool.accounts[0]
        def halt():
            pool.outcome(scope, YouTubeAuthRequired({'state': 'not_logged_in', 'message': 'Login rejected'}))
        halt()
        first = pool.status()['session_states'][0]
        def raced_probe(*args, **kwargs):
            halt()
            return 'valid', 'fresh login'
        with patch('webapp.youtube_auth.probe', side_effect=raced_probe):
            result = YouTubeAuth(self.store).check_profile(identity, self.config, self.base, force=True)
        self.assertFalse(result.get('account_resumed'))
        self.assertEqual(pool.status()['session_states'][0]['state'], 'manual')
        self.assertFalse(pool.clear_account_after_check(identity, first['halt_incident_id'], time.time()))
        def cooldown(db, stamp):
            state = pool.router._state(db, scope)
            state['cooldown_until'] = stamp + 300
            pool.router._save(db, scope, state)
        pool.router.transaction_call(cooldown)
        with patch('webapp.youtube_auth.probe') as probe:
            with self.assertRaises(PipelineBusy):
                YouTubeAuth(self.store).check_profile(identity, self.config, self.base, force=True)
            probe.assert_not_called()
        latest = pool.status()['session_states'][0]
        self.assertTrue(pool.clear_account_after_check(identity, latest['halt_incident_id'], time.time()))
        self.assertEqual(pool.status()['session_states'][0]['state'], 'cooldown')

    def test_all_manual_accounts_wait_without_automatic_probes_or_global_halt(self):
        pool = self.pool()
        for _, _, scope in pool.accounts:
            pool.outcome(scope, YouTubeAuthRequired({'state': 'not_logged_in', 'message': 'Login rejected'}))
        status = pool.status()
        self.assertFalse(status['halted'])
        self.assertEqual(status['capacity_summary']['capacity'], 0)
        self.assertEqual(status['capacity_summary']['available_sessions'], 0)
        self.assertEqual(status['capacity_summary']['manual_sessions'], 3)
        restarted = self.pool()
        with self.assertRaises(PipelineBusy) as waiting:
            with restarted.assignment():
                self.fail('Manually halted account was admitted')
        self.assertEqual(waiting.exception.wait_scope, 'manual_account')
        with patch('webapp.youtube_auth.probe') as probe:
            result = YouTubeAuth(self.store).check_startup(self.config, self.base, force=True)
            self.assertEqual(result['state'], 'waiting')
            probe.assert_not_called()

    def test_account_rejection_does_not_invalidate_other_login_caches(self):
        auth = YouTubeAuth(self.store)
        auth._write('youtube_auth_latest', {'state': 'valid', 'message': 'other account', 'expires_at': time.time()+300})
        auth._write('youtube_auth_generation', 'unchanged')
        auth.reject('Login rejected', profile_id=self.ids[0])
        self.assertEqual(auth.status()['state'], 'valid')
        self.assertEqual(auth._read('youtube_auth_generation'), 'unchanged')

    def test_duplicate_cookie_shares_manual_halt_and_successful_manual_check(self):
        pool = self.pool()
        identity, _, scope = pool.accounts[0]
        duplicate = self.profiles.add('Duplicate', self.profiles.require(identity).read_bytes())['id']
        config = {**self.config, 'youtube_cookie_pool': [identity, duplicate]}
        pool.outcome(scope, YouTubeAuthRequired({'state': 'not_logged_in', 'message': 'Login rejected'}))
        aliased = self.pool(config)
        self.assertEqual(aliased.status()['capacity_summary']['capacity'], 0)
        self.assertEqual({s['state'] for s in aliased.status()['profile_states']}, {'manual'})
        with self.assertRaises(PipelineBusy):
            with aliased.assignment(duplicate):
                self.fail('Duplicate credential bypassed manual halt')
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'fresh login')):
            result = YouTubeAuth(self.store).check_profile(duplicate, config, self.base, recover=True)
        self.assertTrue(result['account_resumed'])
        self.assertEqual({s['state'] for s in aliased.status()['profile_states']}, {'ready'})

    def test_malformed_cookie_does_not_stop_healthy_pool_members(self):
        from web_worker import execute
        from webapp.database import cookie_profiles
        pool = self.pool()
        with self.store.connect() as db:
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id == self.ids[0]).values(content='invalid'))
        with patch('web_worker._execute', return_value={'file_count': 1}) as work:
            execute('download', {'base': str(self.base), 'config': {**self.config, 'path': 'videos', 'min_free_gb': 0},
                                'urls': ['https://youtu.be/00000000001']},
                    store=self.store, task_id=pool.router.task_id)
        self.assertNotEqual(work.call_args.args[1]['config']['_cookie_pool_selected'], self.ids[0])

    def test_worker_keeps_auth_failure_on_original_account_and_runs_healthy_job(self):
        from web_worker import drain
        self.store.set_runtime({**self.config, 'min_free_gb': 0})
        self.store.set_downloads_paused(False)
        jobs = [self.store.create('download', dict(base=str(self.base), urls=[f'https://youtu.be/{i:011d}'],
                config={**self.config, 'path': 'videos', 'min_free_gb': 0})) for i in (1, 2)]
        selected = []
        def work(action, payload, *args, **kwargs):
            identity = payload['config']['_cookie_pool_selected']
            selected.append(identity)
            if len(selected) == 1:
                raise YouTubeAuthRequired({'state': 'not_logged_in', 'message': 'Login rejected'})
            return {'file_count': 1, 'message': 'healthy account completed'}
        with patch('web_worker.TaskStore', return_value=self.store), patch('web_worker._execute', side_effect=work):
            drain(self.store.directory, 0, 'download')
        first, second = [self.store.get(job['id'], private=True) for job in jobs]
        self.assertFalse(self.store.downloads_paused())
        self.assertEqual((first['status'], first['attempts']), ('queued', 1))
        self.assertEqual(first['payload']['_retry_cookie_profile_id'], selected[0])
        self.assertEqual(first['progress']['retry_reason'], 'youtube_session_auth_required')
        self.assertEqual(second['status'], 'succeeded')
        self.assertNotEqual(*selected)
        self.assertEqual(CookiePool(self.store, self.config, self.base, None).status()['capacity_summary']['manual_sessions'], 1)

    def test_server_circuit_requires_three_tasks_and_cools_only_channel_briefly(self):
        now = time.time()
        routers = [self.pool().router for _ in range(3)]
        scope = 'channel:yt_dlp'
        for index, router in enumerate(routers):
            router.clock = lambda db: now
            for _ in range(2):
                router.outcome(router.channels[0], success=False, error=RuntimeError('HTTP Error 503'))
            states = router.snapshot([scope, 'quota:yt_dlp', 'resolver:youtube-origin', 'upstream:youtube-origin'])[0]
            if index < 2:
                self.assertFalse(states[scope].get('cooldown_until'))
            else:
                self.assertEqual(states[scope]['cooldown_until'], now + 30)
                self.assertEqual(states[scope]['cooldown_kind'], 'server_failure')
            self.assertFalse(any(state.get('cooldown_until') for key, state in states.items() if key != scope))
        other = self.pool().router
        other.clock = lambda db: now
        with self.assertRaises(PipelineBusy):
            with other.permit(other.channels[0], 'resolve', timeout=0):
                self.fail('Channel cooldown bypassed')
        now += 31
        with other.permit(other.channels[0], 'resolve', timeout=0):
            pass

    def test_single_server_error_honors_retry_after_without_penalizing_account(self):
        pool = self.pool()
        error = RuntimeError('HTTP Error 503')
        error.headers = {'Retry-After': '90'}
        value = failure_evidence(error)
        policy = recovery_policy(value, self.config)
        self.assertEqual((policy.action, policy.delay, policy.targets), ('retry', 90, ('task', 'channel')))
        pool.router.outcome(pool.router.channels[0], success=False, error=error)
        state = pool.router.snapshot(['channel:yt_dlp'])[0]['channel:yt_dlp']
        self.assertFalse(state.get('cooldown_until'))

    def test_frontend_shows_manual_account_as_waiting_and_explains_recovery(self):
        program = r'''
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
global.document = {getElementById:()=>null,addEventListener:()=>{}};
global.setInterval = ()=>{};
global.escapeHTML = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
vm.runInThisContext(fs.readFileSync('static/risks.js','utf8'));
vm.runInThisContext(fs.readFileSync('static/concurrency.js','utf8'));
vm.runInThisContext(fs.readFileSync('static/cookie_recovery.js','utf8'));
const session = {id:'a',name:'<img src=x>',state:'manual',halt_kind:'authentication_required'};
const pool = {sessions:[session],manual_sessions:1,available_sessions:1};
const html = cookieAccountNoticesHTML(pool,Date.now()/1000);
assert(html.includes('需人工处理'));
assert(html.includes('检测登录'));
assert(!html.includes('低频恢复中'));
assert(!html.includes('<img'));
const status = cookieFilterStates({...session,known:true,reasons:[{code:'account_halted'}],tasks:[]},true);
assert.equal(status.health,'manual');
assert(status.stages.has('waiting'));
assert(!status.stages.has('idle'));
const detail = cookieRecoveryProgressHTML(session,{},true);
assert(detail.includes('新检测通过后恢复该账号'));
assert(!detail.includes('成功进度'));
'''
        result = subprocess.run(['node', '-e', program], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

