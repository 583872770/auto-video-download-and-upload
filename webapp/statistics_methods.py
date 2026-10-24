"""Incremental accounting by the method saved in each task's config snapshot."""
import json

from sqlalchemy import func, select

from webapp.database import download_sessions as sessions, method_tasks, method_totals, tasks
from webapp.statistics import COUNTERS, OUTCOMES, with_test_sizes

METHODS = {
    'yt_dlp': '方式一：yt-dlp',
    'yt5s': '方式二：yt5s',
    'yt5s_fallback': '方式三：yt5s 优先，失败用 yt-dlp',
    'unknown': '历史未分类',
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

    def ensure(self, db, row, identity, status='running'):
        current = db.execute(select(method_tasks).where(method_tasks.c.session_id == identity,
            method_tasks.c.task_id == row['id'])).mappings().first()
        if current:
            return current
        config = self.store.decode(row, private=True, db=db)['payload'].get('config', {})
        method = config.get('download_method', 'yt_dlp')
        if method not in METHODS:
            method = 'unknown'
        current = dict(session_id=identity, task_id=row['id'], method=method, status=status, fallback_used=0)
        db.execute(method_tasks.insert().values(**current))
        changes = {'attempted': 1}
        if status in OUTCOMES:
            changes[status] = 1
        self.add(db, identity, method, changes)
        return current

    def change(self, db, row, identity, status, *, fallback_used=False):
        current = self.ensure(db, row, identity)
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

    def record_file(self, db, row, identity, size, test_mode):
        current = self.ensure(db, row, identity)
        self.add(db, identity, current['method'],
                 {'test_file_count': 1, 'test_file_bytes': size} if test_mode else {'file_count': 1, 'file_bytes': size})

    def fallback(self, task_id):
        """Count a task once, including switches followed by failure or a pause."""
        with self.store.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            row = db.execute(self.store._locked(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            if not row or row['status'] != 'running' or row['worker_id'] != self.store.owner:
                from webapp.downloads import DownloadLeaseLost
                raise DownloadLeaseLost('任务租约已失效，无法登记下载方式切换。')
            identity = json.loads(row['payload']).get('download_session_id')
            if identity:
                self.store.statistics._active(db)
                current = self.ensure(db, row, identity)
                self.change(db, row, identity, current['status'], fallback_used=True)

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
