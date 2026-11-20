"""Indexed hourly test observations; HTTP incidents are separate from final outcomes."""
import math
import time
import uuid

from sqlalchemy import func, select
from webapp.database import (test_observation_hours as hours, test_observation_events as events,
    delivery_runs, tasks, cookie_profiles, instances as instance_table)
from webapp.diagnostics import safe_text


def bucket(store, db, task, event, reason, *, account='', size=0, seconds=0, stamp=None):
    if not task.get('batch_id') or task['action'] != 'download':
        return
    values = dict(job_id=task['batch_id'], instance_id=task.get('instance_id') or 'local',
        account_id=account or '', hour=int((time.time() if stamp is None else stamp) // 3600) * 3600,
        event=event, reason=reason, count=1, size=max(0, int(size or 0)),
        seconds=max(0, seconds) if isinstance(seconds, (float, int)) and math.isfinite(seconds) else 0)
    statement = store.db.insert(hours).values(**values)
    changes = {key: hours.c[key] + values[key] for key in ('count', 'size', 'seconds')}
    db.execute(statement.on_duplicate_key_update(**changes) if store.db.mysql else
        statement.on_conflict_do_update(index_elements=['job_id', 'instance_id', 'account_id', 'hour', 'event', 'reason'], set_=changes))


def terminal(store, db, task, outcome, progress, result, stamp):
    if not task.get('id') or not task.get('batch_id') or task.get('action') != 'download':
        return
    test = db.execute(select(delivery_runs.c.test_mode).where(delivery_runs.c.task_id == task['id'])).scalar_one_or_none()
    if not test:
        return
    outcome = 'succeeded' if outcome == 'test_succeeded' else outcome
    if outcome not in ('succeeded', 'failed', 'skipped'):
        return
    failure = progress.get('last_failure') or {}
    bucket(store, db, task, outcome, failure.get('reason') or failure.get('kind') or 'none',
        account=progress.get('cookie_profile_id') or progress.get('source_account_id') or failure.get('profile_id') or '',
        size=(result or {}).get('file_bytes', 0), seconds=progress.get('elapsed', 0), stamp=stamp)


def incident(store, task, failure, config):
    if config.get('storage_mode') != 'test' or not task.get('batch_id'):
        return
    value = failure.public()
    code = value.get('http_status')
    kind = value.get('kind')
    event = ('http403' if code == 403 else 'http429' if code == 429 else
        'auth' if kind in ('authentication_required', 'verification_required') else
        'local_error' if value.get('source') == 'local' else 'other_error')
    with store.connect() as db:
        identity = str(value.get('incident_id') or uuid.uuid4().hex)
        if db.execute(select(events.c.id).where(events.c.id == identity)).first():
            return
        account = value.get('profile_id') or (task.get('progress') or {}).get('cookie_profile_id') or ''
        reason = str(value.get('reason') or kind or 'unknown')[:64]
        stamp = time.time()
        db.execute(events.insert().values(id=identity, job_id=task['batch_id'],
            instance_id=task.get('instance_id') or 'local', account_id=account, created=stamp,
            task_id=task['id'], video_id=task.get('video_id'), event=event, reason=reason,
            message=safe_text(value.get('message') or '本次执行出现异常。')[:1000]))
        bucket(store, db, task, event, reason, account=account, stamp=stamp)


def prune(store):
    identity = store.fleet.id if store.fleet else 'local'
    with store.connect() as db:
        db.execute(hours.delete().where(hours.c.instance_id == identity, hours.c.hour < time.time() - 30 * 86400))
        db.execute(events.delete().where(events.c.instance_id == identity, events.c.created < time.time() - 7 * 86400))
        cutoff = db.execute(select(events.c.created).where(events.c.instance_id == identity)
            .order_by(events.c.created.desc()).offset(1999).limit(1)).scalar_one_or_none()
        if cutoff:
            db.execute(events.delete().where(events.c.instance_id == identity, events.c.created < cutoff))


def read(store, job, args):
    now = time.time()
    try:
        since = float(args.get('since', now - int(args.get('hours', 24)) * 3600))
        until = float(args.get('until', now))
    except (ValueError, TypeError):
        raise ValueError('请选择有效的观察时间范围。') from None
    if not all(math.isfinite(v) for v in (since, until)) or not now - 30 * 86400 - 60 <= since < until <= now + 60:
        raise ValueError('观察时间须在最近 30 天内。')
    # Hourly buckets align to the hour; expose the actual range in the response.
    since = int(since // 3600) * 3600
    condition = [hours.c.job_id == job, hours.c.hour >= since, hours.c.hour <= until]
    detail_condition = [events.c.job_id == job, events.c.created >= since, events.c.created <= until]
    for name in ('instance_id', 'account_id'):
        if args.get(name):
            condition.append(hours.c[name] == args[name]);detail_condition.append(events.c[name] == args[name])
    event = args.get('event', '')
    if event:
        if event not in ('http403', 'http429', 'auth', 'local_error', 'other_error'):
            raise ValueError('请选择有效的异常类型。')
        detail_condition.append(events.c.event == event)
    interval = 3600 if until - since <= 7 * 86400 else 6 * 3600
    point = (func.floor(hours.c.hour / interval) * interval).label('point')
    with store.connect() as db:
        found = db.execute(select(point, hours.c.event, func.sum(hours.c.count).label('count'),
            func.sum(hours.c.size).label('size'), func.sum(hours.c.seconds).label('seconds'))
            .where(*condition).group_by(point, hours.c.event).order_by(point)).mappings().all()
        options = db.execute(select(hours.c.instance_id, hours.c.account_id).where(hours.c.job_id == job,
            hours.c.hour >= since).distinct()).all()
        account_ids = {r.account_id for r in options if r.account_id}
        names = dict(db.execute(select(cookie_profiles.c.id, cookie_profiles.c.name)
            .where(cookie_profiles.c.id.in_(account_ids))).all()) if account_ids else {}
        instances = dict(db.execute(select(instance_table.c.id, instance_table.c.name)
            .where(instance_table.c.id.in_({r.instance_id for r in options}))).all())
        details = [dict(r) for r in db.execute(select(events, tasks.c.status).outerjoin(tasks, tasks.c.id == events.c.task_id)
            .where(*detail_condition).order_by(events.c.created.desc()).limit(100)).mappings()]
    totals = dict(succeeded=0, failed=0, skipped=0, http403=0, http429=0, auth=0, local_error=0, other_error=0)
    points = {}
    for row in found:
        count = int(row['count']);totals[row['event']] = totals.get(row['event'], 0) + count
        value = points.setdefault(int(row['point']), dict(time=int(row['point']), **{k: 0 for k in totals}, size=0, seconds=0))
        value[row['event']] = value.get(row['event'], 0) + count
        value['size'] += int(row['size']);value['seconds'] += row['seconds']
    denominator = totals['succeeded'] + totals['failed']
    for tick in range(int(since // interval) * interval, int(until // interval) * interval + 1, interval):
        points.setdefault(tick, dict(time=tick, **{k: 0 for k in totals}, size=0, seconds=0))
    return dict(since=since, until=until, totals=totals,
        success_rate=round(100 * totals['succeeded'] / denominator, 2) if denominator else None,
        points=[points[k] for k in sorted(points)], details=details,
        accounts=[dict(id=k, name=names.get(k, k)) for k in sorted(account_ids)],
        instances=[dict(id=k, name=instances.get(k, '本机' if k == 'local' else k)) for k in sorted({r.instance_id for r in options})],
        coverage='启用后开始记录，按整点统计最近 30 天；异常明细最多保留 7 天及每实例最近 2000 条。403、429 是请求异常，不直接等同于账号被风控。')
