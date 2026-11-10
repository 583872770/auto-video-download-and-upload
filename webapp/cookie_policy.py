"""Cookie request limits shared by admission, retries and status rendering."""
from webapp.config_schema import DEFAULTS


DAY = 86400
RECOVERY_INTERVAL = 300
RECOVERY_WINDOW = 3600
RECOVERY_SUCCESSES = 10
STABLE_ACCOUNT_INTERVAL = 45
MATURE_ACCOUNT_INTERVAL = 30
STABLE_SUCCESSES = 5
STABLE_WINDOW = 600
MATURE_SUCCESSES = 20
MATURE_WINDOW = 1800
STABLE_STEP = 5
MATURE_STEP = 15
PROTECTION_INTERVAL = 120
PROTECTION_STAGE_WINDOW = 900
PROTECTION_STAGE_SUCCESSES = 5
PROTECTION_STAGE_STEP = 15
RETRY_SHARE = .2


def account_interval(config, state):
    return max(30, int(config.get('youtube_start_interval', DEFAULTS['youtube_start_interval'])),
               int(state.get('interval', 60)), int(state.get('recovery_interval_floor', 0)),
               RECOVERY_INTERVAL if state.get('recovery_mode') else 0)


def stable_interval_step(state, stamp):
    mature = (state.get('successes', 0) >= MATURE_SUCCESSES
              and state.get('first_success', 0) and stamp - state['first_success'] >= MATURE_WINDOW)
    floor = MATURE_ACCOUNT_INTERVAL if mature else STABLE_ACCOUNT_INTERVAL
    step = MATURE_STEP if mature else STABLE_STEP
    return step, max(floor, state.get('recovery_interval_floor', 0), int(state.get('interval', 60)) - step)


def protection_progress(config, state, stamp):
    """Read a post-limit stage; historical success totals never satisfy it."""
    if (state.get('recovery_mode') or not state.get('recovery_interval_floor')
            or int(state.get('interval', 60)) > PROTECTION_INTERVAL):
        return None
    target = max(30, int(config.get('youtube_start_interval', DEFAULTS['youtube_start_interval'])))
    current = max(30, int(state.get('interval', 60)), int(state['recovery_interval_floor']),
                  min(target, PROTECTION_INTERVAL))
    stage = state.get('protection_stage', {})
    matches = stage.get('interval') == current and stage.get('target') == target
    since = stage.get('started_at', 0) if matches else 0
    count = int(stage.get('successes', 0)) if since else 0
    next_interval = max(target, current - PROTECTION_STAGE_STEP) if current > target else current
    return dict(interval=current, target=target, initialized=bool(since),
                observed_since=since, success_count=count, success_target=PROTECTION_STAGE_SUCCESSES,
                observation_seconds=PROTECTION_STAGE_WINDOW, step=PROTECTION_STAGE_STEP,
                next_interval=next_interval, target_reached=current <= target,
                ready=bool(since and stamp - since >= PROTECTION_STAGE_WINDOW
                           and count >= PROTECTION_STAGE_SUCCESSES))


def _start_protection_stage(state, interval, target, stamp):
    state.update(interval=interval, recovery_interval_floor=interval,
                 protection_stage=dict(interval=interval, target=target, started_at=stamp, successes=0))


def advance_account_interval(config, state, stamp, *, admitted_at=None):
    """Settle one valid success, in the caller's existing account transaction."""
    stage = protection_progress(config, state, stamp)
    if stage is not None:
        if not stage['initialized']:
            # The completion that establishes a stage may have started under
            # older policy/spacing. Only later admissions count for this stage.
            _start_protection_stage(state, stage['interval'], stage['target'], stamp)
        elif not stage['target_reached']:
            if admitted_at is not None and admitted_at < stage['observed_since']:
                return 'protection_stage'
            saved = dict(state['protection_stage'])
            saved['successes'] = stage['success_count'] + 1
            state['protection_stage'] = saved
            if protection_progress(config, state, stamp)['ready']:
                _start_protection_stage(state, stage['next_interval'], stage['target'], stamp)
        return 'protection_stage'
    if state.get('streak', 0) >= STABLE_SUCCESSES and stamp - state.get('first_success', stamp) >= STABLE_WINDOW:
        state['interval'] = stable_interval_step(state, stamp)[1]
        # Arriving at 120 establishes a fresh stage; high-interval successes
        # cannot be reused to step below it immediately.
        stage = protection_progress(config, state, stamp)
        if stage is not None:
            _start_protection_stage(state, stage['interval'], stage['target'], stamp)
    return 'stable_success'


