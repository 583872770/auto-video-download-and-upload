from datetime import datetime, timedelta


def generate_schedule_within_7_days(*hour_time_list, now=None):
    """Return slots at least two hours ahead and strictly within seven days.

    Never turn a too-early scheduled upload into an immediate publication (0).
    """
    if not hour_time_list or any(type(hour) is not int or not 0 <= hour <= 23
                                 for hour in hour_time_list):
        raise ValueError('发布时间必须是 0 到 23 的整数小时，且不能为空')
    now = now or datetime.now()
    earliest = now + timedelta(hours=2)
    latest = now + timedelta(days=7)
    return [slot
            for offset in range(8)
            for hour in sorted(set(hour_time_list))
            if earliest <= (slot := datetime.combine(
                now.date() + timedelta(days=offset), datetime.min.time()
            ).replace(hour=hour, tzinfo=now.tzinfo)) < latest]
