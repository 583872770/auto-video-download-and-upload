"""Persistent start budgets, independent of media transfer throughput."""
from pathlib import Path
import math
from webapp.download_policy import cooldown_deadline, recovery_policy, remember_decision
from webapp.config_schema import PACING_FIELDS, DEFAULTS
from webapp.concurrency_policy import cookie_pool_start_interval


DAY = 86400
CLEAN_WINDOW = 6 * 3600


def session_scope(config, base, instance):
    if config.get('_cookie_pool_scope', '').startswith('youtube-account:'):
        return 'youtube-pacing:session:' + config['_cookie_pool_scope'].removeprefix('youtube-account:')
    # Re-exported files with identical core credentials share one budget.
    # Different session keys do not prove different Google account identities.
    if base is not None and config.get('youtube_cookie'):
        from webapp.cookie_pool import account_group_key, credential_key
        path = Path(config['youtube_cookie'])
        if not path.is_absolute():
            path = Path(base) / path
        try:
            key = credential_key(path.read_bytes())
        except (OSError, ValueError):
            from webapp.youtube_auth import YouTubeAuthRequired
            raise YouTubeAuthRequired({'state': 'invalid',
                'message': 'Cookie 文件缺失或无有效登录凭据，请检测并更新后再启动。'}) from None
        # Cookie-pool assignments provide the profile label selected by the
        # pool. Repeated exports with changed cookie values then share the same
        # pacing budget as their core account session.
        if config.get('_cookie_pool_profile_name'):
            key = account_group_key(config['_cookie_pool_profile_name'], path.read_bytes())
        return 'youtube-pacing:session:' + key
    return 'youtube-pacing:instance:' + instance


def summary(config, state, stamp):
    starts = [s for s in state.get('starts', []) if s > stamp - DAY]
    minimum = int(config.get('youtube_start_interval', DEFAULTS['youtube_start_interval']))
    interval = max(minimum, state.get('interval', 60))
    reason = state.get('interval_reason_code')
    if minimum >= state.get('interval', 60):
        reason = 'configured_minimum'
    elif not reason:
        # Older persisted states predate reason tracking; describe only the
        # history they actually contain, without mistaking a wait for a cause.
        reason = ('account_limit' if state.get('last_limit_at') else
                  'adaptive_backoff' if interval > 60 else
                  'stable_success' if interval < 60 else 'initial')
    explanations = {
        'configured_minimum': '当前间隔受已设置的最小间隔约束。',
        'previous_minimum': '此前提高过最小间隔，当前保留该间隔；稳定完成后逐步缩短。',
        'initial': '渐进模式从至少 60 秒起步，稳定完成后逐步缩短。',
        'account_limit': '此前发生账号限流，当前保持退避恢复节奏；稳定完成后逐步缩短。',
        'request_limit': '此前请求收到 HTTP 429，当前保持退避节奏；这不等于明确账号限流。',
        'transient_error': '上游超时或临时故障触发自动退避，当前已放慢启动节奏。',
        'adaptive_backoff': '当前保留此前的自动退避间隔，稳定完成后逐步缩短。',
        'stable_success': '连续稳定完成视频后，系统已逐步缩短启动间隔。',
    }
    return {'mode': config.get('youtube_pacing_mode', 'manual'),
            'minimum_interval': minimum, 'interval': interval,
            'initialized': 'interval' in state,
            'interval_reason_code': reason,
            'interval_reason': explanations.get(reason, explanations['adaptive_backoff']),
            'hourly_used': sum(s > stamp - 3600 for s in starts),
            'daily_used': len(starts),
            'hourly_limit': int(config.get('youtube_hourly_start_limit', DEFAULTS['youtube_hourly_start_limit'])),
            'daily_limit': int(config.get('youtube_daily_start_limit', DEFAULTS['youtube_daily_start_limit'])),
            'next_start_at': max(state.get('next_at', 0), state.get('blocked_until', 0), state.get('cooldown_until', 0)),
            'reason': state.get('wait_reason', ''),
            'cooldown_seconds': max(0, math.ceil(state.get('cooldown_until', 0) - stamp))}


