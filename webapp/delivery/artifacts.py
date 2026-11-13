"""Fenced, incremental output state; existing ledgers remain billing receipts."""
import json
from pathlib import Path
from sqlalchemy import select
from webapp.database import (delivery_contracts as contracts, delivery_runs as runs,
    delivery_requirements as requirements, delivery_artifacts as artifacts, delivery_totals as totals)
from webapp.delivery.contracts import normalize, packed, digest, variant, summary
from webapp.statistics import stamp


def current(db, job):
    row = db.execute(select(contracts).where(contracts.c.job_id == job).order_by(contracts.c.version.desc()).limit(1)).mappings().first()
    return {'version': row['version'], 'spec': json.loads(row['spec'])} if row else None


def save_contract(db, job, spec):
    spec = normalize(spec)
    old = current(db, job)
    if old and old['spec'] == spec:
        return old
    version = old['version'] + 1 if old else 1
    db.execute(contracts.insert().values(job_id=job, version=version, spec=packed(spec), created_at=stamp()))
    return {'version': version, 'spec': spec}


def count(store, db, job, kind, test, files=0, size=0):
    store.db.ensure(db, totals, dict(job_id=job, kind=kind, test_mode=int(test), file_count=0, file_bytes=0))
    db.execute(totals.update().where(totals.c.job_id == job, totals.c.kind == kind,
        totals.c.test_mode == int(test)).values(file_count=totals.c.file_count + files, file_bytes=totals.c.file_bytes + size))


def state(store, db, run, value):
    if run['status'] == value:
        return
    count(store, db, run['job_id'], '@' + run['status'], run['test_mode'], -1)
    count(store, db, run['job_id'], '@' + value, run['test_mode'], 1)
    db.execute(runs.update().where(runs.c.task_id == run['task_id']).values(status=value))


def pin(store, db, task_id, job, test=False, *, force=False):
    run = db.execute(select(runs).where(runs.c.task_id == task_id).with_for_update()).mappings().first()
    if run:
        if bool(run['test_mode']) != bool(test):
            count(store, db, job, '@' + run['status'], run['test_mode'], -1)
            count(store, db, job, '@pending', test, 1)
            db.execute(runs.update().where(runs.c.task_id == task_id).values(status='pending', test_mode=int(test)))
            db.execute(requirements.delete().where(requirements.c.task_id == task_id))
        return {'version': run['version'], 'spec': json.loads(run['spec'])}
    contract = current(db, job)
    if not contract:
        return None  # Preserve historical jobs and optional sidecars.
    from webapp.database import tasks
    if not force and db.execute(select(tasks.c.started_at).where(tasks.c.id == task_id)).scalar_one_or_none():
        return None  # Previously started legacy items retain their original behavior.
    db.execute(runs.insert().values(task_id=task_id, job_id=job, version=contract['version'],
        spec=packed(contract['spec']), status='pending', test_mode=int(test)))
    count(store, db, job, '@pending', test, 1)
    return contract


def requirement(store, db, task_id, spec, kind, language=''):
    key = digest([kind, language, variant(spec, kind)])
    store.db.ensure(db, requirements, dict(task_id=task_id, key=key, kind=kind, language=language,
        variant=variant(spec, kind), status='pending', updated_at=stamp()))
    return key


def refresh(store, db, task_id):
    run = db.execute(select(runs).where(runs.c.task_id == task_id).with_for_update()).mappings().one()
    rows = list(db.execute(select(requirements.c.status).where(requirements.c.task_id == task_id)).scalars())
    status = ('review' if 'missing' in rows else
              ('accepted' if 'waived' in rows else 'complete') if rows and all(s in ('done', 'waived') for s in rows) else
              'partial' if 'done' in rows else 'pending')
    state(store, db, run, status)
    return status


def fenced_run(store, db, task_id):
    from webapp.database import tasks
    from webapp.downloads import DownloadLeaseLost
    task = db.execute(store._locked(select(tasks).where(tasks.c.id == task_id))).mappings().one()
    if task['status'] != 'running' or task['worker_id'] != store.owner or not task['lease_until'] or task['lease_until'] <= (store.fleet.clock(db) if store.fleet else __import__('time').time()):
        raise DownloadLeaseLost('交付任务租约失效，不能更新产物状态。')
    if store.fleet and store.fleet.row(db)['boot_id'] != store.fleet.boot_id:
        raise DownloadLeaseLost('交付实例已重启，旧进程不能更新产物。')
    return task


def mark(store, task_id, key, value, error=None):
    with store.connect() as db:
        fenced_run(store, db, task_id)
        db.execute(requirements.update().where(requirements.c.task_id == task_id, requirements.c.key == key,
            requirements.c.status != 'waived').values(status=value, error=error, updated_at=stamp()))
        return refresh(store, db, task_id)