def pacing_progress(config, state, stamp):
    """Describe the next success-driven transition without changing admission state.

    The prediction is conditional on no intervening failure/settings change.
    Leaving recovery restores capacity but does not reduce the saved interval.
    """
    recovery = bool(state.get('recovery_mode'))
    since = state.get('recovery_started_at', 0) if recovery else state.get('first_success', 0)
    count = int(state.get('recovery_successes', 0) if recovery else state.get('streak', 0))
    target = RECOVERY_SUCCESSES if recovery else STABLE_SUCCESSES
    window = RECOVERY_WINDOW if recovery else STABLE_WINDOW
    elapsed = max(0, stamp - since) if since else 0
    saved = int(state.get('interval', 60))
    # Predict at the earliest time the observation gate can be satisfied,
    # including the success that will cause the transition.
    step, next_saved = ((0, saved) if recovery else stable_interval_step(
        {**state, 'successes': int(state.get('successes', 0)) + 1},
        max(stamp, since + window) if since else stamp))
    stage = protection_progress(config, state, stamp)
    if stage is not None:
        since, count = stage['observed_since'], stage['success_count']
        target, window = stage['success_target'], stage['observation_seconds']
        elapsed = max(0, stamp - since) if since else 0
        step, next_saved = stage['step'], stage['next_interval']
    return dict(recovery=recovery, cooling_until=state.get('cooldown_until', 0),
        phase='recovery' if recovery else 'protection_stage' if stage else 'stable',
        stage_initialized=stage['initialized'] if stage else False,
        target_reached=stage['target_reached'] if stage else False,
        interval=account_interval(config, state), saved_interval=saved,
        recovery_minimum=RECOVERY_INTERVAL,
        configured_minimum=max(30, int(config.get('youtube_start_interval', DEFAULTS['youtube_start_interval']))),
        protection_minimum=int(state.get('recovery_interval_floor', 0)),
        success_count=count, success_target=target, successes_remaining=max(0, target - count),
        observed_since=since or 0, observed_seconds=elapsed, observation_seconds=window,
        observation_until=since + window if since else 0,
        seconds_remaining=max(0, window - elapsed),
        next_interval=account_interval(config, {**state, 'interval': next_saved, 'recovery_mode': False,
            **({'recovery_interval_floor': next_saved} if stage else {})}),
        step=step,
        stable_step=STABLE_STEP, mature_step=MATURE_STEP,
        mature_success_target=MATURE_SUCCESSES, mature_observation_seconds=MATURE_WINDOW)


def task_rest_until(config, state):
    """Derive rest from the last completion so later claims use saved settings."""
    seconds = int(config.get('youtube_task_rest_interval', DEFAULTS['youtube_task_rest_interval']))
    finished = state.get('last_finished_at', 0)
    return finished + seconds if finished and seconds else 0


def next_account_start(config, state, budget_until=0):
    return max(state.get('next_at', 0), budget_until, task_rest_until(config, state),
               state.get('last_start', 0) + account_interval(config, state) if state.get('last_start') else 0)


def start_budget(config, state, stamp):
    """Return retained admissions and their next permitted start, without writes."""
    starts = sorted(s for s in state.get('starts', []) if s > stamp - DAY)
    hourly = int(config.get('youtube_hourly_start_limit', DEFAULTS['youtube_hourly_start_limit']))
    daily = int(config.get('youtube_daily_start_limit', DEFAULTS['youtube_daily_start_limit']))
    hour = [s for s in starts if s > stamp - 3600]
    until = max(hour[-hourly] + 3600 if len(hour) >= hourly else 0,
                starts[-daily] + DAY if len(starts) >= daily else 0)
    return starts, until


def retry_interval(config, states):
    """Reserve at most 20% of the pool's allowed start rate for manual replays."""
    rate = sum(1 / account_interval(config, state) for state in states)
    shared = int(config.get('youtube_cookie_pool_start_interval', DEFAULTS['youtube_cookie_pool_start_interval']))
    return max(1, shared, 1 / rate if rate else 60) / RETRY_SHARE


def account_limit_delay(state, stamp, minimum=3600):
    # Only independent limit incidents in a rolling day escalate protection.
    previous = state.get('limited_at', 0)
    strikes = min(4, state.get('limit_strikes', 1) + 1) if previous and 300 < stamp - previous < DAY else (
        max(1, state.get('limit_strikes', 1)) if previous and 0 <= stamp - previous <= 300 else 1)
    return strikes, max(minimum, 3600 * 2 ** (strikes - 1))
