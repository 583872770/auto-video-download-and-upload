from pathlib import Path
import json
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select

from webapp.channel_scheduler import ChannelScheduler
from webapp.concurrency_diagnostics import diagnose, read_context
from webapp.cookie_pool import CookiePool
from webapp.cookie_profiles import CookieProfiles
from webapp.database import tasks, download_permits
from webapp.download_policy import failure_evidence
from webapp.downloads import YouTubeAccountLimited
from webapp.store import TaskStore
from webapp.throughput import PipelineBusy, policy_key
from webapp.youtube_auth import YouTubeAuthRequired


class DownloadDecisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = TaskStore(self.root / 'state')
        self.addCleanup(self.store.db.engine.dispose)
        self.now = time.time()
        profiles = CookieProfiles(self.root, self.store)
        identities = [profiles.add(str(i), (f'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-{i}\n').encode())['id']
                      for i in range(2)]
        self.config = dict(download_method='yt_dlp', youtube_cookie_pool=identities,
            youtube_cookie=str(profiles.require(identities[0])), youtube_pacing_mode='adaptive',
            youtube_start_interval=45, youtube_throttle_cooldown=300, worker_count=1)

    def pool(self):
        task = self.store.create('download', {'base': str(self.root), 'urls': ['https://youtu.be/00000000001']})
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status='running', worker_id=self.store.owner, lease_until=self.now + 86400))
        pool = CookiePool(self.store, self.config, self.root, task['id'])
        pool.router.clock = lambda db: self.now
        return pool

    def apply_failure(self, error, phase='resolve'):
        pool = self.pool()
        with self.assertRaises(type(error)):
            with pool.assignment() as selected:
                scheduler = ChannelScheduler(self.store, selected, pool.router.task_id, base=self.root)
                scheduler.clock = lambda db: self.now
                scheduler.outcome(scheduler.channels[0], success=False, error=error, phase=phase)
                raise error
        return pool, scheduler

    def state(self, router, scope):
        return router.transaction_call(lambda db, stamp: router._state(db, scope))

    def diagnostic(self, pool):
        summary = pool.status()['capacity_summary']
        rate = dict(time=self.now, policy=policy_key(self.config), transfers=max(1, summary['capacity']), cookie_pool=summary)
        with self.store.connect() as db:
            context = read_context(db, [('local', self.config)], self.now)
        return diagnose(self.config, 'local', [], rate, context, self.now, paused=True)

    def test_attributed_429_does_not_cool_global_resources(self):
        error = RuntimeError('HTTP Error 429: Too Many Requests')
        pool, router = self.apply_failure(error, phase='subtitles')
        self.assertIsNone(router.youtube_pacing)  # The pool owns account budgets in both modes.
        for scope in (pool.global_scope, 'channel:yt_dlp', 'quota:yt_dlp'):
            self.assertFalse(self.state(router, scope).get('cooldown_until'))
        scope = next(scope for _, _, scope in pool.accounts if self.state(router, scope).get('last_decision'))
        state = self.state(router, scope)
        self.assertEqual(state['cooldown_until'], self.now + 300)
        self.assertEqual(state['last_decision']['incident_id'], failure_evidence(error).incident_id)
        diagnostic = self.diagnostic(pool)
        self.assertFalse(diagnostic['decisions']['manual_required'])
        self.assertEqual(len(diagnostic['decisions']['history']), 1)
        self.assertEqual(diagnostic['decisions']['history'][0]['failure']['phase'], 'subtitles')
        self.assertEqual(len(diagnostic['decisions']['history'][0]['effects']), 1)
        self.now += 31
        with self.pool().assignment() as selected:
            self.assertNotEqual(selected['_cookie_pool_selected'], failure_evidence(error).profile_id)
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(download_permits)).all(), [])

    def test_explicit_account_limit_survives_restart_then_recovers_automatically(self):
        error = YouTubeAccountLimited('Your account has been rate-limited')
        pool, router = self.apply_failure(error)
        limited_id = failure_evidence(error).profile_id
        scope = next(scope for i, _, scope in pool.accounts if i == limited_id)
        before = self.state(router, scope)
        self.assertEqual(before['cooldown_until'], self.now + 3600)
        self.assertIsNone(before['last_decision']['failure']['http_status'])
        self.now += 31
        with self.pool().assignment() as selected:
            self.assertNotEqual(selected['_cookie_pool_selected'], limited_id)
        self.now += 3600
        with self.pool().assignment(limited_id):
            recovering = next(s for s in pool.status()['session_states'] if s['id'] == limited_id)
            self.assertEqual((recovering['state'], recovering['limit'], recovering['interval']), ('recovery', 1, 300))
        value = self.diagnostic(pool)
        self.assertFalse(value['decisions']['manual_required'])
        self.assertEqual(len(value['decisions']['history']), 1)

    def test_duplicate_feedback_and_late_success_do_not_erase_backoff(self):
        error = RuntimeError('HTTP Error 429')
        pool, router = self.apply_failure(error)
        before = self.state(router, 'channel:yt_dlp')
        router.outcome(router.channels[0], success=False, error=error)
        self.assertEqual(self.state(router, 'channel:yt_dlp'), before)
        scope = next(scope for _, _, scope in pool.accounts if self.state(router, scope).get('last_decision'))
        account = self.state(router, scope)
        self.now += 1
        router.outcome(router.channels[0], success=True, size=100, elapsed=1)
        pool.outcome(scope, None)
        self.assertFalse(self.state(router, 'channel:yt_dlp').get('cooldown_until'))
        self.assertEqual(self.state(router, scope), account)

    def test_task_diagnostics_use_structured_kind_instead_of_message_guess(self):
        from webapp.risk_diagnostics import task_risks
        failure = failure_evidence(YouTubeAccountLimited('Your account has been rate-limited')).public()
        risk = task_risks(dict(action='download', status='failed', error='HTTP 429 (old outer text)',
                              progress={'last_failure': failure}))
        self.assertIn('account_rate_limit', risk['codes'])
        self.assertNotIn('http_429', risk['codes'])

    def test_execution_halt_is_cleared_by_explicit_resume_without_erasing_history(self):
        from webapp.execution import isolate_download_failure
        error = YouTubeAccountLimited('Your account has been rate-limited')
        isolate_download_failure(self.store, error)
        with self.store.connect() as db:
            context = read_context(db, [('local', self.config)], self.now)
        value = diagnose(self.config, 'local', [], None, context, self.now, paused=True)
        self.assertTrue(value['decisions']['manual_required'])
        incident = value['decisions']['history'][0]['incident_id']
        self.store.set_downloads_paused(False)
        with self.store.connect() as db:
            context = read_context(db, [('local', self.config)], self.now)
        value = diagnose(self.config, 'local', [], None, context, self.now, paused=False)
        self.assertFalse(value['decisions']['manual_required'])
        self.assertEqual(value['decisions']['history'][0]['incident_id'], incident)

    def test_batched_diagnostics_do_not_import_another_instances_local_halt(self):
        states = {'execution:other': {'active': True},
                  'youtube-cookie-pool:other': {'halted': True, 'cooldown_until': self.now + 3600},
                  'youtube-pacing:instance:other': {'cooldown_until': self.now + 3600}}
        value = diagnose(self.config, 'local', [], None, (states, {}, {}), self.now)
        self.assertFalse(value['decisions']['manual_required'])
        self.assertEqual(value['decisions']['restrictions'], [])

    def test_overlapping_waits_take_latest_deadline_instead_of_adding_durations(self):
        from webapp.decision_diagnostics import explain
        channels = [dict(resources=[dict(scope='resolver', group='yt_dlp')])]
        value = explain({'resolver:yt_dlp': {'cooldown_until': self.now + 300}}, channels, None,
                        dict(next_start_at=self.now + 600, reason='滚动预算已用完'), self.now)
        self.assertEqual(value['longest_wait_until'], self.now + 600)

    def test_ordinary_request_limit_never_clears_a_previous_manual_halt(self):
        pool, router = self.apply_failure(YouTubeAuthRequired({'state': 'verification_required', 'message': 'Login rejected'}))
        before = self.state(router, pool.global_scope)
        pool.outcome(pool.accounts[1][2], YouTubeAuthRequired({'state': 'not_logged_in', 'message': 'Login rejected'}))
        after = self.state(router, pool.global_scope)
        self.assertTrue(after['halted'])
        self.assertEqual(after['halt_kind'], 'authentication_required')
        self.assertGreaterEqual(after['cooldown_until'], before['cooldown_until'])

    def test_original_halt_evidence_survives_bounded_recent_history(self):
        error = YouTubeAuthRequired({'state': 'verification_required', 'message': 'Login rejected'})
        pool, router = self.apply_failure(error)
        identity = failure_evidence(error).incident_id
        for _ in range(10):
            self.now += 1
            pool.outcome(pool.accounts[1][2], YouTubeAuthRequired({'state': 'not_logged_in', 'message': 'Login rejected'}))
        gate = self.state(router, pool.global_scope)
        self.assertEqual(len(gate['decisions']), 8)
        value = self.diagnostic(pool)
        self.assertEqual(len(value['decisions']['history']), 8)
        self.assertIn(identity, [r['incident_id'] for r in value['decisions']['history']])

    def test_worker_wrapped_account_limit_still_pauses_and_preserves_task(self):
        from web_worker import drain
        self.store.set_runtime({'min_free_gb': 0})
        self.store.set_downloads_paused(False)
        job = self.store.create('download', dict(base=str(self.root), urls=['https://youtu.be/00000000001'],
            config={'path': 'videos', 'min_free_gb': 0}))
        error = RuntimeError('wrapped extractor failure')
        error.__cause__ = YouTubeAccountLimited('Your account has been rate-limited')
        with patch('web_worker.TaskStore', return_value=self.store), patch('web_worker.execute', side_effect=error):
            drain(self.store.directory, 0, 'download')
        saved = self.store.get(job['id'])
        self.assertTrue(self.store.downloads_paused())
        self.assertEqual((saved['status'], saved['attempts']), ('queued', 0))
        self.assertEqual(saved['progress']['last_failure']['kind'], 'account_rate_limit')
        self.assertIsNone(saved['progress']['last_failure']['http_status'])

    def test_worker_attributed_pool_limit_preserves_task_and_continues_next_job(self):
        from web_worker import drain
        self.store.set_runtime({**self.config, 'min_free_gb': 0})
        self.store.set_downloads_paused(False)
        jobs = [self.store.create('download', dict(base=str(self.root), urls=[url],
            config={**self.config, 'path': 'videos', 'min_free_gb': 0})) for url in
            ('https://youtu.be/00000000001', 'https://youtu.be/00000000002')]
        error = YouTubeAccountLimited('Your account has been rate-limited')
        failure_evidence(error, profile_id=self.config['youtube_cookie_pool'][0], profile_name='Account A')
        with patch('web_worker.TaskStore', return_value=self.store), patch('web_worker.execute',
                side_effect=[error, {'message': 'healthy account completed', 'file_count': 1}]) as execute:
            drain(self.store.directory, 0, 'download')
        first, second = [self.store.get(job['id'], private=True) for job in jobs]
        self.assertFalse(self.store.downloads_paused())
        self.assertEqual((first['status'], first['attempts']), ('queued', 1))
        self.assertGreaterEqual(first['available_at'] - time.time(), 3598)
        self.assertEqual(first['payload']['_retry_cookie_profile_id'], self.config['youtube_cookie_pool'][0])
        self.assertEqual(first['progress']['retry_reason'], 'youtube_session_rate_limit')
        self.assertEqual(first['progress']['last_failure']['profile_name'], 'Account A')
        self.assertEqual(second['status'], 'succeeded')
        self.assertEqual(execute.call_count, 2)

    def test_worker_retries_typed_http_429_without_message_matching(self):
        from web_worker import drain
        self.store.set_runtime({'min_free_gb': 0})
        self.store.set_downloads_paused(False)
        job = self.store.create('download', dict(base=str(self.root), urls=['https://youtu.be/00000000001'],
            config={'path': 'videos', 'min_free_gb': 0}))
        error = RuntimeError('request rejected')
        error.http_status = 429
        error.headers = {'Retry-After': '720'}
        with patch('web_worker.TaskStore', return_value=self.store), patch('web_worker.execute', side_effect=error):
            drain(self.store.directory, 0, 'download')
        saved = self.store.get(job['id'])
        self.assertFalse(self.store.downloads_paused())
        self.assertEqual((saved['status'], saved['attempts']), ('queued', 1))
        self.assertGreaterEqual(saved['available_at'] - time.time(), 718)
        self.assertEqual(saved['progress']['last_failure']['http_status'], 429)

    def test_node_render_explains_legacy_manual_halt_without_fake_http(self):
        from webapp.decision_diagnostics import explain
        result = explain({'youtube-cookie-pool:local': {'halted': True, 'cooldown_until': self.now - 1}},
                         [], None, None, self.now, paused=True)
        self.assertTrue(result['manual_required'])
        self.assertEqual(result['history'], [])
        program = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