def artifact_identity(job, video_id, spec, kind, language='', test=False):
    return digest([job, video_id, kind, language, variant(spec, kind), bool(test)])


def pending(store, task_id, artifact, path):
    """Retain a verified file for upload retry without issuing a billing receipt."""
    with store.connect() as db:
        task = fenced_run(store, db, task_id)
        old = db.execute(select(artifacts.c.id).where(artifacts.c.id == artifact['id'])).first()
        values = dict(path=str(path), size=Path(path).stat().st_size,
                      instance_id=store.fleet.id if store.fleet else 'local',
                      storage=artifact['storage'], object_key=None, available=0)
        if old:
            db.execute(artifacts.update().where(artifacts.c.id == artifact['id']).values(**values))
        else:
            db.execute(artifacts.insert().values(**values, id=artifact['id'],
                seq=store.db.sequence(db, 'delivery_artifact'), job_id=task['batch_id'], task_id=task_id,
                video_id=task['video_id'], title=artifact['title'], kind=artifact['kind'],
                language=artifact['language'], variant=artifact['variant'], test_mode=0, created_at=stamp()))


def receipt(store, db, task, artifact, path, size, *, fresh):
    """Called inside the same leased transaction as ledger/totals registration."""
    run = db.execute(select(runs).where(runs.c.task_id == task['id']).with_for_update()).mappings().one()
    spec = json.loads(run['spec'])
    kind = artifact['kind']
    expected = artifact_identity(run['job_id'], task['video_id'], spec, kind,
                                 artifact.get('language', ''), bool(run['test_mode']))
    if kind not in spec['outputs'] or artifact['variant'] != variant(spec, kind) or artifact['id'] != expected:
        raise ValueError('产物与当前固定交付要求不匹配。')
    values = dict(id=artifact['id'], job_id=run['job_id'], task_id=task['id'],
        video_id=task['video_id'], title=artifact.get('title') or task['video_id'] or '',
        kind=artifact['kind'], language=artifact.get('language', ''), variant=artifact['variant'],
        path=str(path), size=size, instance_id=task.get('instance_id') or (store.fleet.id if store.fleet else 'local'),
        storage=artifact.get('storage', 'local'), object_key=artifact.get('object_key'), available=1,
        test_mode=int(run['test_mode']), created_at=stamp())
    old = db.execute(select(artifacts.c.id).where(artifacts.c.id == artifact['id'])).first()
    if old:
        db.execute(artifacts.update().where(artifacts.c.id == artifact['id']).values(
            path=str(path), size=size, available=1, instance_id=values['instance_id'],
            storage=values['storage'], object_key=values['object_key']))
    else:
        db.execute(artifacts.insert().values(**values, seq=store.db.sequence(db, 'delivery_artifact')))
    key = requirement(store, db, task['id'], spec, artifact['kind'], artifact.get('language', ''))
    db.execute(requirements.update().where(requirements.c.task_id == task['id'], requirements.c.key == key).values(
        status='done', artifact_id=artifact['id'], error=None, updated_at=stamp()))
    if fresh:
        count(store, db, run['job_id'], artifact['kind'], run['test_mode'], 1, size)
    refresh(store, db, task['id'])


def describe(db, jobs):
    jobs = list(jobs)
    result = {job: {'contract': None, 'states': {}, 'types': {}, 'test_types': {}} for job in jobs}
    if not jobs:
        return result
    for row in db.execute(select(contracts).where(contracts.c.job_id.in_(jobs)).order_by(contracts.c.version)).mappings():
        spec = json.loads(row['spec'])
        result[row['job_id']]['contract'] = {'version': row['version'], 'spec': spec, 'summary': summary(spec)}
    for row in db.execute(select(totals).where(totals.c.job_id.in_(jobs))).mappings():
        item = result[row['job_id']]
        if row['kind'].startswith('@'):
            if not row['test_mode']:
                item['states'][row['kind'][1:]] = row['file_count']
        else:
            item['test_types' if row['test_mode'] else 'types'][row['kind']] = {
                'file_count': row['file_count'], 'file_bytes': row['file_bytes']}
    return result


def task_details(db, ids):
    ids = list(ids)
    result = {i: {'requirements': [], 'status': 'pending'} for i in ids}
    if not ids:
        return result
    for row in db.execute(select(runs).where(runs.c.task_id.in_(ids))).mappings():
        result[row['task_id']].update(status=row['status'], version=row['version'], test_mode=bool(row['test_mode']))
    for row in db.execute(select(requirements).where(requirements.c.task_id.in_(ids))).mappings():
        result[row['task_id']]['requirements'].append(dict(row))
    return result
