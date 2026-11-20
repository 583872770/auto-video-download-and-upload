"""Resolve failed test work without deleting statistics or touching active writers."""
import hashlib
import json
import re
import shutil
import time

from sqlalchemy import select
from werkzeug.exceptions import Conflict

from webapp.database import delivery_work, tasks, download_lists, settings
from webapp.disk_budget import DiskBudget, alive, file_lock, safe_path
from webapp.diagnostics import safe_text


def version(task, work):
    value = [task['id'], task['finished_at'], task['attempts'], task['progress'], work['created_at'], work['attempts']]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def list_failed(store, config, cursor=None):
    if cursor and not re.fullmatch(r'[a-f0-9]{32}', cursor):
        raise ValueError('清理列表分页参数无效。')
    budget = DiskBudget(store.directory, config)
    with budget.connect(write=False) as db:
        leases = {r['id']: dict(r) for r in db.execute("SELECT * FROM leases WHERE category='test'")}
    identity = store.fleet.id if store.fleet else 'local'
    # Start from the small instance-local work table, never scan task payloads/history.
    query = select(tasks.c.id, tasks.c.video_id, tasks.c.title, tasks.c.batch_id,
        tasks.c.error, tasks.c.progress, tasks.c.attempts, tasks.c.finished_at,
        delivery_work.c.created_at, delivery_work.c.attempts.label('local_attempts'),
        delivery_work.c.disk_lease, download_lists.c.name.label('job_name')).select_from(
            delivery_work.join(tasks, tasks.c.id == delivery_work.c.task_id).outerjoin(
                download_lists, download_lists.c.id == tasks.c.batch_id)).where(
        delivery_work.c.instance_id == identity, tasks.c.status == 'failed',
        delivery_work.c.disk_lease.in_(list(leases) or ['']))
    if cursor:
        query = query.where(tasks.c.id > cursor)
    with store.connect() as db:
        found = db.execute(query.order_by(tasks.c.id).limit(51)).mappings().all()
    items = []
    for row in found[:50]:
        lease = leases[row['disk_lease']]
        progress = json.loads(row['progress'] or '{}')
        recommendation = progress.get('local_recovery', 'retry')
        items.append(dict(task_id=row['id'], video_id=row['video_id'], title=row['title'],
            job_id=row['batch_id'], job_name=row['job_name'], failed_at=row['finished_at'],
            error=safe_text(row['error'] or '本地处理未完成。')[:1000],
            bytes=lease['measured'], attempts=row['local_attempts'],
            recommendation=recommendation,
            version=version(row, dict(created_at=row['created_at'], attempts=row['local_attempts']))))
    return dict(items=items, next_cursor=found[49]['id'] if len(found)>50 else None)


