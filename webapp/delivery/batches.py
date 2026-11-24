"""Bounded local batches and file dependency retirement, without source traffic."""
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from webapp.database import delivery_artifacts, delivery_requirements, test_files
from webapp.delivery import artifacts as ledger
from webapp.disk_budget import safe_path


CAPTION_BATCH = 8


def select_work(candidates, now):
    """Bounded, metadata-only scheduling with an absolute FIFO aging limit."""
    import json
    oldest = candidates[0]
    if now - oldest['_work_created'] >= 1800:
        return oldest
    def cost(row):
        plan = json.loads(row['_work_plan'])
        pending = [j for j in plan['jobs'] if not j.get('done')]
        writes = sum(max(0, int(j.get('output_estimate') or 0))
                     for j in pending if not j.get('prepared'))
        # Nonzero overhead prevents tiny captions from monopolizing the queue.
        work = writes + len(pending) * 1024 ** 2
        releasable = max(1024 ** 2, int(plan.get('progress', {}).get('space_plan', {}).get('source_bytes') or 0))
        return (work / releasable, row['_work_created'], row['seq'])
    return min(candidates, key=cost)


def units(jobs):
    ordered = sorted((j for j in jobs if not j.get('done')), key=lambda j: (
        j['kind'] == 'subtitle', j['kind'] == 'av' and len(set(j['inputs'])) == 1))
    captions = []
    for job in ordered:
        if job['kind'] != 'subtitle':
            yield [job]
        else:
            captions.append(job)
            if len(captions) == CAPTION_BATCH:
                yield captions
                captions = []
    if captions:
        yield captions


def test_readiness(session, jobs):
    """Read all disposable receipt facts once, retaining the task fence."""
    task_id = session.payload['_task_id']
    with session.store.connect() as db:
        task = ledger.fenced_run(session.store, db, task_id)
        requirements = {(r['kind'], r['language']): r['status'] for r in db.execute(
            select(delivery_requirements).where(delivery_requirements.c.task_id == task_id)).mappings()}
        artifacts = {r['id']: r for r in db.execute(select(delivery_artifacts).where(
            delivery_artifacts.c.task_id == task_id, delivery_artifacts.c.test_mode == 1,
            delivery_artifacts.c.job_id == task['batch_id'])).mappings()}
        files = {r['id']: r for r in db.execute(select(test_files).where(
            test_files.c.task_id == task_id, test_files.c.video_id == task['video_id'],
            test_files.c.size > 0)).mappings()}
    result = {}
    spec = session.config['_delivery_contract']
    for job in jobs:
        key = (job['kind'], job.get('language', ''))
        identity = ledger.artifact_identity(task['batch_id'], task['video_id'], spec, *key, True)
        artifact, receipt = artifacts.get(identity), files.get(identity)
        recorded = artifact is not None and receipt is not None
        if recorded and session.payload.get('_force_test_sample'):
            recorded = Path(artifact['path']).is_relative_to(Path(session.payload['_disk_workspace']))
        result[key] = requirements.get(key) == 'waived' or recorded
        if recorded:
            session.output_files[receipt['path']] = receipt['size']
    return result


class Dependencies:
    def __init__(self, jobs, workspace):
        self.workspace = Path(workspace)
        self.remaining = Counter(path for job in jobs if not job.get('done') for path in set(job['inputs']))

    def retire(self, jobs, retained):
        for job in jobs:
            for name in set(job['inputs']):
                self.remaining[name] -= 1
                if not self.remaining[name] and name not in retained:
                    safe_path(name, self.workspace).unlink(missing_ok=True)


def retain_test_files(session):
    if not session.test_mode or session.payload.get('_force_test_sample'):
        return True
    from webapp.delivery.samples import policy
    with session.store.connect() as db:
        task = ledger.fenced_run(session.store, db, session.payload['_task_id'])
        return policy(db, task['batch_id'])['enabled']


def reclaim_test_files(jobs, dependencies):
    """Caller committed receipts and the quick-test checkpoint before deletion."""
    for job in jobs:
        name = job.get('destination')
        if name and not dependencies.remaining[name]:
            safe_path(name, dependencies.workspace).unlink(missing_ok=True)
