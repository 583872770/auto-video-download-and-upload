"""Incremental accounting by the provider selected when execution starts."""

from sqlalchemy import func, select

from webapp.database import download_sessions as sessions, method_tasks, method_totals, provider_tasks, tasks
from webapp.settings import download_methods
from webapp.statistics import COUNTERS, OUTCOMES, with_test_sizes

METHODS = {
    'yt_dlp': 'yt-dlp',
    'yt5s': 'yt5s',
    'downmp3': 'downmp3',
    'yt1d': 'yt1d',
    'bergung': 'Bergung',
    'unknown': '未分类',
}
FIELDS = (*COUNTERS, 'test_file_count', 'test_file_bytes', 'fallback_tasks')


def describe_method(method, values):
    value = with_test_sizes({key: int(values.get(key) or 0) for key in FIELDS})
    succeeded = value['succeeded'] + value['test_succeeded']
    value.update(method=method, label=METHODS[method], file_bytes_exact=str(value['file_bytes']),
                 unfinished=max(0, value['attempted'] - succeeded - value['failed'] - value['skipped']),
                 success_rate=round(100 * succeeded / (succeeded + value['failed']), 1) if succeeded + value['failed'] else None)
    return value


class MethodStatistics:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def add(self, db, identity, method, changes):
        self.db.ensure(db, method_totals, {'session_id': identity, 'method': method})
        db.execute(method_totals.update().where(method_totals.c.session_id == identity,
            method_totals.c.method == method).values({key: method_totals.c[key] + value
                                                     for key, value in changes.items() if value}))

    def ensure(self, db, row, identity, status='running', *, provider=None):
        current = db.execute(select(method_tasks).where(method_tasks.c.session_id == identity,
            method_tasks.c.task_id == row['id'])).mappings().first()
        if current and row.get('_previous_download_method') is None:
            return current
        config = self.store.decode(row, private=True, db=db)['payload'].get('config', {})
        if not current and provider is None and config.get('scheduler_mode') == 'auto':
            return None  # No adapter has actually been selected yet.
        method = provider or download_methods(config)[0]
        if current:
            if row.get('_previous_download_method') is not None and current['method'] != method:
                self.remember(db, current)
                # Retrying with a new provider preserves the previous provider's
                # bytes and unfinished attempt in this session.
                if current['status'] == 'running':
                    self.add(db, identity, current['method'], {'running': -1})
                    self.remember(db, {**current, 'status': 'pending'})
                return self.activate(db, row, identity, method, 'pending')
            return current
        current = dict(session_id=identity, task_id=row['id'], method=method, status=status, fallback_used=0)
        db.execute(method_tasks.insert().values(**current))
        self.remember(db, current)
        changes = {'attempted': 1}
        if status in OUTCOMES:
            changes[status] = 1
        self.add(db, identity, method, changes)
        return current

    def remember(self, db, current):
        self.db.ensure(db, provider_tasks, dict(current))
        db.execute(provider_tasks.update().where(provider_tasks.c.session_id == current['session_id'],
            provider_tasks.c.task_id == current['task_id'], provider_tasks.c.method == current['method']).values(
                status=current['status'], fallback_used=current['fallback_used']))

    def activate(self, db, row, identity, method, status, *, fallback=False):
        previous = db.execute(select(provider_tasks).where(provider_tasks.c.session_id == identity,
            provider_tasks.c.task_id == row['id'], provider_tasks.c.method == method)).mappings().first()
        changes = {'attempted': 1} if previous is None else {}
        if previous and previous['status'] in OUTCOMES:
            changes[previous['status']] = -1
        if status in OUTCOMES:
            changes[status] = changes.get(status, 0) + 1
        if fallback and not (previous and previous['fallback_used']):
            changes['fallback_tasks'] = 1
        self.add(db, identity, method, changes)
        current = dict(session_id=identity, task_id=row['id'], method=method, status=status,
                       fallback_used=int(fallback or bool(previous and previous['fallback_used'])))
        self.remember(db, current)
        db.execute(method_tasks.update().where(method_tasks.c.session_id == identity,
            method_tasks.c.task_id == row['id']).values(method=method, status=status, fallback_used=current['fallback_used']))
        return current

    def change(self, db, row, identity, status, *, fallback_used=False):
        current = self.ensure(db, row, identity)
        if current is None:
            return
        config = self.store.decode(row, private=True, db=db)['payload'].get('config', {})
        if config.get('scheduler_mode') == 'auto' and current['status'] != 'running' and status != 'running':
            return  # A retry that has not selected an adapter cannot fail the old one.
        changes = {}
        if current['status'] != status:
            if current['status'] in OUTCOMES:
                changes[current['status']] = -1
            if status in OUTCOMES:
                changes[status] = changes.get(status, 0) + 1
        fallback = current['method'] == 'yt5s_fallback' and fallback_used and not current['fallback_used']
        if fallback:
            changes['fallback_tasks'] = 1
        if changes:
            self.add(db, identity, current['method'], changes)
        db.execute(method_tasks.update().where(method_tasks.c.session_id == identity,
            method_tasks.c.task_id == row['id']).values(status=status,
                fallback_used=1 if fallback or current['fallback_used'] else 0))
        self.remember(db, {**current, 'status': status, 'fallback_used': int(fallback or current['fallback_used'])})

    def record_file(self, db, row, identity, size, test_mode):
        current = self.ensure(db, row, identity)
        if current is None:
            raise RuntimeError('自动调度尚未选择下载方式，无法登记下载文件。')
        self.add(db, identity, current['method'],
                 {'test_file_count': 1, 'test_file_bytes': size} if test_mode else {'file_count': 1, 'file_bytes': size})

    def fallback(self, task_id, provider='yt_dlp'):
        """Count a task once, including switches followed by failure or a pause."""
        self.select_provider(task_id, provider, fallback=True)

    def select_provider(self, task_id, provider, *, fallback=False):
        """Bind automatic routing to the actual adapter without a fake failure."""
        with self.store.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            row = db.execute(self.store._locked(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            if not row or row['status'] != 'running' or row['worker_id'] != self.store.owner:
                from webapp.downloads import DownloadLeaseLost
                raise DownloadLeaseLost('任务租约已失效，无法登记下载方式切换。')
            if self.store.fleet and (not row['lease_until'] or row['lease_until'] <= self.store.fleet.clock(db)
                    or self.store.fleet.row(db)['boot_id'] != self.store.fleet.boot_id):
                from webapp.downloads import DownloadLeaseLost
                raise DownloadLeaseLost('任务租约已过期，无法登记下载方式切换。')
            identity = self.store.decode(row, private=True, db=db)['payload'].get('download_session_id')
            if identity:
                self.store.statistics._active(db)
                current = self.ensure(db, row, identity, provider=provider)
                if current['method'] != provider:
                    if current['status'] == 'running':
                        self.change(db, row, identity, 'failed' if fallback else 'pending')
                    self.activate(db, row, identity, provider, 'running', fallback=fallback)
                elif current['status'] != 'running':
                    self.activate(db, row, identity, provider, 'running', fallback=fallback)

    def summary(self, db, totals, *, identity=None, query=''):
        statement = select(method_totals.c.method, *(func.sum(method_totals.c[key]).label(key) for key in FIELDS))
        statement = statement.select_from(method_totals.join(sessions, method_totals.c.session_id == sessions.c.id))
        if identity:
            statement = statement.where(sessions.c.id == identity)
        if query:
            statement = statement.where(sessions.c.label.contains(query[:200], autoescape=True))
        rows = {row['method']: dict(row) for row in db.execute(statement.group_by(method_totals.c.method)).mappings()}
        known = [describe_method(method, rows.get(method, {})) for method in METHODS if method != 'unknown']
        residual = {key: max(0, int(totals.get(key) or 0) - sum(row[key] for row in known))
                    for key in FIELDS if key != 'fallback_tasks'}
        residual['fallback_tasks'] = int(rows.get('unknown', {}).get('fallback_tasks', 0))
        return [*known, *([describe_method('unknown', residual)] if any(residual.values()) else [])]

