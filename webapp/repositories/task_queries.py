"""TaskQueries contracts used by the composed TaskStore repository."""
import base64
import json
import time

from sqlalchemy import and_, func, or_, select

from webapp.database import batches, counters, download_lists, tasks, job_imports
from webapp.repositories.task_codec import LIST_FIELDS


class TaskQueries:
    def get(self, task_id, private=False):
        with self.connect() as db:
            value = self.decode(db.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first(), private, db)
            return value

    def page(self, *, limit=50, cursor=None, actions=None, status=None, query='', batch_id=None, order='desc', finished_only=False):
        if not 1 <= limit <= 100:
            raise ValueError('每页条数必须在 1 到 100 之间。')
        statement = select(*LIST_FIELDS)
        if actions:
            statement = statement.where(tasks.c.action.in_(actions))
        if finished_only:
            statement = statement.where(tasks.c.status.in_(('succeeded', 'failed', 'interrupted')))
        if status:
            statuses = {'running': ['running', 'queued'], 'active': ['running'], 'failed': ['failed', 'interrupted']}.get(status, [status])
            statement = statement.where(tasks.c.status.in_(statuses))
        if batch_id:
            statement = statement.where(tasks.c.batch_id == batch_id)
        if finished_only:
            # Completion order differs from submission order in large queues and
            # with parallel downloads. Sequence breaks equal-second time ties.
            finished = tasks.c.finished_at
            time_order = finished.asc() if order == 'asc' else finished.desc()
            ordering = [time_order, tasks.c.seq.asc() if order == 'asc' else tasks.c.seq.desc()]
            if cursor:
                try:
                    if len(cursor) > 512 or not cursor.startswith('h.'):
                        raise ValueError
                    timestamp, sequence = json.loads(base64.urlsafe_b64decode(cursor[2:].encode()))
                    if (timestamp is not None and (not isinstance(timestamp, str) or len(timestamp) > 64)) or type(sequence) is not int:
                        raise ValueError
                except (ValueError, TypeError, UnicodeError):
                    raise ValueError('任务记录分页标记无效，请返回第一页。') from None
                after_sequence = tasks.c.seq > sequence if order == 'asc' else tasks.c.seq < sequence
                if timestamp is None:
                    after = and_(finished.is_(None), after_sequence)
                    if order == 'asc':
                        after = or_(after, finished.is_not(None))
                else:
                    after = or_(finished > timestamp if order == 'asc' else finished < timestamp,
                                and_(finished == timestamp, after_sequence))
                    if order != 'asc':
                        after = or_(after, finished.is_(None))
                statement = statement.where(after)
        else:
            ordering = [tasks.c.seq.asc() if order == 'asc' else tasks.c.seq.desc()]
            if cursor:
                statement = statement.where(tasks.c.seq > int(cursor) if order == 'asc' else tasks.c.seq < int(cursor))
        if query:
            escaped = query.casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            video_query = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            statement = statement.where(or_(tasks.c.video_id.startswith(video_query, escape='\\'),
                                             tasks.c.id.startswith(escaped, escape='\\'),
                                             tasks.c.title.startswith(escaped, escape='\\')))
        with self.connect() as db:
            rows = db.execute(statement.order_by(*ordering).limit(limit + 1)).mappings().all()
            next_cursor = None
            if len(rows) > limit:
                last = rows[limit - 1]
                next_cursor = ('h.' + base64.urlsafe_b64encode(json.dumps([last['finished_at'], last['seq']]).encode()).decode()
                               if finished_only else str(last['seq']))
            return {'items': self.decode_many(rows[:limit]),
                    'next_cursor': next_cursor}

    def list(self, limit=100, actions=None):
        return self.page(limit=min(limit, 100), actions=actions)['items']

    def batch_page(self, limit=20, cursor=None):
        statement = select(batches)
        if cursor:
            statement = statement.where(batches.c.seq < int(cursor))
        with self.connect() as db:
            rows = db.execute(statement.order_by(batches.c.seq.desc()).limit(limit + 1)).mappings().all()
            result = []
            for row in rows[:limit]:
                result.append({**row, 'counts': self._counts(db, ['batch:' + row['id']])})
            return {'items': result, 'next_cursor': str(rows[limit - 1]['seq']) if len(rows) > limit else None}

    def _counts(self, db, scopes=None):
        statement = select(counters.c.status, func.sum(counters.c.value)).group_by(counters.c.status)
        statement = statement.where(counters.c.scope.in_(scopes)) if scopes else statement.where(counters.c.scope.like('action:%'))
        result = {row[0]: max(0, int(row[1])) for row in db.execute(statement)}
        return {**result, 'total': sum(value for key, value in result.items() if key not in ('removed', 'cancelled'))}

    def counts(self, actions=None, batch_id=None):
        with self.connect() as db:
            return self._counts(db, ['batch:' + batch_id] if batch_id else
                                ['action:' + action for action in actions] if actions else None)

    def pending(self, *, runnable=False):
        if not runnable:
            values = self.counts()
            return bool(values.get('queued', 0) + values.get('running', 0))
        with self.connect() as db:
            statement = select(tasks.c.id).where(tasks.c.status.in_(('queued', 'running')), self.released_condition())
            if self.claims_paused(db):
                statement = statement.where(tasks.c.action != 'download')
            elif not self._released_queued_count(db):
                statement = statement.where(or_(tasks.c.action != 'download', tasks.c.status == 'running'))
            return db.execute(statement.limit(1)).first() is not None

    def _released_queued_count(self, db):
        if self.fleet and self.workbench_job(db):
            return int(db.execute(select(counters.c.value).where(
                counters.c.scope == 'batch:' + self.workbench_job(db),
                counters.c.status == 'queued')).scalar_one_or_none() or 0)
        selected = self.selected_download_job(db)
        if selected:
            return int(db.execute(select(func.coalesce(counters.c.value, 0)).select_from(
                download_lists.join(counters, counters.c.scope == 'batch:' + download_lists.c.id)).where(
                    download_lists.c.id == selected, download_lists.c.released == 1,
                    counters.c.status == 'queued')).scalar_one_or_none() or 0)
        queued = self._counts(db, ['action:download']).get('queued', 0)
        if not queued:
            return 0
        # Count held videos through their batch counters, rather than scanning
        # millions of unreleased task rows on every scheduler tick.
        held = db.execute(select(func.coalesce(func.sum(counters.c.value), 0)).select_from(
            download_lists.join(counters, counters.c.scope == 'batch:' + download_lists.c.id)).where(
                download_lists.c.released == 0, counters.c.status == 'queued')).scalar_one()
        return max(0, queued - int(held))

    def released_import_pending(self, db):
        counts = self._counts(db, ['action:import'])
        if not (counts.get('queued', 0) or counts.get('running', 0)):
            return False
        identity = func.json_extract(tasks.c.payload, '$.import_id')
        if self.db.mysql:
            identity = func.json_unquote(identity)
        parent = select(job_imports.c.job_id).where(job_imports.c.id == identity).scalar_subquery()
        held = select(download_lists.c.id).where(or_(download_lists.c.id == identity, download_lists.c.id == parent), download_lists.c.released == 0).exists()
        return db.execute(select(tasks.c.id).where(tasks.c.action == 'import',
            tasks.c.status.in_(('queued', 'running')), ~held).limit(1)).first() is not None

    def download_connection_busy(self):
        if self.fleet and self.fleet.can_download:
            with self.connect() as db:
                running = db.execute(select(tasks.c.id).where(tasks.c.instance_id == self.fleet.id,
                    tasks.c.status == 'running', tasks.c.action == 'download').limit(1)).first()
                return bool(running or (not self.fleet.row(db)['paused'] and not self.claims_paused(db)
                                        and self._released_queued_count(db)))
        with self.connect() as db:
            downloads, checks = self._counts(db, ['action:download']), self._counts(db, ['action:check'])
            return bool(downloads.get('running', 0) or checks.get('running', 0) or checks.get('queued', 0)
                        or (not self.downloads_paused(db) and self._released_queued_count(db)))

    def runnable_roles(self):
        """Dispatch only queues with a due task, including after retry backoff."""
        with self.connect() as db:
            paused = self.claims_paused(db)
            queued = db.execute(select(counters.c.scope).where(
                counters.c.status == 'queued', counters.c.value > 0,
                counters.c.scope.like('action:%'))).scalars().all()
            roles = set()
            for scope in queued:
                action = scope.removeprefix('action:')
                if action == 'download' and (paused or not self._released_queued_count(db)):
                    continue
                due = db.execute(select(tasks.c.id).where(
                    tasks.c.action == action, tasks.c.status == 'queued',
                    tasks.c.available_at <= time.time(), self.released_condition()).limit(1)).first()
                if due:
                    roles.update(('download', 'regular') if action == 'download' else
                                 ('maintenance',) if action in ('import', 'index') else ('regular',))
            return roles

    def runnable_counts(self):
        """Small due-count snapshot used to avoid launching idle worker slots."""
        with self.connect() as db:
            paused = self.claims_paused(db)
            counts = {}
            stamp = self.fleet.clock(db) if self.fleet else time.time()
            for role, actions in (('download', ('download',)), ('maintenance', ('import', 'index')),
                                  ('regular', ('check',))):
                if self.fleet and role == 'download' and not self.fleet.admission(db, lock=False):
                    continue
                if not self._counts(db, ['action:' + action for action in actions]).get('queued', 0):
                    continue
                if role == 'download' and (paused or not self._released_queued_count(db)):
                    continue
                # Only the number of available worker slots matters, not the
                # size of a potentially multi-million-video backlog.
                from webapp.throughput import MAX_WORKERS
                statement = select(tasks.c.id).where(tasks.c.action.in_(actions),
                    tasks.c.status == 'queued', tasks.c.available_at <= stamp, self.released_condition()).limit(MAX_WORKERS)
                if self.fleet:
                    statement = statement.where(or_(tasks.c.target_instance_id == self.fleet.id,
                        and_(tasks.c.target_instance_id.is_(None), tasks.c.action.in_(self.fleet.untargeted_actions()))))
                    if role == 'download' and (job := self.workbench_job(db)):
                        statement = statement.where(tasks.c.batch_id == job).order_by(tasks.c.seq)
                        statement = statement.with_hint(tasks, 'FORCE INDEX (ix_tasks_batch_ready)', dialect_name='mysql')
                count = len(db.execute(statement).all())
                if count:
                    counts[role] = count
            return counts

