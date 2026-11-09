"""Cookie request limits shared by admission, retries and status rendering."""
from webapp.config_schema import DEFAULTS


DAY = 86400
RECOVERY_INTERVAL = 300
RECOVERY_WINDOW = 3600
RECOVERY_SUCCESSES = 10
RETRY_SHARE = .2


def account_interval(config, state):
    return max(30, int(config.get('youtube_start_interval', DEFAULTS['youtube_start_interval'])),
               int(state.get('interval', 60)), RECOVERY_INTERVAL if state.get('recovery_mode') else 0)


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
