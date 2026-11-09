"""Pace manual replays without erasing the previous attempt's evidence."""
import json
import time
from collections import Counter

from sqlalchemy import select, func

from webapp.cookie_policy import retry_interval
from webapp.database import tasks


def requeue_failed(store, db, identity, config):
    stamp = store.fleet.clock(db) if store.fleet else time.time()
    pooled = bool(config.get('youtube_cookie_pool'))
    spacing = retry_interval(config, [{}] * len(set(config['youtube_cookie_pool']))) if pooled else 0
    # Existing queued release times survive repeated clicks and process restarts;
    # removed/completed batches cannot leave a stale cursor delaying new work.
    tail = db.execute(select(func.max(tasks.c.available_at)).where(tasks.c.batch_id == identity,
        tasks.c.action == 'download', tasks.c.status == 'queued', tasks.c.available_at > stamp)).scalar()
    cursor = max(stamp, float(tail) + spacing if tail else 0)
    count, reasons = 0, Counter()
    while True:
        rows = db.execute(select(tasks).where(tasks.c.batch_id == identity,
            tasks.c.action == 'download', tasks.c.status.in_(('failed', 'interrupted')))
            .order_by(tasks.c.seq).limit(500).with_for_update()).mappings().all()
        if not rows:
            break
        for task in rows:
            payload = store.decode(task, private=True, db=db)['payload']
            previous = json.loads(task['progress'] or '{}')
            failure = previous.get('last_failure') or {}
            history = payload.get('_retry_history', [])
            payload['_retry_history'] = [*history[-7:], dict(retried_at=stamp,
                finished_at=task['finished_at'], attempts=task['attempts'],
                error=(task['error'] or '')[:1000], failure=failure)]
            payload['_manual_retry'] = True
            reasons[str(failure.get('kind') or 'unclassified')] += 1
            # Terminal account-limit failures retain their original session too.
            profile = failure.get('profile_id') or previous.get('cookie_profile_id')
            extra = {}
            if profile and failure.get('kind') in ('account_rate_limit', 'request_rate_limit'):
                payload.setdefault('_retry_original_target', task.get('target_instance_id'))
                payload.update(_retry_cookie_profile_id=profile,
                    _retry_cookie_instance_id=task.get('instance_id') or 'local')
                if task.get('instance_id'):
                    extra['target_instance_id'] = task['instance_id']
            store._transition(db, task, 'queued')
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='queued', attempts=0,
                error=None, finished_at=None, progress=None, available_at=cursor if spacing else 0,
                worker_id=None, lease_until=None, payload=store._pack(db, payload), **extra))
            cursor += spacing
            count += 1
    return f'重新入队失败视频：{count} 个；' + (
        f'分批放行，初始间隔 {spacing:.1f} 秒；重试启动占允许速率最多 20%；' if spacing else '') + (
        '保留原账号限制和最近 8 轮失败记录；分类：' + json.dumps(dict(reasons), ensure_ascii=False))
