"""Bounded log retention; media files are never removed by maintenance."""
from datetime import datetime, timedelta
import time

from sqlalchemy import select

from webapp.database import tasks


def trim_log(store, task_id, max_mb=10):
    path = store.log_path(task_id)
    maximum = int(max_mb) * 1024 ** 2
    if not path.is_file() or path.stat().st_size <= maximum:
        return
    with path.open('rb') as source:
        source.seek(-maximum, 2)
        tail = source.read(maximum)
    with path.open('wb') as target:
        target.write(b'[Earlier log output removed by retention policy]\n')
        target.write(tail)


def cleanup_logs(store, days, limit=500):
    if not days:
        return 0
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec='seconds')
    with store.connect() as db:
        ids = db.execute(select(tasks.c.id).where(tasks.c.logs_pruned == 0, tasks.c.finished_at < cutoff,
                         tasks.c.status.in_(('succeeded', 'failed', 'interrupted')))
                         .order_by(tasks.c.finished_at).limit(limit)).scalars().all()
    removed = 0
    for task_id in ids:
        path = store.log_path(task_id)
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    if ids:
        with store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id.in_(ids)).values(logs_pruned=1))
    return removed
