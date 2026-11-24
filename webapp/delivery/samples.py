"""Bounded, disposable copies of real verified test outputs, independent of receipts."""
import json
import os
from pathlib import Path
import shutil
import time

from sqlalchemy import func, select
from werkzeug.exceptions import Conflict, NotFound

from webapp.database import (test_sample_policies as policies, test_samples as samples,
    delivery_artifacts as artifacts, delivery_work, tasks)
from webapp.disk_budget import MIB, GIB, file_lock, safe_path

DEFAULT_POLICY = dict(enabled=True, maximum=3, budget_mb=3072, hours=24, failure_hours=24, revision=0)


def local_id(store):
    return store.fleet.id if store.fleet else 'local'


def policy(db, job):
    row = db.execute(select(policies).where(policies.c.job_id == job)).mappings().first()
    value = {**DEFAULT_POLICY, **({k: v for k, v in row.items() if k != 'job_id'} if row else {})}
    value['enabled'] = bool(value['enabled'])
    return value


def save_policy(store, job, data):
    if type(data.get('enabled')) is not bool:
        raise ValueError('请选择快速测试或保留样本。')
    for key, low, high in [('maximum', 1, 20), ('budget_mb', 16, 102400), ('hours', 1, 168), ('failure_hours', 1, 168)]:
        if type(data.get(key)) is not int or not low <= data[key] <= high:
            raise ValueError(f'{key} 必须为 {low}～{high} 的整数。')
    with store.connect() as db:
        store.db.ensure(db, policies, dict(job_id=job, **{**DEFAULT_POLICY, 'enabled': 1}))
        current = db.execute(select(policies).where(policies.c.job_id == job).with_for_update()).mappings().one()
        if data.get('revision') != current['revision']:
            raise Conflict('留样设置已变化，请刷新后重新修改。')
        values = {key: int(data[key]) for key in DEFAULT_POLICY if key != 'revision'}
        db.execute(policies.update().where(policies.c.job_id == job).values(**values, revision=current['revision'] + 1))
        return policy(db, job)


def public(row):
    result = {key: row[key] for key in ('id', 'job_id', 'task_id', 'instance_id', 'video_id', 'title',
        'size', 'created', 'expires', 'review', 'reviewed_by', 'reviewed_at', 'revision')}
    result['files'] = [{key: value for key, value in item.items() if key not in ('source', 'name')}
        | dict(content_url=f'/api/disk/samples/{row["id"]}/files/{item["id"]}?instance_id={row["instance_id"]}')
        for item in json.loads(row['files'])]
    return result


def task_note(store, task_id, message):
    with store.connect() as db:
        task = db.execute(select(tasks.c.progress).where(tasks.c.id == task_id,
            tasks.c.status.in_(('succeeded', 'failed', 'removed'))).with_for_update()).first()
        if task:
            progress = json.loads(task[0] or '{}')
            progress['sample_retention'] = dict(message=message, time=time.time())
            db.execute(tasks.update().where(tasks.c.id == task_id).values(progress=json.dumps(progress, ensure_ascii=False)))
    store.progress_cache.discard([task_id])


def remove(store, budget, row, reason):
    """Caller owns the sample maintenance lock. Never delete source workspaces."""
    path = safe_path(store.directory / 'test-samples' / row['id'], store.directory / 'test-samples')
    # Keep metadata until deletion succeeds so failed unlink remains retryable.
    with store.connect() as db:
        db.execute(samples.update().where(samples.c.id == row['id']).values(state='deleting'))
    if path.exists():
        shutil.rmtree(path)
    with store.connect() as db:
        db.execute(samples.delete().where(samples.c.id == row['id']))
    task_note(store, row['task_id'], reason)
    with budget.connect() as db:
        budget._set(db, 'sample_cleanup', dict(time=time.time(), bytes=row['size'], reason=reason))