class YouTubePacing:
    def __init__(self, scheduler, base=None):
        self.router = scheduler
        self.config = scheduler.config
        self.instance_scope = 'youtube-pacing:instance:' + scheduler.instance
        self.session_scope = session_scope(self.config, base, scheduler.instance)
        self.scopes = list(dict.fromkeys((self.instance_scope, self.session_scope)))
        self.history_profile_id = self.config.get('_cookie_pool_selected')
        self.history_profile_name = self.config.get('_cookie_pool_profile_name')
        if not self.history_profile_id and base is not None and self.config.get('youtube_cookie'):
            try:
                from webapp.cookie_profiles import CookieProfiles
                target = Path(self.config['youtube_cookie'])
                if not target.is_absolute():
                    target = Path(base) / target
                item = next((value for value in CookieProfiles(base, scheduler.store).items()
                             if value['id'] == target.stem), None)
                if item:
                    self.history_profile_id, self.history_profile_name = item['id'], item['name']
            except (OSError, ValueError):
                pass

    def _states(self, db, stamp, channel):
        legacy = self.router._state(db, 'quota:' + channel.quota_group)
        rows = []
        for scope in self.scopes:
            state = self.router._state(db, scope)
            floor = max(1, int(self.config.get('youtube_start_interval', DEFAULTS['youtube_start_interval'])))
            if 'interval' not in state:
                state.update(interval=max(60, floor), clean_since=stamp, clean_successes=0,
                             interval_reason_code='initial')
            # Also adopt newer limits recorded while adaptive mode was disabled.
            last_limit = legacy.get('last_throttle_at', 0)
            if (legacy.get('last_throttle_kind') == 'account' and last_limit > stamp - DAY
                    and last_limit > state.get('last_limit_at', 0)):
                state.update(interval=max(120, floor, state['interval']), last_limit_at=last_limit,
                             interval_reason_code='account_limit',
                             strikes=max(1, state.get('strikes', 0)),
                             cooldown_until=max(state.get('cooldown_until', 0), legacy.get('cooldown_until', 0)),
                             clean_since=stamp, clean_successes=0, last_adjust_at=stamp)
            if floor > state['interval']:
                state['interval_reason_code'] = 'previous_minimum'
            state['interval'] = max(floor, state['interval'])
            state['starts'] = sorted(s for s in state.get('starts', []) if s > stamp - DAY)
            rows.append((scope, state))
        return rows

    def reserve(self, db, stamp, channel):
        if not self.router._owned(db, stamp):
            from webapp.downloads import DownloadLeaseLost
            raise DownloadLeaseLost('请求节奏等待期间任务租约已失效。')
        rows = self._states(db, stamp, channel)
        next_at, reason = stamp, ''
        hourly = int(self.config.get('youtube_hourly_start_limit', DEFAULTS['youtube_hourly_start_limit']))
        daily = int(self.config.get('youtube_daily_start_limit', DEFAULTS['youtube_daily_start_limit']))
        for _, state in rows:
            candidates = [(state.get('cooldown_until', 0), '账号或源站正在冷却'),
                          (state.get('next_at', 0), '等待下一次视频启动')]
            starts = state['starts']
            hour_starts = [s for s in starts if s > stamp - 3600]
            if len(hour_starts) >= hourly:
                candidates.append((hour_starts[-hourly] + 3600, '已达到滚动一小时启动预算'))
            if len(starts) >= daily:
                candidates.append((starts[-daily] + DAY, '已达到滚动二十四小时启动预算'))
            for deadline, explanation in candidates:
                if deadline > next_at:
                    next_at, reason = deadline, explanation
        for scope, state in rows:
            state['wait_reason'] = reason
            state['blocked_until'] = next_at
            if next_at <= stamp:
                state['starts'].append(stamp)
                state['next_at'] = stamp + state['interval']
            self.router._save(db, scope, state)
        return max(0, next_at - stamp), reason

    def outcome(self, db, stamp, channel, *, success, size, failure, delay, error=None):
        if failure and 'pacing' not in recovery_policy(failure, self.config).targets:
            return delay
        rows = self._states(db, stamp, channel)
        from webapp.youtube_cookie_history import record as record_cookie_event
        account_limit = bool(failure and failure.kind == 'account_rate_limit')
        request_limit = bool(failure and failure.kind == 'request_rate_limit')
        if account_limit:
            # Both scopes retain the strongest backoff when their histories differ.
            for _, state in rows:
                previous = state.get('last_limit_at', 0)
                strikes = state.get('strikes', 0) + 1 if 300 < stamp - previous < DAY else max(1, state.get('strikes', 0))
                if previous <= stamp - DAY:
                    strikes = 1
                delay = max(delay or 0, min(21600, 3600 * 2 ** min(3, strikes - 1)))
        for scope, state in rows:
            if failure and state.get('last_decision', {}).get('incident_id') == failure.incident_id:
                continue
            if account_limit:
                # Concurrent reports from the same incident are one strike.
                same_incident = stamp - state.get('last_limit_at', 0) <= 300
                repeat = (state.get('last_limit_at', 0) > stamp - DAY and
                          stamp - state.get('last_limit_at', 0) > 300)
                strikes = state.get('strikes', 0) + 1 if repeat else max(1, state.get('strikes', 0))
                if state.get('last_limit_at', 0) <= stamp - DAY:
                    strikes = 1
                delay = max(delay or 0, min(21600, 3600 * 2 ** min(3, strikes - 1)))
                previous_interval = int(state.get('interval', 60))
                state.update(strikes=strikes, last_limit_at=stamp,
                             interval_reason_code='account_limit',
                             interval=min(600, max(120, state['interval'] if same_incident else state['interval'] * 2)),
                             cooldown_until=max(state.get('cooldown_until', 0), cooldown_deadline(error, stamp, delay)),
                             clean_since=stamp, clean_successes=0, last_adjust_at=stamp,
                             wait_reason='账号或源站正在冷却')
                if self.history_profile_id:
                    record_cookie_event(self.router.store, db=db, profile_id=self.history_profile_id,
                        profile_name=self.history_profile_name, event_type='pacing_failure', phase=failure.phase,
                        kind=failure.kind, message=failure.message, incident_id=failure.incident_id,
                        http_status=failure.http_status, retry_after=failure.retry_after,
                        previous_interval=previous_interval, new_interval=int(state['interval']),
                        previous_state='ready', new_state='cooldown', cooldown_until=state.get('cooldown_until'),
                        task_id=failure.task_id, scope='youtube-pacing:' + self.session_scope)
            elif request_limit:
                previous_interval = int(state.get('interval', 60))
                state.update(interval=min(600, max(90, math.ceil(state['interval'] * 1.5))),
                             interval_reason_code='request_limit',
                             cooldown_until=max(state.get('cooldown_until', 0), cooldown_deadline(error, stamp, delay)),
                             clean_since=stamp, clean_successes=0, last_adjust_at=stamp)
                if self.history_profile_id:
                    record_cookie_event(self.router.store, db=db, profile_id=self.history_profile_id,
                        profile_name=self.history_profile_name, event_type='pacing_failure', phase=failure.phase,
                        kind=failure.kind, message=failure.message, incident_id=failure.incident_id,
                        http_status=failure.http_status, retry_after=failure.retry_after,
                        previous_interval=previous_interval, new_interval=int(state['interval']),
                        previous_state='ready', new_state='cooldown', cooldown_until=state.get('cooldown_until'),
                        task_id=failure.task_id, scope='youtube-pacing:' + self.session_scope)
            elif success and size > 0 and state.get('cooldown_until', 0) <= stamp:
                state['clean_successes'] = state.get('clean_successes', 0) + 1
                # Require sustained verified completions, then at most one
                # five-second step per two hours. Skips never warm the policy.
                if (state['clean_successes'] >= 100 and stamp - state['clean_since'] >= CLEAN_WINDOW
                        and stamp - state.get('last_adjust_at', state['clean_since']) >= 7200):
                    previous_interval = int(state.get('interval', 60))
                    state['interval'] = max(int(self.config.get('youtube_start_interval', DEFAULTS['youtube_start_interval'])),
                                            state['interval'] - 5)
                    state['interval_reason_code'] = 'stable_success'
                    state['last_adjust_at'] = stamp
                    if self.history_profile_id and state['interval'] != previous_interval:
                        record_cookie_event(self.router.store, db=db, profile_id=self.history_profile_id,
                            profile_name=self.history_profile_name, event_type='interval_change', phase='pacing',
                            kind='stable_success', message='连续稳定完成任务，逐步缩短启动间隔。',
                            previous_interval=previous_interval, new_interval=int(state['interval']),
                            previous_state='ready', new_state='ready', scope='youtube-pacing:' + self.session_scope)
            if failure and (account_limit or request_limit):
                remember_decision(state, failure, layer='pacing', scope='实例与登录会话启动节奏',
                    action='cooldown', until=state.get('cooldown_until', 0), stamp=stamp)
            self.router._save(db, scope, state)
        return delay
