"""Remove executor-owned material history without deleting download deduplication."""
from collections import Counter
import json
import re

from sqlalchemy import func, or_, select

from webapp.database import (tasks, counters, snapshots, delivery_artifacts, delivery_requirements,
    delivery_runs, delivery_totals, deliveries, test_files, download_sessions, session_tasks,
    method_tasks, method_totals, provider_tasks, test_totals, download_lists, settings,
    download_permits, job_removed, job_imports, imports, runtime_hours)
from webapp.statistics import COUNTERS, OUTCOMES


def rebuild_session(db, identity):
    states = Counter(dict(db.execute(select(session_tasks.c.status, func.count()).where(
        session_tasks.c.session_id == identity).group_by(session_tasks.c.status)).all()))
    sizes = {}
    for table, prefix in ((deliveries, ''), (test_files, 'test_')):
        count, size = db.execute(select(func.count(), func.coalesce(func.sum(table.c.size), 0))
            .where(table.c.session_id == identity)).one()
        sizes[prefix + 'file_count'], sizes[prefix + 'file_bytes'] = count, size
    if not states and not any(sizes.values()):
        for table in (method_tasks, provider_tasks, method_totals, test_totals, download_sessions):
            column = table.c.id if table is download_sessions else table.c.session_id
            db.execute(table.delete().where(column == identity))
        db.execute(settings.update().where(settings.c.key.startswith('download_statistics_active:'),
            settings.c.value == identity).values(value=''))
        return
    values = {key: states.get(key, 0) for key in OUTCOMES}
    values['attempted'] = sum(states.values())
    values.update(file_count=sizes['file_count'], file_bytes=sizes['file_bytes'])
    db.execute(download_sessions.update().where(download_sessions.c.id == identity).values(**values))
    db.execute(test_totals.update().where(test_totals.c.session_id == identity).values(
        test_file_count=sizes['test_file_count'], test_file_bytes=sizes['test_file_bytes']))
    db.execute(method_totals.delete().where(method_totals.c.session_id == identity))
    for method in db.execute(select(provider_tasks.c.method).where(
            provider_tasks.c.session_id == identity).distinct()).scalars().all():
        records = list(db.execute(select(provider_tasks).where(provider_tasks.c.session_id == identity,
            provider_tasks.c.method == method)).mappings())
        status_counts = Counter(row['status'] for row in records)
        totals = {key: status_counts.get(key, 0) for key in COUNTERS}
        totals.update(attempted=len(records), fallback_tasks=sum(row['fallback_used'] for row in records))
        for table, prefix in ((deliveries, ''), (test_files, 'test_')):
            count, size = db.execute(select(func.count(), func.coalesce(func.sum(table.c.size), 0))
                .select_from(table.join(method_tasks, (table.c.session_id == method_tasks.c.session_id) &
                    (table.c.task_id == method_tasks.c.task_id)))
                .where(table.c.session_id == identity, method_tasks.c.method == method)).one()
            totals[prefix + 'file_count'], totals[prefix + 'file_bytes'] = count, size
        db.execute(method_totals.insert().values(session_id=identity, method=method, **totals))


