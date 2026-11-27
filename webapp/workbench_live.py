"""Display-only workbench snapshots. The task lease stays in the durable store."""
import hashlib
import math
import time

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from webapp.database import counters, delivery_work, notifications, notification_tasks, settings, tasks
from webapp.diagnostics import safe_text
from webapp.wait_diagnostics import task_wait


STATES = ('downloading', 'pending_transfer', 'pending_start', 'idle')
QUEUE = ('processing', 'disk', 'retry', 'ready')


def worker_view(worker, sample, stamp):
    slot = str(worker['slot']).rsplit(':', 1)[-1]
    processing = slot.startswith('processing-')
    progress = {}
    if (worker.get('task_id') and sample and worker.get('attempt') and worker['attempt'] == sample.get('attempt')
            and 0 <= stamp - sample.get('updated_at', 0) < 120):
        progress = sample.get('progress') or {}
    stage = progress.get('stage') or worker['state']
    wait = task_wait(progress, stamp=stamp)
    if stage == 'waiting_start' or worker['state'] in ('disk_wait', 'paused'):
        detail = worker.get('wait') or {}
        wait = {'label': safe_text(worker.get('message') or '等待执行条件'),
                'until': detail.get('until', worker.get('until'))}
        if detail:
            wait.update(code=detail['code'], detail=safe_text(detail['detail']))
        elif stage == 'waiting_start':
            wait['detail'] = '该进程尚未领取视频。启动条件恢复后会自动尝试领取。'
    if worker['state'] == 'idle':
        state = 'idle'
    elif stage == 'downloading' and progress.get('network_phase') == 'transferring':
        state = 'downloading'
    elif stage in ('waiting_request', 'waiting_connection', 'extracting', 'downloading') or (
            stage == 'waiting_transfer' and progress.get('network_seconds', {}).get('transferring', 0) > 0):
        state = 'pending_transfer'
    else:
        state = 'pending_start'
    live_progress = {key: progress[key] for key in ('percent', 'current_downloaded_bytes', 'current_total_bytes',
        'current_total_estimated', 'speed', 'eta') if key in progress}
    if state != 'downloading' or not 0 <= stamp - (progress.get('last_bytes_at') or 0) <= 10:
        live_progress.update(speed=None, eta=None)
    return dict(slot=slot, number=int(slot.rsplit('-', 1)[-1]) + 1, processing=processing, state=state,
                stage=stage, heartbeat=worker['heartbeat'], id=worker.get('task_id'),
                video_id=worker.get('video_id'), title=progress.get('title') or worker.get('title'),
                account=progress.get('cookie_profile_name'), output=progress.get('output_kind'),
                language=progress.get('output_language'), progress=live_progress, wait=wait,
                phase_started_at=progress.get('network_phase_started_at'))


def live_snapshot(cache, identity):
    workers = cache.read_workers()
    if workers is None:
        return None
    stamp = time.time()
    prefix = hashlib.sha256(identity.encode()).hexdigest()[:16] + ':' if identity != 'local' else ''
    latest = {}
    for worker in sorted(workers, key=lambda row: row.get('heartbeat', 0), reverse=True):
        name = str(worker.get('slot', ''))
        if not name.startswith(prefix):
            continue
        slot = name[len(prefix):]
        if ((slot.isdecimal() or (slot.startswith('processing-') and slot[11:].isdecimal()))
                and 0 <= stamp - worker.get('heartbeat', 0) < 30):
            if worker.get('action') in (None, 'download'):
                latest.setdefault(slot, worker)
    samples = cache.read_workbench(identity, list(latest.values()))
    if samples is None:
        return None
    summary, progress = samples
    if not summary or not 0 <= stamp - summary.get('updated_at', 0) < 60:
        return None
    rows = sorted((worker_view(worker, progress.get(worker.get('task_id')), stamp)
                   for worker in latest.values()), key=lambda row: (row['processing'], row['number']))
    counts = dict.fromkeys(STATES, 0)
    measured = []
    for row in rows:
        if not row['processing']:
            counts[row['state']] += 1
            speed = row['progress'].get('speed')
            if row['state'] == 'downloading' and isinstance(speed, (int, float)) and math.isfinite(speed) and speed >= 0:
                measured.append(speed)
    return dict(time=stamp, rows=rows, counts=counts, queue=summary['queue'], queue_at=summary['updated_at'],
                paused=summary['paused'], transfer_summary=dict(time=stamp, transferring=counts['downloading'],
                measured=len(measured), speed=sum(measured)))