global.document = {getElementById:()=>null,addEventListener:()=>{}};
global.setInterval = ()=>{};
global.escapeHTML = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
vm.runInThisContext(fs.readFileSync('static/risks.js','utf8'));
vm.runInThisContext(fs.readFileSync('static/concurrency.js','utf8'));
const data = JSON.parse(process.argv[1]);
const html = decisionExplanationHTML(data, 'main');
assert(html.includes('历史记录缺少完整触发证据'));
assert(html.includes('仍需人工检查'));
assert(!html.includes('HTTP 429'));
const failure = failureEvidenceHTML({kind:'account_rate_limit',phase:'resolve',source:'youtube',http_status:null,evidence:'message',message:'<img src=x onerror=alert(1)>'});
assert(failure.includes('HTTP 状态未记录'));
assert(!failure.includes('<img'));
const pool = {available_sessions:1,cooling_sessions:1,sessions:[{name:'<img src=x onerror=alert(1)>',state:'cooldown',cooldown_kind:'account_rate_limit',cooldown_until:data.time+3600}]};
const accounts = cookieAccountNoticesHTML(pool, data.time);
assert(accounts.includes('其他账号继续调度'));
assert(accounts.includes('300 秒'));
assert(accounts.includes('冷却中不发送下载、字幕或登录检测请求'));
assert(accounts.includes('data-capacity-deadline'));
assert(!accounts.includes('<img'));
pool.available_sessions = 0;
assert(cookieAccountNoticesHTML(pool, data.time).includes('无需手动恢复'));
assert(cookieAccountNoticesHTML(pool, data.time, false).includes('当前状态待确认'));
pool.cooling_sessions = 0;
pool.available_sessions = 1;
pool.sessions[0] = {name:'Account A',state:'recovery',limit:1,interval:300,recovery_successes:2,cooldown_kind:'account_rate_limit'};
assert(cookieAccountNoticesHTML(pool, data.time).includes('本轮有效成功 2 次'));
data.time -= 60;
assert(decisionExplanationHTML(data, 'main').includes('摘要已过期'));
"""
        process = subprocess.run(['node', '-e', program, json.dumps(dict(time=self.now, online=True, decisions=result))],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_node_cookie_status_refresh_preserves_unsaved_pool_selection(self):
        program = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const live = {innerHTML:''}, policy = {textContent:''}, check = {textContent:'检测登录',disabled:false};
const selection = {checked:false}, classes = new Set();
const row = {dataset:{cookieProfileRow:'a'},classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name)},
  querySelector:selector=>({'[data-cookie-session-status]':policy,'[data-cookie-login-check]':check}[selector])};
global.$ = ()=>live;
global.document = {querySelectorAll:selector=>{assert.equal(selector,'[data-cookie-profile-row]'); return [row];}};
global.cookieAccountNoticesHTML = ()=>'<p>Account A cooling</p>';
global.failureKindNames = {account_rate_limit:'明确账号限流'};
const source = fs.readFileSync('static/app.js','utf8');
vm.runInThisContext(source.slice(source.indexOf('function updateCookiePoolLive('),source.indexOf('let cookiePoolPolling')));
const session = {id:'a',name:'Account A',state:'cooldown',cooldown_kind:'account_rate_limit',cooldown_until:Date.now()/1000+3600,limit:0,interval:180};
const data = {pool:{accounts:[{id:'a',configured_limit:3}],session_states:[session],capacity_summary:{}}};
updateCookiePoolLive(data);
assert(check.disabled);
assert(classes.has('cookie-cooling'));
assert(policy.textContent.includes('明确账号限流'));
assert(policy.textContent.includes('当前最多 0 个任务'));
assert.equal(selection.checked,false);
session.state = 'recovery'; session.limit = 1; session.recovery_successes = 2;
updateCookiePoolLive(data);
assert(!check.disabled);
assert(!classes.has('cookie-cooling'));
assert(classes.has('cookie-recovery'));
assert(policy.textContent.includes('本轮有效成功 2 次'));
assert.equal(selection.checked,false);
"""
        result = subprocess.run(['node', '-e', program], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
