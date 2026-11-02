"""Single-instance Cookie admission with fenced permits and conservative pacing.

No retry changes identities. Explicit account limits or verification halt the
whole pool; content failures do not lower unrelated accounts' capacity.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import random
import time

from Utils.youtube_login import parse_youtube_cookie_file
from webapp.channel_scheduler import ChannelScheduler, throttle_details, error_traits
from webapp.cookie_profiles import CookieProfiles
from webapp.throughput import PipelineBusy
from webapp.youtube_auth import AUTH_COOKIES, YouTubeAuthRequired


# A newly imported account remains deliberately conservative.  Once the same
# session has produced a long, clean run, its start interval can be reduced in
# small steps.  The floor still leaves a visible gap between authenticated
# YouTube requests and is restored by any timeout/throttle outcome below.
MATURE_ACCOUNT_INTERVAL = 30
STABLE_ACCOUNT_INTERVAL = 45


def halt_message(state, *, previous=False):
    """Describe persisted admission state without reasserting a fresh source error."""
    cause = {'account_rate_limit': 'YouTube 明确提示账号被限流',
             'http_429': 'HTTP 429（请求过多）',
             'authentication_required': '登录检查未通过或需要额外验证'}.get(
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


def capacity_summary(snapshot, stamp=None):
    """Public capacity estimate, never evidence of independent Google accounts."""
    stamp = time.time() if stamp is None else stamp
    sessions = [a for a in snapshot['accounts'] if not a.get('shared_with') and not a.get('unavailable')]
    capacities = [(a, min(2, max(1, int(a.get('limit', 1))))) for a in sessions]
    capacity = sum(limit for a, limit in capacities if a.get('cooldown_until', 0) <= stamp)
    active = sum(a.get('active', 0) for a in sessions)
    next_starts = [max(snapshot.get('next_at', 0), a.get('next_at', 0), a.get('cooldown_until', 0), stamp)
                   for a, limit in capacities if a.get('active', 0) < limit]
    blocked = bool(snapshot.get('halted')) or snapshot.get('cooldown_until', 0) > stamp
    capacity = 0 if blocked else capacity
    warmup = sum(1 for _, limit in capacities if limit < 2)
    reason = ('Cookie 会话池已拦截，需完成检查后人工恢复' if blocked else
              '没有可用的 Cookie 会话' if not sessions else
              'Cookie 会话正在冷却' if not capacity else
              f"{snapshot.get('configured_files', len(snapshot['accounts']))} 份 Cookie 合并为 {len(sessions)} 个核心会话，"
              f"当前容量 {capacity}，{warmup} 个会话尚未完成预热" if warmup else
              f'Cookie 核心会话当前容量为 {capacity}')
    intervals = [int(a.get('interval', 60)) for a, _ in capacities if a.get('interval')]
    return {'known': True, 'sampled_at': stamp, 'configured_files': snapshot.get('configured_files', len(snapshot['accounts'])),
            'effective_sessions': len(sessions), 'unavailable_files': sum(bool(a.get('unavailable')) for a in snapshot['accounts']),
            'identity_verified': False, 'capacity': capacity, 'active': active, 'warming_sessions': warmup,
            'min_interval': min(intervals) if intervals else None,
            'halted': blocked, 'reason': reason,
            'next_start_at': min(next_starts) if next_starts and not blocked else None}


class CookiePool:
    def __init__(self, store, config, base, task_id, *, strict=True):
        self.router = ChannelScheduler(store, config, task_id)
        self.config, self.base = config, base
        profiles = CookieProfiles(base, store)
        self.accounts = []
        self.unavailable = []
        self.aliases = {}
        seen = {}
        for identity in config.get('youtube_cookie_pool', []):
            try:
                target = profiles.require(identity)
                key = credential_key(target.read_bytes())
            except (ValueError, OSError):
                if strict:
                    raise YouTubeAuthRequired({'state': 'invalid',
                        'message': '账号池中的 Cookie 文件缺失、无效或过期，请检测并更新后再启动。'}) from None
                self.unavailable.append({'id': identity, 'unavailable': True})
                continue
            if key not in seen:
                self.accounts.append((identity, target, 'youtube-account:' + key))
                seen[key] = identity
            else:
                self.aliases[identity] = seen[key]
        self.global_scope = 'youtube-cookie-pool:' + self.router.instance

    def status(self):
        def read(db, stamp):
            global_state = self.router._state(db, self.global_scope)
            accounts = [{'id': identity, **self.router._state(db, scope),
                         'active': self.router._active(db, scope, stamp)} for identity, _, scope in self.accounts]
            for identity, canonical in self.aliases.items():
                accounts.append({**next(a for a in accounts if a['id'] == canonical),
                                 'id': identity, 'shared_with': canonical})
            result = {'halted': bool(global_state.get('halted')),
                    'reason': halt_message(global_state) if global_state.get('halted') else None,
                    'halted_at': global_state.get('halted_at'), 'halt_kind': global_state.get('halt_kind'),
                    'cooldown_until': global_state.get('cooldown_until', 0),
                    'next_at': global_state.get('next_at', 0),
                    'effective_accounts': len(self.accounts), 'effective_sessions': len(self.accounts),
                    'configured_files': len(self.config.get('youtube_cookie_pool', [])),
                    'accounts': accounts + self.unavailable}
            result['capacity_summary'] = capacity_summary(result, stamp)
            return result
        return self.router.transaction_call(read)

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
            gate.update(halted=False, reason=None, halted_at=None, halt_kind=None)
            self.router._save(db, self.global_scope, gate)
        self.router.transaction_call(clear)

    @contextmanager
    def assignment(self):
        router = self.router
        if not router.enabled or not self.accounts:
            raise ValueError('Cookie 账号池必须通过下载队列执行，且至少选择一个账号。')

        def reserve(db, stamp):
            gate = router._state(db, self.global_scope)
            if gate.get('halted'):
                raise YouTubeAuthRequired({'state': 'verification_required',
                    'source': 'cookie_pool_halted', 'halted_at': gate.get('halted_at'),
                    'halt_kind': gate.get('halt_kind'), 'message': halt_message(gate, previous=True)})
            if gate.get('next_at', 0) > stamp:
                return None, gate['next_at'] - stamp
            candidates, delays = [], []
            for identity, path, scope in self.accounts:
                state = router._state(db, scope)
                active = router._active(db, scope, stamp)
                if max(state.get('next_at', 0), state.get('cooldown_until', 0)) > stamp:
                    delays.append(max(state.get('next_at', 0), state.get('cooldown_until', 0)) - stamp)
                    continue
                cap = min(2, max(1, state.get('limit', 1)))
                if active < cap:
                    candidates.append((active, state.get('last_start', 0), identity, path, scope, state, cap))
            if not candidates:
                return None, min(delays, default=10)
            _, _, identity, path, scope, state, cap = min(candidates, key=lambda x: x[:3])
            token, delay = router._admit(db, stamp, [(scope, cap, 1)])
            if not token:
                return None, delay
            # Preserve the permit policy written by _admit.
            state = router._state(db, scope)
            # Keep the persisted per-account backoff.  Stable accounts may
            # reach the mature 30s floor, while a fresh account always starts
            # at the conservative 60s interval.
            interval = max(30, state.get('interval', max(60, int(self.config.get('youtube_start_interval', 60)))))
            jitter = min(10, max(2, interval * .2))
            state.update(last_start=stamp, interval=interval, next_at=stamp + interval + random.uniform(0, jitter))
            router._save(db, scope, state)
            # This floor remains shared by all accounts on the same instance,
            # but should not become the bottleneck once each account has its
            # own 30s/45s lease.  With three mature sessions this allows at
            # most six starts per minute while each session remains at its
            # individual interval.
            gate['next_at'] = stamp + max(10, 30 / len(self.accounts))
            router._save(db, self.global_scope, gate)
            return (identity, path, scope, token), 0

        selected, delay = router.transaction_call(reserve)
        if not selected:
            error = PipelineBusy('Cookie 账号池正在按账号并发及请求间隔等待。')
            error.retry_after = max(2, min(60, delay))
            raise error
        identity, path, scope, token = selected
        error = None
        try:
            yield {**self.config, 'youtube_cookie': str(path), '_cookie_pool_selected': identity}
        except BaseException as failure:
            error = failure
            raise
        finally:
            try:
                self.outcome(scope, error)
            finally:
                router._release(token)

    def outcome(self, scope, error):
        status, delay = throttle_details(error) if error else (None, None)
        transient, video_error = error_traits(error) if error else (False, False)
        if error:
            from webapp.proxy_health import connection_failure
            transient |= (connection_failure(error) or status == 503 or
                          any(term in str(error).lower() for term in ('timed out', 'timeout')))
        auth_block = isinstance(error, YouTubeAuthRequired) and not error.retryable
        text = str(error).lower()
        content = video_error or any(term in text for term in (
            'requested format is not available', 'drm protected', 'account associated with this video has been terminated'))
        if content and status != 429 and not auth_block:
            return
        if error and not auth_block and status != 429 and not transient:
            # Includes content failures, local pressure, and unknown failures;
            # none are evidence that an account can safely increase its pace.
            return

        def record(db, stamp):
            state = self.router._state(db, scope)
            if auth_block or status == 429:
                kind = ('account_rate_limit' if getattr(error, 'account_rate_limited', False) or
                        'your account has been rate-limited' in str(error).lower() else
                        'http_429' if status == 429 else 'authentication_required')
                gate = self.router._state(db, self.global_scope)
                gate.update(halted=True, halt_kind=kind, halted_at=stamp,
                            cooldown_until=stamp + max(3600, delay or 0))
                gate['reason'] = halt_message(gate)
                self.router._save(db, self.global_scope, gate)
                state.update(limit=1, streak=0, interval=max(120, state.get('interval', 60) * 2),
                             cooldown_until=stamp + max(3600, delay or 0))
            elif error:
                state.update(limit=1, streak=0, first_success=0,
                             interval=min(600, max(120, state.get('interval', 60) * 2)),
                             cooldown_until=stamp + max(60, delay or 0))
            else:
                state['streak'] = state.get('streak', 0) + 1
                state['first_success'] = state.get('first_success') or stamp
                state['successes'] = state.get('successes', 0) + 1
                if state['streak'] >= 5 and stamp - state['first_success'] >= 600:
                    state['limit'] = 2
                    # The first stable window only moves to 45s.  A mature
                    # session (20 clean completions over at least 30 minutes)
                    # can step down to 30s; any error below resets it to 120s.
                    floor = (MATURE_ACCOUNT_INTERVAL
                             if state['successes'] >= 20 and stamp - state['first_success'] >= 1800
                             else STABLE_ACCOUNT_INTERVAL)
                    step = 15 if floor == MATURE_ACCOUNT_INTERVAL else 5
                    state['interval'] = max(floor, state.get('interval', 60) - step)
            self.router._save(db, scope, state)
        self.router.transaction_call(record)