def _sweep(store, budget, *, incoming=None):
    now = time.time()
    with store.connect() as db:
        rows = [dict(r) for r in db.execute(select(samples).where(samples.c.instance_id == local_id(store))
            .order_by(samples.c.created)).mappings()]
        policies_by_job = {r['job_id']: policy(db, r['job_id']) for r in rows}
    pressure = budget._free() < int(budget.config['disk_resume_free_gb']) * GIB
    snapshot = budget.snapshot()
    pressure |= (snapshot['committed_bytes'] >= snapshot['budget_bytes'] or
        snapshot['categories']['test']['committed_bytes'] >= snapshot['categories']['test']['budget_bytes'])
    maximum_bytes = int(budget.config.get('disk_sample_budget_mb', 3072)) * MIB
    total = sum(r['size'] for r in rows)
    retained = len(rows) + int(incoming is not None)
    per_job = {}
    for row in rows:
        amount, count = per_job.get(row['job_id'], (0, 0))
        per_job[row['job_id']] = amount + row['size'], count + 1
    if incoming:
        total += incoming['size']
        amount, count = per_job.get(incoming['job_id'], (0, 0))
        per_job[incoming['job_id']] = amount + incoming['size'], count + 1
    removed = 0
    for row in rows:
        if row['state'] == 'building':
            # A workspace lease will resume this publication after a crash.
            with budget.connect(write=False) as db:
                active_lease = db.execute('SELECT id FROM leases WHERE id=?', (row['id'],)).fetchone()
            if active_lease:
                continue
        p = policies_by_job[row['job_id']]
        amount, count = per_job[row['job_id']]
        reason = ('空间紧张，样本已自动回收。' if pressure else
            '样本已到期，自动清理。' if row['expires'] <= now else
            '已关闭留样，样本自动清理。' if not p['enabled'] and not row['requested'] else
            '已达到留样数量或容量上限，旧样本自动替换。')
        evict = (row['state'] != 'ready' or pressure or (not p['enabled'] and not row['requested']) or row['expires'] <= now or
            total > maximum_bytes or retained > 200 or amount > p['budget_mb'] * MIB or count > p['maximum'])
        if not evict or row['view_until'] > now:
            continue
        try:
            remove(store, budget, row, reason)
        except OSError:
            with budget.connect() as db:
                budget._set(db, 'sample_cleanup', dict(time=now, error='样本文件暂时无法删除，将自动重试；可在实例日志中检查文件权限。'))
            continue
        total -= row['size'];per_job[row['job_id']] = amount - row['size'], count - 1
        retained -= 1
        removed += 1
        snapshot = budget.snapshot(force=True)
        pressure = (budget._free() < int(budget.config['disk_resume_free_gb']) * GIB or
            snapshot['committed_bytes'] >= snapshot['budget_bytes'] or
            snapshot['categories']['test']['committed_bytes'] >= snapshot['categories']['test']['budget_bytes'])
    return total, per_job, pressure, removed


def sweep(store, budget):
    with file_lock(store.directory / 'sample-maintenance.lock') as owned:
        if owned:
            removed = _sweep(store, budget)[3]
            status(store, budget)
            return removed
    return 0


def status(store, budget):
    with store.connect() as db:
        count, amount = db.execute(select(func.count(), func.coalesce(func.sum(samples.c.size), 0)).where(
            samples.c.instance_id == local_id(store), samples.c.state == 'ready')).one()
    with budget.connect() as db:
        budget._set(db, 'sample_status', dict(count=int(count), used_bytes=int(amount), time=time.time()))


def capture(store, budget, lease, task):
    """Move completed files on the same disk; no re-encoding or duplicate traffic.

    Caller owns the stopped workspace lease lock. A durable building row makes
    rename-before-commit recoverable, including a crash between individual files.
    """
    if lease['category'] != 'test' or not task or task['status'] != 'succeeded' or not task.get('batch_id'):
        return
    workspace = safe_path(lease['path'], store.directory / 'test-downloads')
    with file_lock(store.directory / 'sample-maintenance.lock') as owned:
        if not owned:
            raise OSError('样本管理繁忙，稍后继续整理。')
        with store.connect() as db:
            p = policy(db, task['batch_id'])
            if (task.get('progress') or {}).get('_sample_requested'):
                p['enabled'] = True
            previous = db.execute(select(samples).where(samples.c.id == lease['id'])).mappings().first()
            if previous and previous['state'] == 'ready':
                return
            # A building sample already has its manifest. A fast test does not
            # need file discovery at all; neither path should scan receipts.
            outputs = list(db.execute(select(artifacts).where(artifacts.c.task_id == task['id'],
                artifacts.c.job_id == task['batch_id'], artifacts.c.test_mode == 1,
                artifacts.c.instance_id == local_id(store))).mappings()) if not previous and p['enabled'] else []
        if previous:
            candidate = dict(previous)
        else:
            if not p['enabled']:
                task_note(store, task['id'], '快速测试：校验结果已登记，真实文件已按设置清理。')
                return
            files = []
            for output in outputs:
                try:
                    path = safe_path(output['path'], workspace)
                except ValueError:
                    continue  # A retry can reuse receipts from a previous workspace.
                if not path.is_file():
                    continue
                files.append(dict(id=output['id'], kind=output['kind'], language=output['language'],
                    subtitle_source=output['subtitle_source'], title=output['title'], video_id=output['video_id'],
                    size=path.stat().st_size, extension=path.suffix.lower(), name=output['id'] + path.suffix.lower(),
                    source=str(path), storage='test', instance_id=local_id(store), available=True))
            size = sum(f['size'] for f in files)
            if not files:
                return
            cap = min(p['budget_mb'], int(budget.config.get('disk_sample_budget_mb', 3072))) * MIB
            if size > cap:
                task_note(store, task['id'], '样本超过留样容量上限，校验通过后已自动清理。')
                return
            candidate = dict(id=lease['id'], job_id=task['batch_id'], task_id=task['id'],
                instance_id=local_id(store), video_id=task['video_id'], title=task.get('title') or files[0]['title'] or task['video_id'],
                workspace=str(workspace), files=json.dumps(files, ensure_ascii=False), size=size,
                state='building', created=time.time(), expires=time.time() + p['hours'] * 3600,
                view_until=0, review='pending', reviewed_by=None, reviewed_at=None, revision=1)
            candidate['requested'] = int(bool((task.get('progress') or {}).get('_sample_requested')))
            total, amounts, pressure, _ = _sweep(store, budget, incoming=candidate)
            amount, count = amounts.get(task['batch_id'], (size, 1))
            if (pressure or total > int(budget.config.get('disk_sample_budget_mb', 3072)) * MIB
                    or sum(value[1] for value in amounts.values()) > 200
                    or amount > p['budget_mb'] * MIB or count > p['maximum']):
                task_note(store, task['id'], '空间紧张或已有样本正在查看，本次未留样；校验结果保留。')
                return
            with store.connect() as db:
                db.execute(samples.insert().values(**candidate))
        folder = safe_path(store.directory / 'test-samples' / candidate['id'], store.directory / 'test-samples')
        folder.mkdir(parents=True, exist_ok=True)
        for item in json.loads(candidate['files']):
            source = safe_path(item['source'], workspace)
            target = safe_path(folder / item['name'], folder)
            if target.is_file() and target.stat().st_size == item['size']:
                continue
            if not source.is_file():
                remove(store, budget, candidate, '留样时源文件已不可用，本次未保留预览；测试回执仍然保留。')
                return
            os.replace(source, target)
        with store.connect() as db:
            db.execute(samples.update().where(samples.c.id == candidate['id']).values(state='ready'))
        task_note(store, task['id'], '真实测试文件已留样，可在任务中心查看；空间紧张时可能提前回收。')
        status(store, budget)


