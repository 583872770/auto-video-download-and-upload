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
RETRY_SHARE = .2


def account_interval(config, state):
    return max(30, int(config.get('youtube_start_interval', DEFAULTS['youtube_start_interval'])),
               int(state.get('interval', 60)), RECOVERY_INTERVAL if state.get('recovery_mode') else 0)


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
    # Predict at the earliest time the observation gate can be satisfied.
    mature = (not recovery and int(state.get('successes', 0)) + 1 >= MATURE_SUCCESSES
              and since and max(stamp, since + window) - since >= MATURE_WINDOW)
    step = 0 if recovery else MATURE_STEP if mature else STABLE_STEP
    saved = int(state.get('interval', 60))
    next_saved = saved if recovery else max(
        MATURE_ACCOUNT_INTERVAL if mature else STABLE_ACCOUNT_INTERVAL,
        state.get('recovery_interval_floor', 0), saved - step)
    return dict(recovery=recovery, cooling_until=state.get('cooldown_until', 0),
        interval=account_interval(config, state), saved_interval=saved,
        recovery_minimum=RECOVERY_INTERVAL,
        configured_minimum=max(30, int(config.get('youtube_start_interval', DEFAULTS['youtube_start_interval']))),
        protection_minimum=int(state.get('recovery_interval_floor', 0)),
        success_count=count, success_target=target, successes_remaining=max(0, target - count),
        observed_since=since or 0, observed_seconds=elapsed, observation_seconds=window,
        observation_until=since + window if since else 0,
        seconds_remaining=max(0, window - elapsed),
        next_interval=account_interval(config, {**state, 'interval': next_saved, 'recovery_mode': False}),
        step=step,
        stable_step=STABLE_STEP, mature_step=MATURE_STEP,
        mature_success_target=MATURE_SUCCESSES, mature_observation_seconds=MATURE_WINDOW)


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