def selected_job(store, db, identity):
    key = 'workbench_job:' + identity if store.fleet else 'selected_download_job'
    return db.execute(select(settings.c.value).where(settings.c.key == key)).scalar_one_or_none()


def queue_queries(identity, batch, stamp):
    pending = (tasks.c.status == 'queued', tasks.c.action == 'download')
    processing = delivery_work.join(tasks, tasks.c.id == delivery_work.c.task_id)
    local = select(tasks).select_from(processing).where(*pending, delivery_work.c.instance_id == identity)
    job_tasks = (*pending, tasks.c.batch_id == batch)
    no_processing = ~select(delivery_work.c.task_id).where(delivery_work.c.task_id == tasks.c.id).exists()
    incidents = notifications.join(notification_tasks, notifications.c.id == notification_tasks.c.group_id)
    disk_tasks = (notifications.c.job_id == batch, notifications.c.code == 'disk', notification_tasks.c.state != 'resolved')
    disk = select(tasks).select_from(incidents.join(tasks, tasks.c.id == notification_tasks.c.task_id))
    not_disk = ~select(notification_tasks.c.task_id).select_from(incidents).where(
        notification_tasks.c.task_id == tasks.c.id, *disk_tasks).exists()
    download = select(tasks).where(*job_tasks, no_processing, not_disk)
    return {
        'processing': local,
        'disk': disk.where(*job_tasks, *disk_tasks, no_processing),
        'retry': download.where(tasks.c.available_at > stamp),
        'ready': download.where(tasks.c.available_at <= stamp),
    }


def publish_queue(store):
    cache = store.progress_cache
    if not cache.enabled or time.monotonic() < cache.retry_at:
        return
    identity = store.fleet.id if store.fleet else 'local'
    stamp = time.time()
    counts = dict.fromkeys(QUEUE, 0)
    with store.connect() as db:
        batch = selected_job(store, db, identity)
        paused = store.claims_paused(db)
        queries = queue_queries(identity, batch, stamp)
        def count(statement):
            query = statement.with_only_columns(func.count(), maintain_column_froms=True)
            try:
                return db.execute(query.prefix_with('/*+ MAX_EXECUTION_TIME(1500) */', dialect='mysql')).scalar_one()
            except OperationalError as error:
                if error.orig.args[0] != 3024:
                    raise
                return None

        counts['processing'] = count(queries['processing'])
        if batch:
            counts['disk'] = count(queries['disk'])
            counts['retry'] = count(queries['retry'].with_hint(tasks, 'FORCE INDEX (ix_tasks_dispatch)', dialect_name='mysql'))
            total = db.execute(select(counters.c.value).where(counters.c.scope == 'batch:' + batch,
                counters.c.status == 'queued')).scalar_one_or_none() or 0
            processing = count(select(delivery_work.c.task_id).select_from(delivery_work.join(tasks,
                tasks.c.id == delivery_work.c.task_id)).where(tasks.c.batch_id == batch, tasks.c.status == 'queued'))
            deductions = (processing, counts['disk'], counts['retry'])
            counts['ready'] = (None if None in deductions else max(0, int(total) - sum(deductions)))
    cache.publish_workbench(identity, dict(queue=counts, paused=paused, updated_at=time.time()))


def queue_details(store, identity, kind, cursor):
    stamp = time.time()
    with store.connect() as db:
        batch = selected_job(store, db, identity)
        queries = queue_queries(identity, batch, stamp)
        if kind != 'processing' and not batch:
            return dict(items=[], next_cursor=None)
        statement = queries[kind]
        if kind == 'ready':
            statement = statement.with_hint(tasks, 'FORCE INDEX (ix_tasks_batch_ready)', dialect_name='mysql')
        elif kind == 'retry':
            statement = statement.with_hint(tasks, 'FORCE INDEX (ix_tasks_dispatch)', dialect_name='mysql')
        rows = db.execute(statement.with_only_columns(tasks.c.id, tasks.c.video_id, tasks.c.title,
            tasks.c.error, tasks.c.available_at, tasks.c.seq).where(tasks.c.seq > cursor)
            .order_by(tasks.c.seq).limit(21)).mappings().all()
    items = [dict(id=row['id'], video_id=row['video_id'], title=row['title'],
                  reason=safe_text(row['error'] or '')[:300], until=row['available_at'] if row['available_at'] > stamp else None)
             for row in rows[:20]]
    return dict(items=items, next_cursor=rows[19]['seq'] if len(rows) > 20 else None)