def expire_failed(store, budget, lease, task):
    """Only terminal, unlocked test plans with settled receipts may be abandoned."""
    if lease['category'] != 'test' or not task or task['status'] not in ('failed', 'interrupted'):
        return
    from datetime import datetime
    with store.connect() as db:
        p = policy(db, task['batch_id'])
        work = db.execute(select(delivery_work).where(delivery_work.c.task_id == task['id'],
            delivery_work.c.disk_lease == lease['id'], delivery_work.c.instance_id == local_id(store))).mappings().first()
        if not work:
            return
        finished = datetime.fromisoformat(task['finished_at']).timestamp() if task['finished_at'] else time.time()
        pressure = budget._free() < int(budget.config['disk_resume_free_gb']) * GIB
        snapshot = budget.snapshot()
        pressure |= (snapshot['committed_bytes'] >= snapshot['budget_bytes'] or
            snapshot['categories']['test']['committed_bytes'] >= snapshot['categories']['test']['budget_bytes'] or
            snapshot['waiting_tasks'] > 0)
        if not pressure and time.time() - finished < p['failure_hours'] * 3600:
            return
        db.execute(delivery_work.delete().where(delivery_work.c.task_id == task['id']))
    task_note(store, task['id'], '失败测试临时文件已按空间保护或保留期限自动清理；失败原因和已登记统计保留。')


def action(store, budget, sample_id, data, actor=''):
    with file_lock(store.directory / 'sample-maintenance.lock') as owned:
        if not owned:
            raise Conflict('样本正在整理，请稍后刷新。')
        with store.connect() as db:
            row = db.execute(select(samples).where(samples.c.id == sample_id,
                samples.c.job_id == data.get('job_id'), samples.c.instance_id == local_id(store)).with_for_update()).mappings().first()
            if not row or row['state'] != 'ready':
                raise NotFound('样本已清理或不属于当前实例。')
            if data.get('revision') != row['revision']:
                raise Conflict('样本状态已变化，请刷新后操作。')
            operation = data.get('action')
            values = dict(revision=row['revision'] + 1)
            if operation == 'extend':
                values['expires'] = time.time() + 86400
            elif operation in ('accept', 'reject'):
                values.update(review='accepted' if operation == 'accept' else 'rejected',
                    reviewed_by=actor, reviewed_at=time.time())
                task = db.execute(select(tasks.c.progress).where(tasks.c.id == row['task_id']).with_for_update()).first()
                if task:
                    progress = json.loads(task[0] or '{}')
                    progress['sample_review'] = dict(status=values['review'], actor=actor, time=values['reviewed_at'])
                    db.execute(tasks.update().where(tasks.c.id == row['task_id']).values(progress=json.dumps(progress, ensure_ascii=False)))
                from webapp.jobs import event
                event(store, db, row['job_id'], f'样本 {row["video_id"]} 人工验收：' + ('通过。' if operation == 'accept' else '发现问题。'))
            elif operation == 'delete':
                if row['view_until'] > time.time():
                    raise Conflict('样本正在查看，请关闭预览后稍候再清理。')
            else:
                raise ValueError('请选择验收、延长保留或清理。')
            if operation != 'delete':
                db.execute(samples.update().where(samples.c.id == row['id']).values(**values))
        if operation == 'delete':
            remove(store, budget, row, '已手动清理样本，测试结果与统计保留。')
    budget._refresh_usage(force=True)
    status(store, budget)
    store.progress_cache.discard([row['task_id']])
    return dict(message='样本已清理。' if operation == 'delete' else '已保存；空间保护仍然生效。')