def resolve(store, config, task_id, action, expected_version):
    if action not in ('retry', 'redownload', 'discard'):
        raise ValueError('请选择重试处理、重新下载或放弃测试并清理。')
    if not re.fullmatch(r'[a-f0-9]{32}', str(task_id)) or not re.fullmatch(r'[a-f0-9]{64}', str(expected_version)):
        raise ValueError('请刷新清理列表后选择任务。')
    identity = store.fleet.id if store.fleet else 'local'
    budget = DiskBudget(store.directory, config)
    with store.connect() as db:
        work = db.execute(select(delivery_work).where(delivery_work.c.task_id == task_id,
            delivery_work.c.instance_id == identity)).mappings().first()
    if not work:
        raise Conflict('该处理计划已处理或不属于当前实例，请刷新列表。')
    work = dict(work)
    with budget.connect(write=False) as db:
        lease = db.execute("SELECT * FROM leases WHERE id=? AND task=? AND category='test'",
                           (work['disk_lease'], task_id)).fetchone()
    if not lease or lease['path'] != work['workspace'] or json.loads(work['plan'])['config'].get('storage_mode') != 'test':
        raise Conflict('只能处理当前实例已确认归属的测试临时文件。')
    path = safe_path(lease['path'], store.directory / 'test-downloads')
    with file_lock(store.directory / 'disk-cleanup.lock') as cleaning:
        if not cleaning:
            raise Conflict('后台正在清理，请稍后重试。')
        with file_lock(store.directory / 'disk-locks' / (lease['id'] + '.lock')) as owned:
            if not owned or alive(json.loads(lease['owner'])) or any(alive(p) for p in json.loads(lease['children'])):
                raise Conflict('文件仍有进程使用，请稍后刷新，不能清理或重新入队。')
            receipts = path / '.completed-files.json'
            if action != 'retry' and receipts.exists() and not budget.receipts_recorded(store, task_id, receipts):
                raise Conflict('已有产物尚未完成统计登记，请先选择重试本地处理，登记后再清理。')
            with store.connect() as db:
                if store.db.sqlite:
                    db.exec_driver_sql('BEGIN IMMEDIATE')
                if store.fleet:
                    db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget').with_for_update()).first()
                task = db.execute(select(tasks).where(tasks.c.id == task_id).with_for_update()).mappings().first()
                current = db.execute(select(delivery_work).where(delivery_work.c.task_id == task_id,
                    delivery_work.c.instance_id == identity).with_for_update()).mappings().first()
                if (not task or task['status'] != 'failed' or not current
                        or current['disk_lease'] != lease['id'] or version(task, current) != expected_version):
                    raise Conflict('任务状态已变化，本次操作未执行，请刷新列表。')
                progress = json.loads(task['progress'] or '{}')
                stamp = store.fleet.clock(db) if store.fleet else time.time()
                progress['retention_resolution'] = dict(action=action, time=stamp)
                if action == 'retry' and progress.get('local_recovery') == 'redownload':
                    raise Conflict('已检测到源文件损坏或轨道缺失，请选择重新下载，不能重复转换同一文件。')
                if action in ('retry', 'redownload'):
                    payload = store.decode(task, private=True, db=db)['payload']
                    history = payload.get('_retry_history', [])
                    payload['_retry_history'] = [*history[-7:], dict(retried_at=stamp,
                        finished_at=task['finished_at'], attempts=task['attempts'], error=task['error'],
                        action=action, failure=progress.get('last_failure'))]
                    payload['_manual_retry'] = True
                    store._transition(db, task, 'queued')
                    progress.update(stage='waiting_processing' if action == 'retry' else 'queued',
                        wait_reason='等待本地处理。' if action == 'retry' else '等待重新下载缺项。', retry_at=None)
                    progress.pop('local_recovery', None)
                    db.execute(tasks.update().where(tasks.c.id == task_id).values(status='queued',
                        finished_at=None, attempts=0, available_at=0, error=None, worker_id=None, lease_until=None,
                        payload=store._pack(db, payload), progress=json.dumps(progress, ensure_ascii=False)))
                    from webapp.notifications import retry_task
                    retry_task(store, db, task)
                else:
                    db.execute(tasks.update().where(tasks.c.id == task_id).values(
                        progress=json.dumps(progress, ensure_ascii=False)))
                if action == 'retry':
                    db.execute(delivery_work.update().where(delivery_work.c.task_id == task_id).values(attempts=0))
                else:
                    db.execute(delivery_work.delete().where(delivery_work.c.task_id == task_id))
            store.progress_cache.discard([task_id])
            # No database transaction is held during filesystem deletion. If interrupted,
            # the existing sweeper sees the retired plan and retries safe cleanup.
            removed = False
            if action != 'retry':
                try:
                    if path.exists():
                        shutil.rmtree(path)
                    with budget.connect() as db:
                        db.execute('DELETE FROM leases WHERE id=?', (lease['id'],))
                    removed = True
                except OSError:
                    pass
    budget._refresh_usage(force=True)
    pending_cleanup = action != 'retry' and not removed
    budget._retention_health(store, int(pending_cleanup),
        ['文件删除暂未完成，后台将继续安全清理。'] if pending_cleanup else [])
    budget.publish(store, budget.snapshot(force=True))
    if action == 'retry':
        from webapp.worker.idle import notify_processing
        notify_processing(store.directory)
    messages = dict(retry='已重新排队本地处理，保留已下载文件，不重新请求源站。',
        redownload='已放弃旧处理计划并重新排队下载；已登记的测试产物与统计保留，只补缺项。',
        discard='已放弃本次测试的剩余处理，失败记录与已登记统计保留。')
    message = messages[action]
    if action != 'retry':
        message += '临时文件已清理。' if removed else '文件删除暂未完成，后台将继续安全清理，请刷新查看。'
    else:
        message += '处理再次失败时会保留文件，并在此列出。'
    if action != 'discard':
        message += '任务和实例需处于运行状态，且该任务已被选中，才会开始；本操作不会自动启动已暂停的任务。'
    return dict(message=message, files_removed=removed)
