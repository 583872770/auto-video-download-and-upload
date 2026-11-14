"""One persistent request lane for captions, shared by all Cookie accounts."""
from contextlib import contextmanager

from webapp.download_policy import cooldown_deadline, failure_evidence, remember_decision
from webapp.throughput import PipelineBusy


SCOPE = 'subtitles:youtube-origin'
INTERVAL = 30
COOLDOWN = 15 * 60


def _busy(delay, *, cooling=False):
    error = PipelineBusy('字幕接口限流冷却中，等待恢复后重试。' if cooling else
                         '字幕请求正在排队，等待请求间隔或其他字幕任务释放名额。')
    error.retry_after = max(2, delay)
    # Caption admission must not hold the instance's video claim gate.
    error.start_gate_delay = 0
    error.wait_scope = 'subtitles'
    raise error


def check(scheduler):
    """Avoid extracting metadata repeatedly while a subtitle-only job is blocked."""
    if not scheduler.enabled:
        return
    states, active, stamp = scheduler.snapshot([SCOPE])
    state = states.get(SCOPE, {})
    delay = max(state.get('next_at', 0), state.get('cooldown_until', 0)) - stamp
    if delay > 0 or active.get(SCOPE, 0):
        _busy(delay, cooling=state.get('cooldown_until', 0) > stamp)


def failed(scheduler, error):
    """HTTP 429 on captions can affect the endpoint across different accounts."""
    if not scheduler.enabled:
        return
    failure = failure_evidence(error, phase='subtitles', source='youtube',
        task_id=scheduler.task_id, profile_id=scheduler.config.get('_cookie_pool_selected'),
        profile_name=scheduler.config.get('_cookie_pool_profile_name'))
    if failure.http_status not in (429, 503) or failure.source in ('local', 'oss'):
        return

    def cool(db, stamp):
        state = scheduler._state(db, SCOPE)
        if state.get('last_decision', {}).get('incident_id') == failure.incident_id:
            return state['cooldown_until'], max(0, state['cooldown_until'] - stamp)
        if failure.http_status == 429:
            strikes = min(6, state.get('throttle_strikes', 0) + 1) if state.get('last_throttle_at', 0) > stamp - 86400 else 1
            delay = min(6 * 3600, max(COOLDOWN, scheduler.config.get('youtube_throttle_cooldown', 300)) * 2 ** (strikes - 1))
            state.update(throttle_strikes=strikes, last_throttle_at=stamp)
        else:
            delay = 60
        delay = max(delay, failure.retry_after or 0)
        until = max(state.get('cooldown_until', 0), cooldown_deadline(error, stamp, delay))
        state.update(cooldown_until=until, cooldown_status=failure.http_status, cooldown_kind='subtitles')
        remember_decision(state, failure, layer='subtitles', scope='共享字幕请求队列',
                          action='cooldown', until=until, stamp=stamp)
        scheduler._save(db, SCOPE, state)
        return until, max(0, until - stamp)

    until, remaining = scheduler.transaction_call(cool)
    # The task and account layers reuse this deadline, rather than immediately retrying.
    error.retry_after = max(getattr(error, 'retry_after', 0) or 0, remaining)
    return until


@contextmanager
def slot(scheduler):
    if not scheduler.enabled:
        yield
        return

    def admit(db, stamp):
        state = scheduler._state(db, SCOPE)
        delay = max(state.get('next_at', 0), state.get('cooldown_until', 0)) - stamp
        if delay > 0:
            return None, delay, state.get('cooldown_until', 0) > stamp
        token, delay = scheduler._admit(db, stamp, [(SCOPE, 1, 1)])
        if token:
            state = scheduler._state(db, SCOPE)
            state['next_at'] = stamp + INTERVAL
            scheduler._save(db, SCOPE, state)
        return token, delay, False

    token, delay, cooling = scheduler.transaction_call(admit)
    if not token:
        _busy(delay, cooling=cooling)
    try:
        yield
    except Exception as error:
        failed(scheduler, error)
        raise
    finally:
        scheduler._release(token)