def reset_records(store, local_id, remove_log):
    condition = tasks.c.action.in_(('download', 'import', 'index'))
    if store.fleet:
        condition &= or_(tasks.c.instance_id == local_id, tasks.c.target_instance_id == local_id)
    with store.connect() as db:
        if db.execute(select(tasks.c.id).where(condition, tasks.c.status == 'running').limit(1)).first():
            raise RuntimeError('仍有下载或导入任务运行，清理已停止。')
        artifact_rows = list(db.execute(select(delivery_artifacts.c.id, delivery_artifacts.c.task_id,
            delivery_artifacts.c.job_id).where(
            delivery_artifacts.c.instance_id == local_id)).mappings())
        orphan_ids = {row['task_id'] for row in artifact_rows}
        candidates = list(orphan_ids)
        for start in range(0, len(candidates), 500):
            orphan_ids.difference_update(db.execute(select(tasks.c.id).where(
                tasks.c.id.in_(candidates[start:start + 500]))).scalars())
    # Enumerate actual log files rather than stat three nonexistent paths for
    # every queued task. Queues can contain millions of never-started entries.
    log_ids = sorted({path.name.split('.')[0] for path in (store.directory / 'logs').rglob('*')
                      if re.fullmatch(r'[a-f0-9]{32}\.(?:log|offset(?:\.tmp)?)', path.name)})
    for start in range(0, len(log_ids), 500):
        candidates = log_ids[start:start + 500]
        with store.connect() as db:
            owned = set(db.execute(select(tasks.c.id).where(condition, tasks.c.id.in_(candidates))).scalars())
        for identity in owned | (orphan_ids & set(candidates)):
            path = store.log_path(identity, create=False)
            for candidate in (path, path.with_suffix('.offset'), path.with_suffix('.offset.tmp')):
                remove_log(candidate)

    jobs = {row['job_id'] for row in artifact_rows}
    session_ids = set()
    snapshot_ids = set()
    deleted_tasks = cursor = 0
    with store.connect() as db:
        if store.db.sqlite:
            db.exec_driver_sql('BEGIN IMMEDIATE')
        active = store.statistics._active(db, local_id)
        if active:
            session_ids.add(active)
        while True:
            batch = list(db.execute(select(tasks.c.id, tasks.c.seq, tasks.c.action, tasks.c.status,
                tasks.c.batch_id, tasks.c.payload).where(condition, tasks.c.seq > cursor)
                .order_by(tasks.c.seq).limit(500)).mappings())
            if not batch:
                break
            cursor = batch[-1]['seq']
            ids = [row['id'] for row in batch]
            jobs.update(row['batch_id'] for row in batch if row['batch_id'])
            payloads = [json.loads(row['payload']) for row in batch]
            snapshot_ids.update(payload.get('snapshot_id') for payload in payloads)
            # Counters must change with the physical task deletion, or workbench
            # queues would continue to report tasks that no longer exist.
            changes = Counter()
            for row in batch:
                changes[('action:' + row['action'], row['status'])] -= 1
                if row['batch_id']:
                    changes[('batch:' + row['batch_id'], row['status'])] -= 1
            for (scope, status), delta in sorted(changes.items()):
                store._counter(db, scope, status, delta)
            for table in (session_tasks, deliveries, test_files):
                session_ids.update(db.execute(select(table.c.session_id).where(table.c.task_id.in_(ids))).scalars())
            import_ids = list(db.execute(select(job_imports.c.id).where(job_imports.c.task_id.in_(ids))).scalars())
            import_ids.extend(payload['import_id'] for payload in payloads if payload.get('import_id'))
            if import_ids:
                db.execute(imports.delete().where(imports.c.id.in_(import_ids)))
            for table in (session_tasks, deliveries, test_files, method_tasks, provider_tasks,
                          delivery_requirements, delivery_runs, download_permits, job_removed, job_imports):
                db.execute(table.delete().where(table.c.task_id.in_(ids)))
            db.execute(tasks.delete().where(tasks.c.id.in_(ids)))
            deleted_tasks += len(batch)
            store.progress_cache.discard(ids)
            for identity in ids:
                store._progress_checkpoints.pop(identity, None)
        orphan_ids = sorted(orphan_ids)
        for start in range(0, len(orphan_ids), 500):
            ids = orphan_ids[start:start + 500]
            for table in (session_tasks, deliveries, test_files):
                session_ids.update(db.execute(select(table.c.session_id).where(table.c.task_id.in_(ids))).scalars())
            for table in (session_tasks, deliveries, test_files, method_tasks, provider_tasks,
                          delivery_requirements, delivery_runs, download_permits, job_removed):
                db.execute(table.delete().where(table.c.task_id.in_(ids)))
        for start in range(0, len(artifact_rows), 500):
            ids = [row['id'] for row in artifact_rows[start:start + 500]]
            for table in (deliveries, test_files):
                session_ids.update(db.execute(select(table.c.session_id).where(table.c.id.in_(ids))).scalars())
                db.execute(table.delete().where(table.c.id.in_(ids)))
            db.execute(delivery_requirements.update().where(delivery_requirements.c.artifact_id.in_(ids)).values(
                artifact_id=None, status='missing', error='素材已清空。'))
            db.execute(delivery_artifacts.delete().where(delivery_artifacts.c.id.in_(ids)))
        for identity in session_ids:
            rebuild_session(db, identity)
        for job in jobs:
            values = {}
            for table, prefix in ((deliveries, ''), (test_files, 'test_')):
                count, size = db.execute(select(func.count(), func.coalesce(func.sum(table.c.size), 0))
                    .select_from(table.join(tasks, table.c.task_id == tasks.c.id))
                    .where(tasks.c.batch_id == job)).one()
                values[prefix + 'file_count'], values[prefix + 'file_bytes'] = count, size
            for status in ('skipped', 'test_succeeded'):
                values[status] = db.execute(select(func.count()).select_from(session_tasks.join(
                    tasks, session_tasks.c.task_id == tasks.c.id)).where(tasks.c.batch_id == job,
                        session_tasks.c.status == status)).scalar_one()
            db.execute(download_lists.update().where(download_lists.c.id == job).values(**values))
            db.execute(delivery_totals.delete().where(delivery_totals.c.job_id == job))
            for kind, test, count, size in db.execute(select(delivery_artifacts.c.kind,
                    delivery_artifacts.c.test_mode, func.count(), func.sum(delivery_artifacts.c.size))
                    .where(delivery_artifacts.c.job_id == job)
                    .group_by(delivery_artifacts.c.kind, delivery_artifacts.c.test_mode)):
                db.execute(delivery_totals.insert().values(job_id=job, kind=kind, test_mode=test,
                    file_count=count, file_bytes=size))
            for status, test, count in db.execute(select(delivery_runs.c.status, delivery_runs.c.test_mode,
                    func.count()).where(delivery_runs.c.job_id == job)
                    .group_by(delivery_runs.c.status, delivery_runs.c.test_mode)):
                db.execute(delivery_totals.insert().values(job_id=job, kind='@' + status,
                    test_mode=test, file_count=count, file_bytes=0))
        for identity in snapshot_ids - {None}:
            if not db.execute(select(tasks.c.id).where(tasks.c.payload.contains(identity)).limit(1)).first():
                db.execute(snapshots.delete().where(snapshots.c.id == identity))
        db.execute(runtime_hours.delete().where(runtime_hours.c.scope.in_((local_id, 'instance:' + local_id))))
        db.execute(counters.delete().where(counters.c.scope == 'instance:' + local_id))
        db.execute(counters.delete().where(counters.c.value == 0))
    store.statistics._dashboard_cache.clear()
    store.statistics._history_cache.clear()
    return {'deleted_tasks': deleted_tasks, 'deleted_artifacts': len(artifact_rows)}
