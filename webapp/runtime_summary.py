"""Incremental, secret-free terminal results retained independently of logs."""
from datetime import datetime, timezone
import math
import time

from sqlalchemy import select

from webapp.database import runtime_hours

RETENTION_DAYS = 30
REASONS = {'drm_only', 'format_unavailable', 'private', 'deleted', 'geo_blocked', 'no_data',
           'incomplete_response', 'content_restriction', 'access_denied', 'request_rate_limit',
           'account_rate_limit', 'authentication_required', 'verification_required',
           'transient_failure', 'storage_failure', 'local_resource', 'unknown'}


def record_terminal(store, db, row, outcome, progress, result, stamp):
    """Caller owns the task row and has decided this is a terminal transition."""
    if row['action'] != 'download':
        return
    failure = progress.get('last_failure') or {}
    reason = failure.get('reason') or failure.get('kind') or 'unknown'
    reason = reason if reason in REASONS else 'unknown'
    if outcome != 'failed':
        reason = 'none'
    result = result or {}
    elapsed = progress.get('elapsed')
    elapsed = max(0, elapsed) if isinstance(elapsed, (int, float)) and math.isfinite(elapsed) else 0
    values = dict(scope=row.get('instance_id') or 'local', hour=int(stamp // 3600) * 3600,
                  outcome=outcome, reason=reason, task_count=1,
                  file_count=max(0, int(result.get('file_count') or 0)),
                  file_bytes=max(0, int(result.get('file_bytes') or 0)),
                  elapsed_seconds=elapsed)
    statement = store.db.insert(runtime_hours).values(**values)
    updates = {name: runtime_hours.c[name] + values[name] for name in
               ('task_count', 'file_count', 'file_bytes', 'elapsed_seconds')}
    statement = (statement.on_duplicate_key_update(**updates) if store.db.mysql else
                 statement.on_conflict_do_update(index_elements=['scope', 'hour', 'outcome', 'reason'], set_=updates))
    db.execute(statement)


def prune(store, stamp=None):
    cutoff = int(((time.time() if stamp is None else stamp) - RETENTION_DAYS * 86400) // 3600) * 3600
    scope = store.fleet.id if store.fleet else 'local'
    with store.connect() as db:
        db.execute(runtime_hours.delete().where(runtime_hours.c.scope == scope, runtime_hours.c.hour < cutoff))


def read(store, days=1, stamp=None):
    """At most 30 days of small indexed buckets, never a scan of task history."""
    days = max(1, min(RETENTION_DAYS, int(days)))
    stamp = time.time() if stamp is None else stamp
    since = int((stamp - days * 86400) // 3600) * 3600
    scope = store.fleet.id if store.fleet else 'local'
    with store.connect() as db:
        rows = db.execute(select(runtime_hours).where(runtime_hours.c.scope == scope,
            runtime_hours.c.hour >= since, runtime_hours.c.hour <= stamp).order_by(runtime_hours.c.hour)).mappings().all()
    totals = dict(completed=0, succeeded=0, failed=0, skipped=0, test_succeeded=0,
                  file_count=0, file_bytes=0, elapsed_seconds=0)
    reasons, hours = {}, {}
    for row in rows:
        count = int(row['task_count'])
        totals['completed'] += count
        if row['outcome'] in totals:
            totals[row['outcome']] += count
        for name in ('file_count', 'file_bytes', 'elapsed_seconds'):
            totals[name] += row[name]
        if row['outcome'] == 'failed':
            reasons[row['reason']] = reasons.get(row['reason'], 0) + count
        hour = hours.setdefault(row['hour'], dict(hour=row['hour'], succeeded=0, failed=0, skipped=0, file_bytes=0))
        outcome = 'succeeded' if row['outcome'] == 'test_succeeded' else row['outcome']
        hour[outcome] = hour.get(outcome, 0) + count
        hour['file_bytes'] += int(row['file_bytes'])
    successes = totals['succeeded'] + totals['test_succeeded']
    denominator = successes + totals['failed']
    totals['success_rate'] = round(100 * successes / denominator, 2) if denominator else None
    totals['file_bytes_exact'] = str(totals['file_bytes'])
    return dict(scope=scope, since=since, until=stamp, retention_days=RETENTION_DAYS,
                since_utc=datetime.fromtimestamp(since, timezone.utc).isoformat(),
                coverage='本版本启用后按完成时间累计；不回填历史任务。时间范围按整点对齐。',
                totals=totals, failure_reasons=reasons, hours=list(hours.values()))
