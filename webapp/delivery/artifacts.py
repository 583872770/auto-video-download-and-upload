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
    old = current(db, job)
    from webapp.task_storage import normalize as normalize_storage
    if not isinstance(spec, dict):
        raise ValueError('请提交有效的交付要求。')
    spec = {**spec, 'storage': normalize_storage(spec.get('storage'), old['spec']['storage'] if old else None)}
    spec = normalize(spec)
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


def pin(store, db, task_id, job):
    run = db.execute(select(runs).where(runs.c.task_id == task_id).with_for_update()).mappings().first()
    if run:
        return {'version': run['version'], 'spec': json.loads(run['spec'])}
    contract = current(db, job)
    if not contract:
        raise ValueError('任务缺少交付与保存配置，请在任务中心设置后执行。')
    test = contract['spec']['storage']['storage_mode'] == 'test'
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
    if status in ('complete', 'accepted'):
        from webapp.notifications import resolve_task
        resolve_task(store, db, task_id, code='delivery')
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
        task = fenced_run(store, db, task_id)
        db.execute(requirements.update().where(requirements.c.task_id == task_id, requirements.c.key == key,
            requirements.c.status != 'waived').values(status=value, error=error, updated_at=stamp()))
        status = refresh(store, db, task_id)
        if value == 'missing':
            from webapp.notifications import settle_task
            settle_task(store, db, task, result={'message': error or '来源缺少所选内容。'},
                        progress=json.loads(task['progress']) if task['progress'] else {})
        return status


def artifact_identity(job, video_id, spec, kind, language='', test=False):
    return digest([job, video_id, kind, language, variant(spec, kind), bool(test)])


def recorded_test_output(db, task, artifact):
    """A disposable test output is complete once its semantic receipt is durable.

    The first successful size is the statistics receipt. Later attempts can
    produce different bytes (especially metadata); those do not invalidate or
    replace the original completed test. Formal deliveries still require files.
    """
    from webapp.database import test_files
    from webapp.delivery.contracts import KINDS
    kind, language = artifact.get('kind'), artifact.get('language', '')
    if kind not in KINDS or artifact.get('id') != digest([
            task['batch_id'], task['video_id'], kind, language, artifact.get('variant'), True]):
        return None
    published = db.execute(select(artifacts.c.id).where(
        artifacts.c.id == artifact['id'], artifacts.c.task_id == task['id'],
        artifacts.c.job_id == task['batch_id'], artifacts.c.video_id == task['video_id'],
        artifacts.c.kind == kind, artifacts.c.language == language,
        artifacts.c.variant == artifact['variant'], artifacts.c.test_mode == 1)).first()
    if not published:
        return None
    return db.execute(select(test_files).where(test_files.c.id == artifact['id'],
        test_files.c.task_id == task['id'], test_files.c.video_id == task['video_id'],
        test_files.c.size > 0)).mappings().first()


def pending(store, task_id, artifact, path):
    """Retain a verified file for upload retry without issuing a billing receipt."""
    with store.connect() as db:
        task = fenced_run(store, db, task_id)
        version = db.execute(select(runs.c.version).where(runs.c.task_id == task_id)).scalar_one()
        old = db.execute(select(artifacts.c.id).where(artifacts.c.id == artifact['id'])).first()
        values = dict(path=str(path), size=Path(path).stat().st_size,
                      instance_id=store.fleet.id if store.fleet else 'local',
                      storage=artifact['storage'], object_key=None, available=0, contract_version=version,
                      subtitle_source=artifact.get('subtitle_source', ''))
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
        subtitle_source=artifact.get('subtitle_source', ''),
        path=str(path), size=size, instance_id=task.get('instance_id') or (store.fleet.id if store.fleet else 'local'),
        storage=artifact.get('storage', 'local'), object_key=artifact.get('object_key'), available=1,
        contract_version=run['version'],
        test_mode=int(run['test_mode']), created_at=stamp())
    old = db.execute(select(artifacts.c.id).where(artifacts.c.id == artifact['id'])).first()
    if old:
        db.execute(artifacts.update().where(artifacts.c.id == artifact['id']).values(
            path=str(path), size=size, available=1, instance_id=values['instance_id'],
            storage=values['storage'], object_key=values['object_key'],
            contract_version=values['contract_version'],
            subtitle_source=values['subtitle_source']))
    else:
        db.execute(artifacts.insert().values(**values, seq=store.db.sequence(db, 'delivery_artifact')))
    key = requirement(store, db, task['id'], spec, artifact['kind'], artifact.get('language', ''))
    db.execute(requirements.update().where(requirements.c.task_id == task['id'], requirements.c.key == key).values(
        status='done', artifact_id=artifact['id'], error=None, updated_at=stamp()))
    if fresh:
        count(store, db, run['job_id'], artifact['kind'], run['test_mode'], 1, size)
    refresh(store, db, task['id'])


def storage_config(db, artifact):
    """Resolve the immutable destination used to publish this particular file."""
    raw = db.execute(select(contracts.c.spec).where(contracts.c.job_id == artifact['job_id'],
        contracts.c.version == artifact['contract_version'])).scalar_one()
    return {**json.loads(raw)['storage'], '_task_storage': True}


def describe(db, jobs):
    jobs = list(jobs)
    result = {job: {'contract': None, 'states': {}, 'types': {}, 'test_types': {}} for job in jobs}
    if not jobs:
        return result
    for row in db.execute(select(contracts).where(contracts.c.job_id.in_(jobs)).order_by(contracts.c.version)).mappings():
        spec = json.loads(row['spec'])
        from webapp.task_storage import public_spec
        result[row['job_id']]['contract'] = {'version': row['version'], 'spec': public_spec(spec), 'summary': summary(spec)}
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
