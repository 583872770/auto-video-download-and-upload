"""TaskWrites contracts used by the composed TaskStore repository."""
from collections import Counter
import json
import uuid

from sqlalchemy import select

from webapp.database import batches, counters, reservations, snapshots, tasks, job_profiles, job_removed
from webapp.store_utils import source_identity


class TaskWrites:
    def _counter(self, db, scope, status, delta):
        db.execute(self.db.upsert(counters, dict(scope=scope, status=status, value=delta),
                                 ['scope', 'status'], {'value': counters.c.value + delta}))

    def _transition(self, db, row, status):
        scopes = ['action:' + row['action']]
        if row.get('batch_id'):
            scopes.append('batch:' + row['batch_id'])
        # Claims and retries touch the same counters in opposite directions.
        # Lock them in a consistent order to avoid cross-worker deadlocks.
        for scope in sorted(scopes):
            changes = Counter({status: 1})
            if row.get('status'):
                changes[row['status']] -= 1
            for state in sorted(changes):
                if changes[state]:
                    self._counter(db, scope, state, changes[state])

    def _create(self, db, action, payload, seq, *, deduplicate=False):
        task_id = uuid.uuid4().hex
        url = (payload.get('urls') or [None])[0]
        video_id, source_key = source_identity(url)
        if payload.get('config', {}).get('storage_mode') == 'test':
            source_key = 'test:' + source_key
        batch_id = payload.get('batch_id')
        if action == 'download' and batch_id and db.execute(select(job_profiles.c.id).where(job_profiles.c.id == batch_id)).first():
            source_key = 'job:' + batch_id + ':' + source_key
        if action == 'download':
            self.db.ensure(db, reservations, {'key': source_key, 'task_id': task_id})
            if deduplicate and db.execute(select(reservations.c.task_id).where(
                    reservations.c.key == source_key)).scalar_one() != task_id:
                return None
        if batch_id:
            self.db.ensure(db, batches, {'id': batch_id, 'seq': self.db.sequence(db, 'batch'),
                                        'created_at': self._timestamp(), 'name': payload.get('batch_name', '')})
        values = dict(id=task_id, seq=seq, action=action, status='queued', created_at=self._timestamp(),
                      payload=self._pack(db, payload), batch_id=batch_id, video_id=video_id, source_key=source_key,
                      url=url, title='', attempts=0, max_attempts=payload.get('config', {}).get('download_attempts', 3),
                      available_at=0, target_instance_id=self.fleet.id if self.fleet and action != 'download' else None)
        db.execute(tasks.insert().values(**values))
        self._transition(db, {**values, 'status': None}, 'queued')
        return task_id

    def create(self, action, payload):
        with self.connect() as db:
            task_id = self._create(db, action, payload, self.db.sequence(db, 'task'))
        return self.get(task_id)

    def create_batch(self, payloads, *, deduplicate=False):
        if not payloads:
            return []
        with self.connect() as db:
            ids = self.insert_downloads(db, payloads, deduplicate=deduplicate)
            rows = db.execute(select(tasks).where(tasks.c.id.in_(ids)).order_by(tasks.c.seq)).mappings().all() if ids else []
            return [self.decode(row) for row in rows]

    def insert_downloads(self, db, payloads, *, deduplicate=True, shared_snapshot=None):
        """Bounded bulk insert; importer commits its cursor in this same transaction."""
        if not payloads:
            return []
        start = self.db.sequence(db, 'task', len(payloads))
        rows, reservation_rows, packed = [], [], {}
        batch_ids = {p.get('batch_id') for p in payloads if p.get('batch_id')}
        managed = set(db.execute(select(job_profiles.c.id).where(job_profiles.c.id.in_(batch_ids))).scalars()) if batch_ids else set()
        restored_ids = []
        if managed:
            existing = {}
            urls = [p['urls'][0] for p in payloads if p.get('batch_id') in managed]
            for offset in range(0, len(urls), 1000):
                video_ids = [source_identity(url)[0] for url in urls[offset:offset+1000]]
                # An OR across video/url indexes made MySQL scan and lock most
                # of the batch. Keep both legacy URL and canonical-ID matches,
                # using their respective indexes and only restoration fields.
                columns = (tasks.c.id, tasks.c.batch_id, tasks.c.video_id, tasks.c.url,
                           tasks.c.action, tasks.c.status, tasks.c.finished_at, tasks.c.attempts)
                for condition, index_name in (
                        (tasks.c.video_id.in_(video_ids), 'ix_tasks_video'),
                        (tasks.c.url.in_(urls[offset:offset+1000]), 'ix_tasks_url_prefix')):
                    query = select(*columns).where(tasks.c.batch_id.in_(managed),
                        tasks.c.action == 'download', condition).with_hint(
                            tasks, 'FORCE INDEX (' + index_name + ')', dialect_name='mysql')
                    for old in db.execute(query.with_for_update()).mappings():
                        existing[(old['batch_id'], old['video_id'] or old['url'])] = old
            accepted_payloads = []
            for payload in payloads:
                key = (payload.get('batch_id'), source_identity(payload['urls'][0])[0] or payload['urls'][0])
                old = existing.get(key)
                if old is None:
                    accepted_payloads.append(payload)
                    continue
                if old['status'] == 'removed':
                    previous = db.execute(select(job_removed.c.previous_status).where(job_removed.c.task_id == old['id'])).scalar_one()
                    status = 'succeeded' if previous == 'succeeded' else 'queued'
                    self._transition(db, old, status)
                    db.execute(tasks.update().where(tasks.c.id == old['id']).values(status=status, worker_id=None,
                        lease_until=None, attempts=0 if status == 'queued' else old['attempts'], available_at=0,
                        finished_at=old['finished_at'] if status == 'succeeded' else None, error=None))
                    db.execute(job_removed.delete().where(job_removed.c.task_id == old['id']))
                    existing[key] = {**old, 'status': status}
                    restored_ids.append(old['id'])
            payloads = accepted_payloads
            if not payloads:
                return restored_ids
        for index, payload in enumerate(payloads):
            url = payload['urls'][0]
            video_id, source_key = source_identity(url)
            if payload.get('config', {}).get('storage_mode') == 'test':
                source_key = 'test:' + source_key
            if payload.get('batch_id') in managed:
                source_key = 'job:' + payload['batch_id'] + ':' + source_key
            task_id = uuid.uuid4().hex
            snapshot_id, encoded = shared_snapshot or self.prepare_snapshot(payload)
            packed[snapshot_id] = encoded
            rows.append(dict(id=task_id, seq=start + index, action='download', status='queued', created_at=self._timestamp(),
                             payload=json.dumps({'snapshot_id': snapshot_id, 'urls': [url], 'batch_id': payload.get('batch_id')}, ensure_ascii=False),
                             batch_id=payload.get('batch_id'), video_id=video_id, source_key=source_key, url=url, title='',
                             attempts=0, max_attempts=payload.get('config', {}).get('download_attempts', 3), available_at=0))
            reservation_rows.append({'key': source_key, 'task_id': task_id})
        # Preserve the first occurrence inside a chunk before database deduplication.
        unique = {row['key']: row for row in reversed(reservation_rows)}
        accepted = self.db.reserve_downloads(db, [unique[key] for key in sorted(unique)])
        if deduplicate:
            rows = [row for row in rows if row['id'] in accepted]
        if not rows:
            return restored_ids
        for key, value in packed.items():
            self.db.ensure(db, snapshots, {'id': key, 'payload': value})
        batch_ids = {row['batch_id'] for row in rows if row['batch_id']}
        existing_batches = set(db.execute(select(batches.c.id).where(batches.c.id.in_(batch_ids))).scalars()) if batch_ids else set()
        for batch_id in sorted(batch_ids - existing_batches):
            self.db.ensure(db, batches, {'id': batch_id, 'seq': self.db.sequence(db, 'batch'), 'created_at': self._timestamp(),
                                        'name': payloads[0].get('batch_name', '')})
        db.execute(tasks.insert(), rows)
        self._counter(db, 'action:download', 'queued', len(rows))
        for batch_id, count in Counter(row['batch_id'] for row in rows if row['batch_id']).items():
            self._counter(db, 'batch:' + batch_id, 'queued', count)
        return restored_ids + [row['id'] for row in rows]

