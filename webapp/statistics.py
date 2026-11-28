"""Transactional session totals and a ledger of completed video files."""
from datetime import datetime
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import uuid

from sqlalchemy import func, literal, or_, select

from webapp.database import deliveries, delivery_artifacts, download_sessions as sessions, session_tasks, settings, tasks, test_files, test_totals


COUNTERS = ('attempted', 'running', 'succeeded', 'failed', 'skipped', 'test_succeeded', 'file_count', 'file_bytes')
OUTCOMES = ('running', 'succeeded', 'failed', 'skipped', 'test_succeeded')
UNITS = {'GB': 1000 ** 3, 'GiB': 1024 ** 3}
DELIVERY_WINDOW_SECONDS = 24 * 60 * 60
DELIVERY_INTERVAL_SECONDS = 10 * 60
SESSION_OWNER_PREFIX = 'download_statistics_owner:'


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
    value['amount'] = None
    value['billing_scope'] = 'execution_only'
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
        from webapp.read_cache import ReadCache
        self._recent_cache = ReadCache(ttl=3)
        self._delivery_speed_cache = ReadCache(ttl=20)
        self._dashboard_cache = ReadCache(ttl=2)

    @staticmethod
    def _scope(instance_id=None):
        return str(instance_id or 'local')

    @classmethod
    def _active_key(cls, instance_id=None):
        return 'download_statistics_active:' + cls._scope(instance_id)

    def _active(self, db, instance_id=None):
        """Return the active session for one executor scope."""
        key = self._active_key(instance_id)
        value = db.execute(self.store._locked(select(settings.c.value).where(
            settings.c.key == key))).scalar_one_or_none()
        return value or ''

    def defaults(self, db):
        value = db.execute(select(settings.c.value).where(settings.c.key == 'download_billing_defaults')).scalar_one_or_none()
        return json.loads(value) if value else {'price': '0', 'unit': 'GB'}

    def dashboard(self):
        return self._dashboard_cache.read('totals', self._dashboard)

    def _dashboard(self):
        with self.store.connect() as db:
            sizes = db.execute(select(func.coalesce(func.sum(sessions.c.file_count), 0).label('file_count'),
                func.coalesce(func.sum(sessions.c.file_bytes), 0).label('file_bytes'))).mappings().one()
            testing = db.execute(select(func.coalesce(func.sum(test_totals.c.test_file_count), 0).label('test_file_count'),
                func.coalesce(func.sum(test_totals.c.test_file_bytes), 0).label('test_file_bytes'))).mappings().one()
        result = {key: int(value) for key, value in {**dict(sizes), **dict(testing)}.items()}
        return {**result, 'file_bytes_exact': str(result['file_bytes']), 'test_file_bytes_exact': str(result['test_file_bytes'])}

    def current_session(self, db, instance_id=None):
        """Workbench output rate uses this executor's session ledger and wall time."""
        identity = self._active(db, instance_id)
        if not identity:
            return None
        row = db.execute(session_select().where(sessions.c.id == identity,
            sessions.c.status.in_(('active', 'stopping')))).mappings().first()
        if not row:
            return None
        from webapp.fleet import database_time
        end = (datetime.fromisoformat(row['ended_at']).timestamp() if row['ended_at']
               else database_time(db, self.db.mysql))
        started = datetime.fromisoformat(row['started_at']).timestamp()
        elapsed = max(0, end - started)
        value = describe(row)
        return {**value, 'elapsed_seconds': round(elapsed, 3),
                'output_mbps': round(value['downloaded_file_bytes'] * 8 / elapsed / 1e6, 2) if elapsed > 0 else None,
                **self.recent_output(db, identity, started, end)}

    def recent_output(self, db, identity, started, end):
        return self._recent_cache.read((identity, started, int(end // 3)),
            lambda: self._recent_output(db, identity, started, end))

    def _recent_output(self, db, identity, started, end):
        recent_start = max(started, end - 600)
        recent_seconds = max(0, end - recent_start)
        recent_bytes = 0
        if recent_seconds:
            for ledger in (deliveries, test_files):
                recent_bytes += int(db.execute(select(func.coalesce(func.sum(ledger.c.size), 0)).where(
                    ledger.c.session_id == identity, ledger.c.created_epoch >= started,
                    ledger.c.created_epoch > end - 600, ledger.c.created_epoch <= end)).scalar_one())
        return dict(output_recent_mbps=round(recent_bytes * 8 / recent_seconds / 1e6, 2) if recent_seconds else None,
                    output_recent_seconds=round(recent_seconds, 3), output_recent_file_bytes_exact=str(recent_bytes))

    def delivery_speed(self, instance_id=None):
        scope = self._scope(instance_id)
        return self._delivery_speed_cache.read(scope, lambda: self._delivery_speed(scope))

    def _delivery_speed(self, scope):
        from webapp.fleet import database_time
        interval = DELIVERY_INTERVAL_SECONDS
        with self.store.connect() as db:
            end = database_time(db, self.db.mysql)
            start = end - DELIVERY_WINDOW_SECONDS
            identities = self._delivery_sessions(db, scope, start, end)
            totals = {}
            # 会话已确定归属，按会话和完成时间的联合索引扫描账本，无需逐条查询产物。
            for ledger in (deliveries, test_files) if identities else ():
                bucket = func.floor(ledger.c.created_epoch / interval)
                rows = db.execute(select(bucket, func.sum(ledger.c.size), func.count()).where(
                    ledger.c.session_id.in_(identities), ledger.c.created_epoch >= start, ledger.c.created_epoch < end
                ).group_by(bucket))
                for index, size, count in rows:
                    index = int(index)
                    previous_size, previous_count = totals.get(index, (0, 0))
                    totals[index] = previous_size + int(size), previous_count + count
        points = []
        for aligned_start in range(int(start // interval) * interval, int(end) + 1, interval):
            # 首尾按滚动窗口截断，未满十分钟的区间按实际覆盖时长计算。
            begin = max(start, aligned_start)
            finish = min(end, aligned_start + interval)
            elapsed = finish - begin
            if elapsed <= 0:
                continue
            size, count = totals.get(aligned_start // interval, (0, 0))
            points.append(dict(start_at=begin, end_at=finish, file_bytes_exact=str(size), file_count=count,
                               mbps=round(size * 8 / elapsed / 1e6, 2), partial=elapsed < interval))
        return dict(start_at=start, end_at=end, interval_seconds=interval, points=points)

    def _delivery_sessions(self, db, scope, start, end):
        def window(ledger):
            return (ledger.c.session_id == sessions.c.id, ledger.c.created_epoch >= start, ledger.c.created_epoch < end)

        def legacy_owner(ledger):
            # 旧会话最多读取一条交付回执来确定归属。文件已删除也可使用账本记录。
            source = (ledger.outerjoin(delivery_artifacts, delivery_artifacts.c.id == ledger.c.id)
                      .outerjoin(tasks, (tasks.c.id == ledger.c.task_id) & delivery_artifacts.c.id.is_(None)))
            return (select(func.coalesce(delivery_artifacts.c.instance_id, tasks.c.instance_id)).select_from(source)
                    .where(*window(ledger)).order_by(ledger.c.created_epoch).limit(1).correlate(sessions).scalar_subquery())

        active = {value: key.split(':', 1)[1] for key, value in db.execute(select(settings.c.key, settings.c.value)
            .where(settings.c.key.startswith('download_statistics_active:'))) if value}
        owner = func.coalesce(settings.c.value, legacy_owner(deliveries), legacy_owner(test_files), 'local')
        source = sessions.outerjoin(settings, settings.c.key == literal(SESSION_OWNER_PREFIX) + sessions.c.id)
        relevant = or_(*(select(ledger.c.id).where(*window(ledger)).exists() for ledger in (deliveries, test_files)))
        return [identity for identity, recorded_owner in db.execute(select(sessions.c.id, owner).select_from(source)
                .where(relevant)) if active.get(identity, recorded_owner) == scope]

    def _create_session(self, db, scope):
        identity = uuid.uuid4().hex
        db.execute(sessions.insert().values(id=identity, seq=self.db.sequence(db, 'download_session'),
            started_at=stamp(), status='active', label=''))
        # 会话由实例独享。保存首次归属，跨会话或文件回收后仍可直接按账本汇总。
        db.execute(settings.insert().values(key=SESSION_OWNER_PREFIX + identity, value=scope))
        key = self._active_key(scope)
        self.db.ensure(db, settings, {'key': key, 'value': identity})
        db.execute(settings.update().where(settings.c.key == key).values(value=identity))
        return identity

    def ensure_session(self, db, instance_id=None):
        scope = self._scope(instance_id)
        identity = self._active(db, scope)
        if identity:
            row = db.execute(select(sessions.c.status).where(sessions.c.id == identity)).scalar_one_or_none()
            if row in ('active', 'stopping'):
                return identity
        return self._create_session(db, scope)

    def start_session(self, db, instance_id=None):
        """Start the lifecycle session for an instance if one is not active."""
        scope = self._scope(instance_id)
        existing = self._active(db, scope)
        if existing:
            status = db.execute(select(sessions.c.status).where(sessions.c.id == existing)).scalar_one_or_none()
            if status == 'active':
                return existing
            db.execute(sessions.update().where(sessions.c.id == existing, sessions.c.ended_at.is_(None)).values(
                status='paused', ended_at=stamp()))
        return self._create_session(db, scope)

    def stop_session(self, db, instance_id=None):
        """Mark an instance session as stopping and close it when idle."""
        scope = self._scope(instance_id)
        identity = self._active(db, scope)
        if not identity:
            return
        row = db.execute(select(sessions).where(sessions.c.id == identity).with_for_update()).mappings().first()
        if not row:
            db.execute(settings.update().where(settings.c.key == self._active_key(scope)).values(value=''))
            return
        if row['status'] == 'active':
            db.execute(sessions.update().where(sessions.c.id == identity).values(
                status='stopping', stop_requested_at=stamp()))
            row = {**row, 'status': 'stopping'}
        if not row['running']:
            db.execute(sessions.update().where(sessions.c.id == identity).values(
                status='paused', ended_at=stamp()))
            db.execute(settings.update().where(settings.c.key == self._active_key(scope)).values(value=''))

    def stop_all(self, db):
        """Stop every instance session for a global pause/stop command."""
        keys = db.execute(select(settings.c.key).where(
            settings.c.key.startswith('download_statistics_active:'))).scalars().all()
        for key in keys:
            self.stop_session(db, key.split(':', 1)[1])

    def begin(self, db, row, identity):
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

    def transition(self, db, row, status, error=None):
        if row['action'] != 'download':
            return
        identity = self.store.decode(row, private=True, db=db)['payload'].get('download_session_id')
        if not identity:
            return
        self._active(db, row.get('instance_id'))  # Use one lock order across completion, pause and file registration.
        old = db.execute(select(session_tasks).where(session_tasks.c.session_id == identity,
            session_tasks.c.task_id == row['id'])).mappings().first()
        if not old or old['status'] == status:
            return
        db.execute(session_tasks.update().where(session_tasks.c.session_id == identity,
            session_tasks.c.task_id == row['id']).values(status=status, error=error,
                finished_at=None if status == 'running' else stamp()))
        changes = {}
        if old['status'] in OUTCOMES:
            changes[old['status']] = sessions.c[old['status']] - 1
        if status in OUTCOMES:
            changes[status] = sessions.c[status] + 1
        if changes:
            db.execute(sessions.update().where(sessions.c.id == identity).values(**changes))

    def settle(self, db, *, stop=False, instance_id=None):
        # Sessions follow instance start/stop.  A completed queue does not end
        # the session because the instance may be waiting for the next task.
        if stop:
            self.stop_session(db, instance_id)
            return
        identity = self._active(db, instance_id)
        if not identity:
            return
        row = db.execute(select(sessions).where(sessions.c.id == identity)).mappings().one()
        if row['status'] == 'stopping' and not row['running']:
            db.execute(sessions.update().where(sessions.c.id == identity).values(
                status='paused', ended_at=stamp()))
            db.execute(settings.update().where(settings.c.key == self._active_key(instance_id)).values(value=''))

    def file_identity(self, task, path, *, test_mode=False):
        """Use the same durable identity for registration and receipt recovery."""
        logical = (task['id'] + ('\0test' if test_mode else '\0delivery')) if (
            self.store.fleet or (task['source_key'] or '').startswith('job:')) else (
            (task['id'] + '\0' if test_mode else '') + str(path))
        return hashlib.sha256(logical.encode('utf-8')).hexdigest()

    def record_file(self, task_id, path, video_id='', title='', *, test_mode=False, artifact=None):
        """Called only after successful final-file processing, never from progress bytes."""
        path = Path(path).resolve()
        size = path.stat().st_size
        if not size:
            return False
        from webapp.db_retry import transaction_retry
        # Retry only a completely rolled-back ledger transaction. The caller's
        # download, final-file processing and receipt publication run just once.
        recorded = transaction_retry(lambda: self._record_file(
            task_id, path, size, video_id, title, test_mode=test_mode, artifact=artifact))
        if recorded:
            self._recent_cache.clear()
            self._dashboard_cache.clear()
        return recorded

    def record_files(self, task_id, files, *, test_mode=False):
        """Register a bounded publication batch in one retriable transaction."""
        from webapp.db_retry import transaction_retry
        prepared = [{**item, 'path': Path(item['path']).resolve(),
                     'size': Path(item['path']).stat().st_size} for item in files]
        if len(prepared) > 16 or any(item['size'] <= 0 for item in prepared):
            raise ValueError('文件登记批次无效。')
        def commit():
            with self.store.connect() as db:
                if self.db.sqlite:
                    db.exec_driver_sql('BEGIN IMMEDIATE')
                return [self._record_file(task_id, item['path'], item['size'],
                    item.get('video_id', ''), item.get('title', ''), test_mode=test_mode,
                    artifact=item.get('artifact'), _db=db) for item in prepared]
        result = transaction_retry(commit)
        if any(result):
            self._recent_cache.clear()
            self._dashboard_cache.clear()
        return result

    def _record_file(self, task_id, path, size, video_id, title, *, test_mode, artifact=None, _db=None):
        # A delivered path is registered once across retries and sessions.
        ledger = test_files if test_mode else deliveries
        with (self.store.connect() if _db is None else nullcontext(_db)) as db:
            if self.db.sqlite and _db is None:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            task = db.execute(self.store._locked(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            if not task or task['status'] != 'running' or task['worker_id'] != self.store.owner:
                from webapp.downloads import DownloadLeaseLost
                raise DownloadLeaseLost('任务租约已失效，无法登记下载文件。')
            if artifact:
                from webapp.delivery.artifacts import fenced_run
                fenced_run(self.store, db, task_id)
            if self.store.fleet and (not task['lease_until'] or task['lease_until'] <= self.store.fleet.clock(db)
                    or self.store.fleet.row(db)['boot_id'] != self.store.fleet.boot_id):
                from webapp.downloads import DownloadLeaseLost
                raise DownloadLeaseLost('任务租约已过期，无法登记下载文件。')
            session_id = self.store.decode(task, private=True, db=db)['payload'].get('download_session_id')
            if not session_id:
                raise ValueError('任务缺少下载会话，无法登记下载文件。')
            identity = artifact['id'] if artifact else self.file_identity(task, path, test_mode=test_mode)
            self._active(db, task.get('instance_id'))
            if db.execute(select(ledger.c.id).where(ledger.c.id == identity)).first():
                if artifact:
                    from webapp.delivery.artifacts import receipt
                    receipt(self.store, db, task, artifact, path, size, fresh=False)
                return False
            completed_at = stamp()
            db.execute(ledger.insert().values(id=identity, seq=self.db.sequence(db, 'download_test_file' if test_mode else 'download_delivery'),
                session_id=session_id, task_id=task_id, video_id=video_id or task.get('video_id'),
                title=str(title or video_id or task.get('video_id') or path.stem), path=str(path), size=size,
                created_at=completed_at, created_epoch=datetime.fromisoformat(completed_at).timestamp()))
            if test_mode:
                self.db.ensure(db, test_totals, {'session_id': session_id, 'test_file_count': 0, 'test_file_bytes': 0})
                db.execute(test_totals.update().where(test_totals.c.session_id == session_id).values(
                    test_file_count=test_totals.c.test_file_count + 1, test_file_bytes=test_totals.c.test_file_bytes + size))
            else:
                db.execute(sessions.update().where(sessions.c.id == session_id).values(
                    file_count=sessions.c.file_count + 1, file_bytes=sessions.c.file_bytes + size))
            if artifact:
                from webapp.delivery.artifacts import receipt
                receipt(self.store, db, task, artifact, path, size, fresh=True)
            self.store.lists.record_file(db, task, size, test_mode)
            if self.store.fleet:
                scope = 'instance:' + self.store.fleet.id
                prefix = 'test_file' if test_mode else 'file'
                self.store._counter(db, scope, prefix + '_count', 1)
                self.store._counter(db, scope, prefix + '_bytes', size)
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
            from webapp.billing import BillingService
            billing = BillingService(self.store).summary(db)
            active_id = db.execute(select(sessions.c.id).where(sessions.c.status.in_(('active', 'stopping')))
                .order_by(sessions.c.seq.desc()).limit(1)).scalar_one_or_none()
            active = db.execute(session_select().where(sessions.c.id == active_id, condition)).mappings().first() if active_id else None
            since = db.execute(select(settings.c.value).where(settings.c.key == 'download_statistics_since')).scalar_one()
            test_since = db.execute(select(settings.c.value).where(settings.c.key == 'download_test_statistics_since')).scalar_one()
            defaults = self.defaults(db)
        return {'summary': {**with_test_sizes({**{key: int(value) for key, value in totals.items()}, **test_summary}),
                            'file_bytes_exact': str(int(totals['file_bytes'])),
                            'amount': billing['amount'], 'unpriced_bytes': billing['unpriced_bytes'],
                            'billing_scope': billing['scope']},
                'active': describe(active) if active else None,
                'items': [describe(row) for row in rows[:20]], 'next_cursor': str(rows[19]['seq']) if len(rows) > 20 else None,
                'pricing': defaults, 'tracking_since': since, 'test_tracking_since': test_since,
                'billing': billing}


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
        if set(data) - {'label'}:
            raise ValueError('会话仅记录运行情况；请在任务中心调整任务单价和计费单位。')
        label = data.get('label', '')
        if not isinstance(label, str) or len(label) > 200:
            raise ValueError('客户 / 清单备注最多 200 个字符。')
        with self.store.connect() as db:
            db.execute(sessions.update().where(sessions.c.id == identity).values(label=label.strip()))
        return self.session(identity)

    def set_defaults(self, data):
        values = pricing(data)
        with self.store.connect() as db:
            db.execute(self.db.upsert(settings, {'key': 'download_billing_defaults', 'value': json.dumps(values)}, ['key']))
        return values
