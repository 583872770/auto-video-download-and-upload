"""Single-instance Cookie admission with fenced permits and conservative pacing.

Account limits cool only the affected account. Verification still requires
manual handling; content failures do not lower unrelated accounts' capacity.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import random
import re
import time
from functools import lru_cache

from sqlalchemy import select

from Utils.youtube_login import parse_youtube_cookie_file
from webapp.channel_scheduler import ChannelScheduler
from webapp.download_policy import (RecoveryPolicy, failure_evidence, recovery_policy, cooldown_deadline, remember_decision)
from webapp.cookie_profiles import CookieProfiles
from webapp.throughput import PipelineBusy
from webapp.youtube_auth import AUTH_COOKIES, YouTubeAuthRequired
from webapp.concurrency_policy import (MAX_TRANSFERS, cookie_pool_start_interval,
                                      cookie_session_limit as session_limit)
from webapp.cookie_policy import (account_interval, start_budget, retry_interval, account_limit_delay,
                                  RECOVERY_INTERVAL, RECOVERY_WINDOW, RECOVERY_SUCCESSES,
                                  STABLE_ACCOUNT_INTERVAL, MATURE_ACCOUNT_INTERVAL,
                                  STABLE_SUCCESSES, STABLE_WINDOW, MATURE_SUCCESSES, MATURE_WINDOW,
                                  STABLE_STEP, MATURE_STEP,
                                  pacing_progress)
from webapp.config_schema import DEFAULTS


@lru_cache(maxsize=256)
def _member_identity(name, content):
    # Bounded, process-local parsing cache. Live membership and binding are
    # always read from the DB; replacement content/name changes this key.
    data = content.encode('utf-8')
    return account_group_key(name, data), 'credential:' + credential_key(data)


# A newly imported account remains deliberately conservative.  Once the same
# session has produced a long, clean run, its start interval can be reduced in
# small steps.  The floor still leaves a visible gap between authenticated
# YouTube requests and is restored by any timeout/throttle outcome below.
DENIED_WINDOW = 15 * 60
DENIED_THRESHOLD = 3
DENIED_COOLDOWN = 15 * 60


def halt_message(state, *, previous=False):
    """Describe persisted admission state without reasserting a fresh source error."""
    cause = {'account_rate_limit': 'YouTube 明确提示账号被限流',
             'http_429': 'HTTP 429（请求过多）',
             'authentication_required': '登录检查未通过或需要额外验证',
             'verification_required': '源站要求额外验证'}.get(
                 state.get('halt_kind'), '旧记录未区分限流与登录验证')
    if previous:
        # The gate is checked before reserving an account or yielding to yt-dlp.
        # Historical causes remain in status metadata; this is a local refusal.
        message = 'Cookie 池沿用此前保存的停止状态，本次尚未选择 Cookie 或发起视频下载请求。'
    else:
        message = f'Cookie 池已停止。触发原因：{cause}。'
    stamp = state.get('halted_at')
    if isinstance(stamp, (int, float)) and 0 < stamp < 253402300800:
        message += '原停止时间：' + datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec='seconds') + '。'
    if state.get('limited_profile_name'):
        message += '触发账号：' + str(state['limited_profile_name']) + '。'
    return message + '请在停止实例后检查全部账号并解除拦截；冷却到期不会自动恢复。'


def credential_key(data):
    cookies = parse_youtube_cookie_file(data)
    # LOGIN_INFO and auxiliary flags can differ across repeated exports of the
    # same browser session. SID/PSID, rather than the entire file, owns a budget.
    values = []
    for name in ('SID', '__Secure-3PSID', '__Secure-1PSID', 'SAPISID'):
        values = sorted(set((c['name'], c['value']) for c in cookies if c['name'] == name and c['value']))
        if values:
            break
    if not values:
        values = sorted((c['name'], c['value']) for c in cookies
                        if c['name'] in AUTH_COOKIES and c['value'])
    if not values:
        raise ValueError('账号池中的 Cookie 缺少登录凭据，请先检测登录。')
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def account_group_key(name, data):
    """Return a durable local group for repeated exports of one account."""
    label = ' '.join(str(name or '').split())
    label = re.sub(r'\s*[·•]\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*$', '', label).strip()
    generic = {'cookies.txt', 'cookie.txt', '原有 cookie（上传 / 浏览器登录）'}
    if label and label.casefold() not in generic and not label.startswith('浏览器登录 '):
        return 'named:' + hashlib.sha256(label.casefold().encode('utf-8')).hexdigest()
    return 'credential:' + credential_key(data)


def session_view(identity, name, state, config, stamp, *, pacing=None):
    cooling = state.get('cooldown_until', 0) > stamp
    recovery = bool(state.get('recovery_mode'))
    starts, budget_until = start_budget(config, state, stamp)
    return dict(id=identity, name=name, state='cooldown' if cooling else 'recovery' if recovery else 'ready',
        active=int(state.get('active', 0)), next_start_at=max(state.get('next_at', 0), budget_until,
            state.get('last_start', 0) + account_interval(config, state) if state.get('last_start') else 0),
        hourly_used=sum(s > stamp - 3600 for s in starts), daily_used=len(starts), budget_until=budget_until,
        hourly_limit=int(config.get('youtube_hourly_start_limit', DEFAULTS['youtube_hourly_start_limit'])),
        daily_limit=int(config.get('youtube_daily_start_limit', DEFAULTS['youtube_daily_start_limit'])),
        cooldown_kind=state.get('cooldown_kind'), cooldown_until=state.get('cooldown_until', 0),
        last_failure_at=state.get('last_failure_at', 0),
        denied_count=len(state.get('denied_tasks', [])),
        limited_at=state.get('limited_at'), limited_count=state.get('limited_count', 0),
        recovery_started_at=state.get('recovery_started_at', 0),
        recovery_successes=state.get('recovery_successes', 0),
        pacing=pacing if pacing is not None else pacing_progress(config, state, stamp),
        interval=account_interval(config, state),
        limit=0 if cooling else session_limit(config, state),
        configured_limit=int(config.get('youtube_cookie_session_limit', DEFAULTS['youtube_cookie_session_limit'])))


def capacity_summary(snapshot, stamp=None):
    """Public capacity estimate, never evidence of independent Google accounts."""
    stamp = time.time() if stamp is None else stamp
    sessions = [a for a in snapshot['accounts'] if not a.get('shared_with') and not a.get('unavailable')]
    capacities = [(a, max(1, int(a.get('limit', 1)))) for a in sessions]
    capacity = sum(limit for a, limit in capacities if a.get('cooldown_until', 0) <= stamp)
    capacity = min(capacity, MAX_TRANSFERS)
    active = sum(a.get('active', 0) for a in sessions)
    next_starts = [max(snapshot.get('next_at', 0), a.get('next_at', 0), a.get('cooldown_until', 0), stamp)
                   for a, limit in capacities if a.get('active', 0) < limit]
    blocked = bool(snapshot.get('halted')) or snapshot.get('cooldown_until', 0) > stamp
    capacity = 0 if blocked else capacity
    warmup = sum(1 for a, limit in capacities if a.get('warming', limit < 2))
    session_states = snapshot.get('session_states', [])
    cooling_sessions = [s for s in session_states if s['state'] == 'cooldown']
    recovery_sessions = [s for s in session_states if s['state'] == 'recovery']
    reason = ('Cookie 会话池已拦截，需完成检查后人工恢复' if snapshot.get('halted') else
              'Cookie 会话池临时冷却，到期后重新准入检查' if snapshot.get('cooldown_until', 0) > stamp else
              '没有可用的 Cookie 会话' if not sessions else
              '全部账号正在冷却，自动等待最早到期的账号；无需手动恢复' if not capacity else
              f'{len(cooling_sessions)} 个账号冷却，其他 {len(sessions) - len(cooling_sessions)} 个账号继续调度；冷却后自动低频恢复'
              if cooling_sessions else
              f"{snapshot.get('configured_files', len(snapshot['accounts']))} 份 Cookie 合并为 {len(sessions)} 个核心会话，"
              f"当前容量 {capacity}，{warmup} 个会话仍在预热；池独立调度" if warmup else
              f'Cookie 核心会话当前容量为 {capacity}，池独立调度')
    intervals = [int(a.get('interval', 60)) for a, _ in capacities if a.get('interval')]
    return {'known': True, 'sampled_at': stamp, 'configured_files': snapshot.get('configured_files', len(snapshot['accounts'])),
            'effective_sessions': len(sessions), 'unavailable_files': sum(bool(a.get('unavailable')) for a in snapshot['accounts']),
            'execution_limit': MAX_TRANSFERS, 'session_limit': snapshot.get('session_limit'),
            'identity_verified': False, 'capacity': capacity, 'active': active, 'warming_sessions': warmup,
            'min_interval': min(intervals) if intervals else None,
            'profiles': snapshot.get('profile_states', []),
            'sessions': session_states, 'cooling_sessions': len(cooling_sessions),
            'available_sessions': len(sessions) - len(cooling_sessions),
            'recovery_sessions': len(recovery_sessions),
            'halted': bool(snapshot.get('halted')), 'cooling': snapshot.get('cooldown_until', 0) > stamp,
            'cooldown_until': snapshot.get('cooldown_until', 0),
            'halted_at': snapshot.get('halted_at'), 'halt_kind': snapshot.get('halt_kind'),
            'halt_decision': snapshot.get('halt_decision'),
            'limited_profile_id': snapshot.get('limited_profile_id'),
            'limited_profile_name': snapshot.get('limited_profile_name'),
            'decisions': sorted([*snapshot.get('decisions', []),
                *(r for a in sessions for r in a.get('decisions', []))],
                key=lambda r: r.get('time', 0))[-8:], 'reason': reason,
            'session_restrictions': [dict(scope='Cookie：' + s['name'], action='cooldown',
                until=s['cooldown_until'], manual=False, reason=s['cooldown_kind']) for s in cooling_sessions],
            'next_start_at': min(next_starts) if next_starts and not blocked else None}


class CookiePool:
    def __init__(self, store, config, base, task_id, *, strict=True):
        self.router = ChannelScheduler(store, config, task_id)
        self.config, self.base = config, base
        profiles = CookieProfiles(base, store)
        self.accounts = []
        self.unavailable = []
        self.aliases = {}
        self.profile_names = {}
        try:
            members, self.unavailable = profiles.pool_members(config.get('youtube_cookie_pool', []), strict=strict)
        except (ValueError, OSError) as error:
            from webapp.cookie_binding import CookieBindingConflict
            raise YouTubeAuthRequired({'state': 'invalid',
                'message': str(error) if isinstance(error, CookieBindingConflict) else
                '账号池中的 Cookie 文件缺失、无效或过期，请检测并更新后再启动。'}) from None
        self.profile_names.update({item['id']: item['name'] for item in self.unavailable})
        parsed = []
        for item in members:
            identity = item['id']
            self.profile_names[identity] = item['name']
            try:
                group, credential = _member_identity(item['name'], item['content'])
            except ValueError:
                if strict:
                    raise YouTubeAuthRequired({'state': 'invalid',
                        'message': '账号池中的 Cookie 缺少登录凭据，请检测并更新后再启动。'}) from None
                self.unavailable.append({'id': identity, 'unavailable': True})
                continue
            parsed.append((identity, item['path'], group, credential))
        from webapp.channel_scheduler import _key
        from webapp.database import download_resources
        alias_scopes = {_key('youtube-account-alias:' + credential): credential for _, _, _, credential in parsed}
        with self.router.store.connect() as db:
            groups = {alias_scopes[key]: json.loads(raw or '{}').get('group') for key, raw in db.execute(
                select(download_resources.c.key, download_resources.c.state).where(
                    download_resources.c.key.in_(alias_scopes)))}
        missing = [(group, credential) for _, _, group, credential in parsed if not groups.get(credential)]
        if missing:
            def initialize_aliases(db, stamp):
                result = {}
                for group, credential in missing:
                    scope = 'youtube-account-alias:' + credential
                    state = self.router._state(db, scope)
                    if not state.get('group'):
                        state['group'] = group
                        self.router._save(db, scope, state)
                    result[credential] = state['group']
                return result
            groups.update(self.router.transaction_call(initialize_aliases))
        seen = {}
        for identity, target, _, credential in parsed:
            group_key = groups[credential]
            canonical = next((seen[key] for key in (group_key, credential) if key in seen), None)
            if canonical is None:
                key = group_key
                self.accounts.append((identity, target, 'youtube-account:' + key))
                seen[group_key] = identity
                seen[credential] = identity
            else:
                self.aliases[identity] = canonical
        self.global_scope = 'youtube-cookie-pool:' + self.router.instance

    def status(self):
        states, usage, sampled_at = self.router.snapshot([self.global_scope, *(scope for _, _, scope in self.accounts)])
        def read(db, stamp):
            global_state = states.get(self.global_scope, {})
            accounts = [{'id': identity, **states.get(scope, {}),
                         'active': usage.get(scope, 0)} for identity, _, scope in self.accounts]
            # Preserve the stored interval before the public account summary
            # replaces it with the effective interval imposed by configuration.
            progress = {a['id']: pacing_progress(self.config, a, stamp) for a in accounts}
            configured = int(self.config.get('youtube_cookie_session_limit', DEFAULTS['youtube_cookie_session_limit']))
            for account in accounts:
                warming = account.get('limit', 1) < 2
                account.update(limit=session_limit(self.config, account),
                               configured_limit=configured, warming=warming,
                               interval=account_interval(self.config, account))
                account['next_at'] = session_view(account['id'], '', account, self.config, stamp,
                                                  pacing=progress[account['id']])['next_start_at']
            for identity, canonical in self.aliases.items():
                accounts.append({**next(a for a in accounts if a['id'] == canonical),
                                 'id': identity, 'shared_with': canonical})
            views = [session_view(a['id'], self.profile_names.get(a['id'], a['id']), a, self.config, stamp,
                                  pacing=progress[a['id']])
                     for a in accounts if not a.get('shared_with')]
            profile_views = [dict(session_view(a['id'], self.profile_names.get(a['id'], a['id']), a,
                                              self.config, stamp, pacing=progress[a.get('shared_with') or a['id']]),
                                  shared_with=a.get('shared_with'))
                             for a in accounts]
            profile_views.extend({'id': a['id'], 'name': self.profile_names.get(a['id'], a['id']),
                                  'state': 'unavailable'} for a in self.unavailable)
            result = {'halted': bool(global_state.get('halted')),
                    'reason': halt_message(global_state) if global_state.get('halted') else None,
                    'halted_at': global_state.get('halted_at'), 'halt_kind': global_state.get('halt_kind'),
                    'halt_decision': global_state.get('halt_decision'),
                    'limited_profile_id': global_state.get('limited_profile_id'),
                    'limited_profile_name': global_state.get('limited_profile_name'),
                    'decisions': global_state.get('decisions', []),
                    'cooldown_until': global_state.get('cooldown_until', 0),
                    'next_at': global_state.get('next_at', 0),
                    'effective_accounts': len(self.accounts), 'effective_sessions': len(self.accounts),
                    'configured_files': len(self.config.get('youtube_cookie_pool', [])),
                    'execution_limit': MAX_TRANSFERS, 'session_limit': configured,
                    'accounts': accounts + self.unavailable, 'session_states': views,
                    'profile_states': profile_views}
            result['capacity_summary'] = capacity_summary(result, stamp)
            return result
        return read(None, sampled_at)

    def clear_halt_after_checks(self):
        """Called only by the idle-only API after fresh checks of every member."""
        def clear(db, stamp):
            gate = self.router._state(db, self.global_scope)
            if gate.get('cooldown_until', 0) > stamp:
                raise ValueError('账号池仍在冷却，不能解除拦截。请等待冷却结束。')
            for _, _, scope in self.accounts:
                state = self.router._state(db, scope)
                if state.get('cooldown_until', 0) > stamp or self.router._active(db, scope, stamp):
                    raise ValueError('账号仍在冷却或有任务运行，不能解除拦截。请等待冷却结束。')
            for _, _, scope in self.accounts:
                state = self.router._state(db, scope)
                state.update(limit=1, streak=0, first_success=0)
                self.router._save(db, scope, state)
            gate = self.router._state(db, self.global_scope)
            gate.update(halted=False, reason=None, halted_at=None, halt_kind=None, halt_decision=None,
                        limited_profile_id=None, limited_profile_name=None)
            self.router._save(db, self.global_scope, gate)
        self.router.transaction_call(clear)

    @contextmanager
    def assignment(self, profile_id=None, *, count_success=True, manual_retry=False):
        router = self.router
        if not router.enabled or not self.accounts:
            raise ValueError('Cookie 账号池必须通过下载队列执行，且至少选择一个账号。')

        def reserve(db, stamp):
            gate = router._state(db, self.global_scope)
            if gate.get('halted'):
                raise YouTubeAuthRequired({'state': 'verification_required',
                    'source': 'cookie_pool_halted', 'halted_at': gate.get('halted_at'),
                    'halt_kind': gate.get('halt_kind'), 'message': halt_message(gate, previous=True)})
            if gate.get('cooldown_until', 0) > stamp:
                return None, gate['cooldown_until'] - stamp, 'pool'
            if gate.get('next_at', 0) > stamp:
                return None, gate['next_at'] - stamp, 'pool'
            if manual_retry and gate.get('retry_next_at', 0) > stamp:
                return None, gate['retry_next_at'] - stamp, 'manual_retry'
            scopes = [scope for _, _, scope in self.accounts]
            state_by_scope = router._states(db, scopes)
            active_by_scope = router._active_many(db, scopes, stamp)
            states = [(identity, path, scope, state_by_scope.get(scope, {}), active_by_scope.get(scope, 0))
                      for identity, path, scope in self.accounts]
            capacity = min(MAX_TRANSFERS, sum(session_limit(self.config, state)
                for _, _, _, state, _ in states if state.get('cooldown_until', 0) <= stamp))
            candidates, delays = [], []
            for identity, path, scope, state, active in states:
                if profile_id and identity != self.aliases.get(profile_id, profile_id):
                    continue
                _, budget_until = start_budget(self.config, state, stamp)
                next_at = max(state.get('next_at', 0), state.get('cooldown_until', 0), budget_until,
                    state.get('last_start', 0) + account_interval(self.config, state) if state.get('last_start') else 0)
                if next_at > stamp:
                    delays.append(next_at - stamp)
                    continue
                cap = session_limit(self.config, state)
                if active < cap:
                    candidates.append((active, state.get('last_start', 0), identity, path, scope, state, cap))
            if not candidates:
                return None, min(delays, default=10), 'account' if profile_id else 'pool'
            _, _, identity, path, scope, state, cap = min(candidates, key=lambda x: x[:3])
            token, delay = router._admit(db, stamp, [(self.global_scope, capacity, 1), (scope, cap, 1)])
            if not token:
                resource = (router.blocked_resource or '').split(':', 1)[0]
                return None, delay, 'account' if resource == 'youtube-account' else 'pool'
            # Preserve the permit policy written by _admit.
            state = router._state(db, scope)
            if state.get('recovery_mode') and not state.get('recovery_started_at'):
                state.update(recovery_started_at=stamp, recovery_successes=0)
            # The configured floor also applies to accounts imported earlier.
            interval = account_interval(self.config, state)
            jitter = min(10, max(2, interval * .2))
            starts, _ = start_budget(self.config, state, stamp)
            state['starts'] = [*starts, stamp]
            pending = {key: value for key, value in state.get('pending_starts', {}).items()
                       if value > stamp - 86400}
            pending[router.task_id] = stamp
            state['pending_starts'] = pending
            state.update(last_start=stamp, interval=interval, next_at=stamp + interval + random.uniform(0, jitter))
            router._save(db, scope, state)
            # The same setting also spaces parser starts in ChannelScheduler.
            # Per-account intervals and cooldowns remain independent.
            gate = router._state(db, self.global_scope)
            gate['next_at'] = stamp + cookie_pool_start_interval(self.config)
            if manual_retry:
                eligible = [s for _, _, _, s, _ in states if s.get('cooldown_until', 0) <= stamp
                            and start_budget(self.config, s, stamp)[1] <= stamp]
                gate['retry_next_at'] = stamp + retry_interval(self.config, eligible)
            router._save(db, self.global_scope, gate)
            return (identity, path, scope, token, stamp), 0, 'pool'

        selected, delay, wait_scope = router.transaction_call(reserve)
        if not selected:
            messages = {
                'pool': 'Cookie 账号池正在按账号并发及请求间隔等待。',
                'account': '该任务对应的 Cookie 正在等待账号并发、启动间隔或冷却，其他账号继续调度。',
                'manual_retry': '手动重试正在等待启动份额，普通任务继续调度。',
            }
            error = PipelineBusy(messages[wait_scope])
            error.retry_after = max(2, min(60, delay))
            # Only waits affecting the whole pool may delay other task claims.
            # A pinned account/replay keeps its own available_at deadline.
            error.start_gate_delay = error.retry_after if wait_scope == 'pool' else 0
            error.wait_scope = wait_scope
            raise error
        identity, path, scope, token, admitted_at = selected
        error = None
        completed = False
        try:
            yield {**self.config, 'youtube_cookie': str(path), '_cookie_pool_selected': identity,
                   '_cookie_pool_profile_name': self.profile_names.get(identity, identity),
                   '_cookie_pool_scope': scope, '_cookie_pool_admitted_at': admitted_at,
                   '_cookie_pool_next_at': admitted_at + cookie_pool_start_interval(self.config)}
            completed = True
        except BaseException as failure:
            error = failure
            raise
        finally:
            try:
                if error or completed and count_success:
                    self.outcome(scope, error, admitted_at=admitted_at)
            finally:
                try:
                    # A local resource wait before any source request must not
                    # consume the rolling account budget. Crashes conservatively
                    # retain their reservation until the rolling window expires.
                    def refund_unstarted(db, stamp):
                        state = router._state(db, scope)
                        pending = state.get('pending_starts', {})
                        if pending.get(router.task_id) != admitted_at:
                            return
                        pending.pop(router.task_id)
                        starts = state.get('starts', [])
                        if admitted_at in starts:
                            starts.remove(admitted_at)
                        router._save(db, scope, state)
                    router.transaction_call(refund_unstarted)
                finally:
                    router._release(token)

    def outcome(self, scope, error, *, admitted_at=None):
        profile_id = next((identity for identity, _, resource in self.accounts if resource == scope), None)
        failure = failure_evidence(error, source='youtube', task_id=self.router.task_id,
            profile_id=profile_id,
            profile_name=self.profile_names.get(profile_id, profile_id)) if error else None
        policy = recovery_policy(failure, self.config) if failure else None
        if failure and failure.source == 'oss':
            return  # Storage delivery never changes YouTube session health.
        if failure and failure.kind in ('local_resource', 'local_halt'):
            return  # Scheduling/resource waits belong to the task, not Cookie failure history.
        denied = bool(failure and failure.kind == 'access_denied' and failure.http_status == 403
                      and failure.source == 'youtube' and failure.phase == 'transfer' and failure.task_id)
        from webapp.youtube_cookie_history import record as record_cookie_event
        if failure and policy.action == 'none' and not denied:
            # Content-specific failures do not alter pacing, but remain visible
            # in the per-Cookie timeline for diagnosis.
            record_cookie_event(self.router.store, profile_id=profile_id,
                profile_name=self.profile_names.get(profile_id, profile_id), event_type='failure',
                phase=failure.phase, kind=failure.kind, message=failure.message,
                incident_id=failure.incident_id, http_status=failure.http_status,
                retry_after=failure.retry_after, task_id=failure.task_id)
            return

        def record(db, stamp):
            current_policy = policy
            state = self.router._state(db, scope)
            if failure and state.get('last_decision', {}).get('incident_id') == failure.incident_id:
                return
            if (failure and failure.kind in ('account_rate_limit', 'request_rate_limit')
                    and admitted_at is not None and admitted_at < state.get('limited_at', 0)):
                # An older in-flight request cannot prolong a newer cooldown.
                return
            if denied:
                recent = [row for row in state.get('denied_tasks', []) if row['time'] >= stamp - DENIED_WINDOW]
                if any(row['task'] == failure.task_id for row in recent):
                    return  # Multiple wrappers/reports of one task are not new failures.
                recent.append(dict(task=failure.task_id, time=stamp))
                state.update(denied_tasks=recent[-20:], last_failure_at=stamp)
                # Results of work admitted before a newer protection event must
                # not extend that event's cooldown or trigger recovery probes.
                late = admitted_at is not None and admitted_at < state.get('limited_at', 0)
                if not late and (len(recent) >= DENIED_THRESHOLD or state.get('recovery_mode')):
                    strikes = min(3, state.get('denied_cooldowns', 0) + 1)
                    state['denied_cooldowns'] = strikes
                    current_policy = RecoveryPolicy('cooldown',
                                                    max(DENIED_COOLDOWN * 2 ** (strikes - 1), failure.retry_after or 0),
                                                    targets=('session',))
                else:
                    record_cookie_event(self.router.store, db=db, profile_id=profile_id,
                        profile_name=self.profile_names.get(profile_id, profile_id), event_type='failure',
                        phase=failure.phase, kind=failure.kind, message=failure.message,
                        incident_id=failure.incident_id, http_status=failure.http_status,
                        retry_after=failure.retry_after, task_id=failure.task_id,
                        metadata={'action': 'record', 'distinct_failed_tasks': len(recent)})
                    self.router._save(db, scope, state)
                    return
            if failure:
                previous_interval = int(state.get('interval', 60))
                previous_state = ('recovery' if state.get('recovery_mode') else
                                  'cooldown' if state.get('cooldown_until', 0) > stamp else 'ready')
                delay = max(current_policy.delay, getattr(error, 'retry_after', 0) or 0)
                if failure.kind == 'account_rate_limit':
                    strikes, delay = account_limit_delay(state, stamp, delay)
                    state['limit_strikes'] = strikes
                until = cooldown_deadline(error, stamp, delay)
                state.update(limit=1, streak=0, first_success=0,
                             cooldown_kind='repeated_http_403' if denied else failure.kind,
                             last_failure_at=stamp,
                             interval=min(600, max(120, state.get('interval', 60) * 2)),
                             cooldown_until=max(state.get('cooldown_until', 0), until))
                if state.get('recovery_mode'):
                    state.update(recovery_started_at=0, recovery_successes=0,
                                 interval=max(RECOVERY_INTERVAL, state['interval']))
                if denied or failure.kind in ('account_rate_limit', 'request_rate_limit'):
                    state.update(limited_at=stamp,
                                 limited_count=state.get('limited_count', 0) + 1,
                                 recovery_mode=True, recovery_started_at=0, recovery_successes=0,
                                 interval=max(RECOVERY_INTERVAL, state['interval']), recovery_interval_floor=120)
                remember_decision(state, failure, layer='session', scope='Cookie：' + self.profile_names.get(profile_id, profile_id),
                    action=current_policy.action, until=until, manual=current_policy.manual, stamp=stamp)
                record_cookie_event(self.router.store, db=db, profile_id=profile_id,
                    profile_name=self.profile_names.get(profile_id, profile_id), event_type='failure',
                    phase=failure.phase, kind=failure.kind, message=failure.message,
                    incident_id=failure.incident_id, http_status=failure.http_status,
                    retry_after=failure.retry_after, task_id=failure.task_id,
                    previous_interval=previous_interval, new_interval=int(state.get('interval', previous_interval)),
                    previous_state=previous_state,
                    new_state='recovery' if state.get('recovery_mode') else 'cooldown',
                    cooldown_until=state.get('cooldown_until'),
                    metadata={'action': current_policy.action, 'targets': current_policy.targets,
                              'distinct_failed_tasks': len(state.get('denied_tasks', [])) if denied else None})
            if failure and 'pool' in current_policy.targets:
                gate = self.router._state(db, self.global_scope)
                gate['cooldown_until'] = max(gate.get('cooldown_until', 0), until)
                first_halt = current_policy.manual and not gate.get('halted')
                if first_halt:
                    gate.update(halted=True, halt_kind=failure.kind, halted_at=stamp,
                                limited_profile_id=failure.profile_id,
                                limited_profile_name=failure.profile_name)
                    gate['reason'] = halt_message(gate)
                decision = remember_decision(gate, failure, layer='pool', scope='当前实例 Cookie 池',
                    action=current_policy.action, until=gate['cooldown_until'], manual=current_policy.manual, stamp=stamp)
                if first_halt:
                    gate['halt_decision'] = decision
                self.router._save(db, self.global_scope, gate)
            elif not failure:
                # A task already in flight cannot undo a newer failure's backoff.
                if state.get('cooldown_until', 0) > stamp:
                    return
                if admitted_at is not None and admitted_at < state.get('limited_at', 0):
                    return
                if admitted_at is not None and admitted_at >= state.get('last_failure_at', 0):
                    state['denied_tasks'] = []
                if state.get('recovery_mode'):
                    since = state.get('recovery_started_at', 0)
                    if not since:
                        return  # No recovery task has been admitted yet.
                    state['recovery_successes'] = state.get('recovery_successes', 0) + 1
                    successes = state['recovery_successes']
                    if successes >= RECOVERY_SUCCESSES and stamp - since >= RECOVERY_WINDOW:
                        state.update(recovery_mode=False, limit=2, streak=successes, first_success=since,
                                     denied_cooldowns=0)
                        record_cookie_event(self.router.store, db=db, profile_id=profile_id,
                            profile_name=self.profile_names.get(profile_id, profile_id), event_type='recovery',
                            phase='recovery', kind='stable_success',
                            message='恢复观察达到 10 次成功并持续一小时，恢复设置的并发；启动间隔继续逐步缩短。',
                            previous_state='recovery', new_state='ready',
                            new_interval=int(state.get('interval', 120)),
                            metadata={'recovery_successes': successes})
                    self.router._save(db, scope, state)
                    return
                state['streak'] = state.get('streak', 0) + 1
                state['first_success'] = state.get('first_success') or stamp
                state['successes'] = state.get('successes', 0) + 1
                if state['streak'] >= STABLE_SUCCESSES and stamp - state['first_success'] >= STABLE_WINDOW:
                    # Keep the legacy warmup marker; session_limit() applies
                    # the current per-session configuration after warmup.
                    state['limit'] = 2
                    # The first stable window only moves to 45s.  A mature
                    # session (20 clean completions over at least 30 minutes)
                    # can step down to 30s; any error below resets it to 120s.
                    floor = (MATURE_ACCOUNT_INTERVAL
                             if state['successes'] >= MATURE_SUCCESSES and stamp - state['first_success'] >= MATURE_WINDOW
                             else STABLE_ACCOUNT_INTERVAL)
                    step = MATURE_STEP if floor == MATURE_ACCOUNT_INTERVAL else STABLE_STEP
                    previous_interval = int(state.get('interval', 60))
                    state['interval'] = max(floor, state.get('recovery_interval_floor', 0),
                                            state.get('interval', 60) - step)
                    if state['interval'] != previous_interval:
                        record_cookie_event(self.router.store, db=db, profile_id=profile_id,
                            profile_name=self.profile_names.get(profile_id, profile_id),
                            event_type='interval_change', phase='success', kind='stable_success',
                            message='连续稳定完成任务，逐步缩短启动间隔。',
                            previous_interval=previous_interval, new_interval=state['interval'],
                            previous_state='ready', new_state='ready',
                            metadata={'successes': state['successes'], 'streak': state['streak']})
            self.router._save(db, scope, state)
        self.router.transaction_call(record)
