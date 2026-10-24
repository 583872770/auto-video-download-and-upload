"""Transactional session totals and a ledger of completed video files."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import uuid

from sqlalchemy import func, select

from webapp.database import deliveries, download_sessions as sessions, session_tasks, settings, tasks, test_files, test_totals


COUNTERS = ('attempted', 'running', 'succeeded', 'failed', 'skipped', 'test_succeeded', 'file_count', 'file_bytes')
OUTCOMES = ('running', 'succeeded', 'failed', 'skipped', 'test_succeeded')
UNITS = {'GB': 1000 ** 3, 'GiB': 1024 ** 3}


def with_test_sizes(value):
    value = dict(value)
    for key in ('test_file_count', 'test_file_bytes'):
        value[key] = int(value.get(key) or 0)
    value['test_file_bytes_exact'] = str(value['test_file_bytes'])
    value['downloaded_file_count'] = value['file_count'] + value['test_file_count']
    value['downloaded_file_bytes'] = value['file_bytes'] + value['test_file_bytes']
    value['downloaded_file_bytes_exact'] = str(value['downloaded_file_bytes'])
    return value


def session_select():
    return select(sessions, func.coalesce(test_totals.c.test_file_count, 0).label('test_file_count'),
                  func.coalesce(test_totals.c.test_file_bytes, 0).label('test_file_bytes')).select_from(
                      sessions.outerjoin(test_totals, sessions.c.id == test_totals.c.session_id))


def stamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def pricing(data):
    try:
        price = Decimal(str(data.get('price', '0')))
        if not price.is_finite() or price < 0 or price > 1000000 or price.as_tuple().exponent < -6:
            raise ValueError()
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('单价须为 0 到 1000000 之间的数字，最多六位小数。') from None
    unit = data.get('unit', 'GB')
    if not isinstance(unit, str) or unit not in UNITS:
        raise ValueError('请选择 GB 或 GiB 作为计费单位。')
    return {'price': format(price, 'f'), 'unit': unit}


def describe(row):
    value = with_test_sizes(row)
    size = value['file_bytes']
    value['file_bytes_exact'] = str(size)
    value['quantity'] = format(Decimal(size) / UNITS[value['unit']], '.6f')
    value['amount'] = format(charge(size, value['price'], value['unit']), 'f')
    value['unfinished'] = max(0, value['attempted'] - sum(value[key] for key in
        ('succeeded', 'failed', 'skipped', 'test_succeeded')))
    succeeded = value['succeeded'] + value['test_succeeded']
    value['success_rate'] = round(100 * succeeded / (succeeded + value['failed']), 1) if succeeded + value['failed'] else None
    return value


def charge(size, price, unit):
    return (Decimal(size) * Decimal(price) / UNITS[unit]).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class DownloadStatistics:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def _active(self, db):
        return db.execute(self.store._locked(select(settings.c.value).where(
            settings.c.key == 'download_statistics_active'))).scalar_one()

    def defaults(self, db):
        value = db.execute(select(settings.c.value).where(settings.c.key == 'download_billing_defaults')).scalar_one_or_none()
        return json.loads(value) if value else {'price': '0', 'unit': 'GB'}

    def ensure_session(self, db):
        identity = self._active(db)
        if identity:
            return identity
        identity = uuid.uuid4().hex
        db.execute(sessions.insert().values(id=identity, seq=self.db.sequence(db, 'download_session'),
            started_at=stamp(), status='active', label='', **self.defaults(db)))
        db.execute(settings.update().where(settings.c.key == 'download_statistics_active').values(value=identity))
        return identity

    def begin(self, db, row):
        identity = self.ensure_session(db)
        old = db.execute(select(session_tasks).where(session_tasks.c.session_id == identity,
            session_tasks.c.task_id == row['id'])).mappings().first()
        if old:
            db.execute(session_tasks.update().where(session_tasks.c.session_id == identity,
                session_tasks.c.task_id == row['id']).values(status='running', finished_at=None, error=None))
        else:
            db.execute(session_tasks.insert().values(session_id=identity, task_id=row['id'], seq=row['seq'],
                video_id=row.get('video_id'), batch_id=row.get('batch_id'), status='running', started_at=stamp()))
        changes = {'running': sessions.c.running + (0 if old and old['status'] == 'running' else 1)}
        if not old:
            changes['attempted'] = sessions.c.attempted + 1
        elif old['status'] in OUTCOMES and old['status'] != 'running':
            changes[old['status']] = sessions.c[old['status']] - 1
        db.execute(sessions.update().where(sessions.c.id == identity).values(**changes))
        return identity

    def transition(self, db, row, status, error=None):
        identity = json.loads(row['payload']).get('download_session_id')
        if row['action'] != 'download' or not identity:
            return
        self._active(db)  # Use one lock order across completion, pause and file registration.
        old = db.execute(select(session_tasks).where(session_tasks.c.session_id == identity,
            session_tasks.c.task_id == row['id'])).mappings().first()
        if not old or old['status'] == status:
            return
        db.execute(session_tasks.update().where(session_tasks.c.session_id == identity,
            session_tasks.c.task_id == row['id']).values(status=status, error=error, finished_at=stamp()))
        changes = {}
        if old['status'] in OUTCOMES:
            changes[old['status']] = sessions.c[old['status']] - 1
        if status in OUTCOMES:
            changes[status] = sessions.c[status] + 1
        if changes:
            db.execute(sessions.update().where(sessions.c.id == identity).values(**changes))

    def settle(self, db, *, stop=False):
        identity = self._active(db)
        if not identity:
            return
        row = db.execute(select(sessions).where(sessions.c.id == identity)).mappings().one()
        if stop and row['status'] == 'active':
            db.execute(sessions.update().where(sessions.c.id == identity).values(status='stopping', stop_requested_at=stamp()))
        paused = stop or row['status'] == 'stopping'
        counts = self.store._counts(db, ['action:download', 'action:import'])
        if not row['running'] and (paused or not (counts.get('queued', 0) + counts.get('running', 0))):
            db.execute(sessions.update().where(sessions.c.id == identity).values(
                status='paused' if paused else 'completed', ended_at=stamp()))
            db.execute(settings.update().where(settings.c.key == 'download_statistics_active').values(value=''))

    def record_file(self, task_id, path, video_id='', title='', *, test_mode=False):
        """Called only after successful final-file processing, never from progress bytes."""
        path = Path(path).resolve()
        size = path.stat().st_size
        if not size:
            return False
        # A delivered path is registered once across retries and sessions.
        identity = hashlib.sha256(((task_id + '\0' if test_mode else '') + str(path)).encode('utf-8')).hexdigest()
        ledger = test_files if test_mode else deliveries
        with self.store.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            task = db.execute(self.store._locked(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            if not task or task['status'] != 'running' or task['worker_id'] != self.store.owner:
                from webapp.downloads import DownloadLeaseLost
                raise DownloadLeaseLost('任务租约已失效，无法登记下载文件。')
            session_id = json.loads(task['payload']).get('download_session_id')
            if not session_id:
                return False  # Jobs already executing during an upgrade have no reliable session.
            self._active(db)
            if db.execute(select(ledger.c.id).where(ledger.c.id == identity)).first():
                return False
            db.execute(ledger.insert().values(id=identity, seq=self.db.sequence(db, 'download_test_file' if test_mode else 'download_delivery'),
                session_id=session_id, task_id=task_id, video_id=video_id or task.get('video_id'),
                title=str(title or video_id or task.get('video_id') or path.stem), path=str(path), size=size, created_at=stamp()))
            if test_mode:
                self.db.ensure(db, test_totals, {'session_id': session_id, 'test_file_count': 0, 'test_file_bytes': 0})
                db.execute(test_totals.update().where(test_totals.c.session_id == session_id).values(
                    test_file_count=test_totals.c.test_file_count + 1, test_file_bytes=test_totals.c.test_file_bytes + size))
            else:
                db.execute(sessions.update().where(sessions.c.id == session_id).values(
                    file_count=sessions.c.file_count + 1, file_bytes=sessions.c.file_bytes + size))
        return True

    def session_rows(self, db, cursor=None, query='', limit=21):
        statement = session_select().order_by(sessions.c.seq.desc()).limit(limit)
        if cursor:
            statement = statement.where(sessions.c.seq < self.cursor(cursor))
        if query:
            statement = statement.where(sessions.c.label.contains(query[:200], autoescape=True))
        return db.execute(statement).mappings().all()

    def overview(self, cursor=None, query=''):
        with self.store.connect() as db:
            rows = self.session_rows(db, cursor, query)
            condition = sessions.c.label.contains(query[:200], autoescape=True) if query else True
            totals = db.execute(select(*(func.coalesce(func.sum(sessions.c[key]), 0).label(key) for key in COUNTERS))
                .where(condition)).mappings().one()
            test_summary = db.execute(select(
                func.coalesce(func.sum(test_totals.c.test_file_count), 0).label('test_file_count'),
                func.coalesce(func.sum(test_totals.c.test_file_bytes), 0).label('test_file_bytes'))
                .select_from(test_totals.join(sessions, sessions.c.id == test_totals.c.session_id))
                .where(condition)).mappings().one()
            amount, unpriced_bytes = Decimal('0'), 0
            for size, price, unit in db.execute(select(sessions.c.file_bytes, sessions.c.price, sessions.c.unit)
                    .where(condition, sessions.c.file_bytes > 0)).yield_per(500):
                amount += charge(size, price, unit)
                if not Decimal(price):
                    unpriced_bytes += size
            active_id = db.execute(select(settings.c.value).where(settings.c.key == 'download_statistics_active')).scalar_one()
            active = db.execute(session_select().where(sessions.c.id == active_id, condition)).mappings().first() if active_id else None
            since = db.execute(select(settings.c.value).where(settings.c.key == 'download_statistics_since')).scalar_one()
            test_since = db.execute(select(settings.c.value).where(settings.c.key == 'download_test_statistics_since')).scalar_one()
            defaults = self.defaults(db)
        return {'summary': {**with_test_sizes({**{key: int(value) for key, value in totals.items()}, **test_summary}),
                            'file_bytes_exact': str(int(totals['file_bytes'])),
                            'amount': format(amount, '.2f'), 'unpriced_bytes': unpriced_bytes},
                'active': describe(active) if active else None,
                'items': [describe(row) for row in rows[:20]], 'next_cursor': str(rows[19]['seq']) if len(rows) > 20 else None,
                'pricing': defaults, 'tracking_since': since, 'test_tracking_since': test_since}

    @staticmethod
    def cursor(value):
        try:
            result = int(value)
            if result < 0:
                raise ValueError()
            return result
        except (TypeError, ValueError):
            raise ValueError('无效的统计分页游标。') from None

    def session(self, identity):
        with self.store.connect() as db:
            row = db.execute(session_select().where(sessions.c.id == identity)).mappings().first()
        return describe(row) if row else None

    def details(self, identity, *, kind='files', cursor=None, limit=50):
        if kind not in ('files', 'test_files', 'failed', 'tasks'):
            raise ValueError('请选择交付文件、测试文件、失败任务或全部任务。')
        table = deliveries if kind == 'files' else test_files if kind == 'test_files' else session_tasks
        statement = select(table).where(table.c.session_id == identity).order_by(table.c.seq.desc()).limit(limit + 1)
        if kind == 'failed':
            statement = statement.where(table.c.status == 'failed')
        if cursor:
            statement = statement.where(table.c.seq < self.cursor(cursor))
        with self.store.connect() as db:
            rows = db.execute(statement).mappings().all()
        return {'items': [{**dict(row), **({'size_exact': str(row['size'])} if kind in ('files', 'test_files') else {})}
                          for row in rows[:limit]],
                'next_cursor': str(rows[limit - 1]['seq']) if len(rows) > limit else None}

    def update(self, identity, data):
        values = pricing(data)
        label = data.get('label', '')
        if not isinstance(label, str) or len(label) > 200:
            raise ValueError('客户 / 清单备注最多 200 个字符。')
        with self.store.connect() as db:
            db.execute(sessions.update().where(sessions.c.id == identity).values(**values, label=label.strip()))
        return self.session(identity)

    def set_defaults(self, data):
        values = pricing(data)
        with self.store.connect() as db:
            db.execute(self.db.upsert(settings, {'key': 'download_billing_defaults', 'value': json.dumps(values)}, ['key']))
        return values
