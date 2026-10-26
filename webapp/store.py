"""Indexed, paginated task store with transactional counters and leased claims."""
import base64
import codecs
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import OperationalError

from webapp.database import (Database, archive, batches, counters, download_lists, imports, reservations,
                             rotations, settings, snapshots, tasks, workers, job_profiles, job_imports, job_removed)


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def source_identity(url):
    parsed = urlsplit(url or '')
    host = (parsed.hostname or '').lower().removeprefix('www.')
    video_id = None
    if host in ('youtube.com', 'm.youtube.com', 'music.youtube.com'):
        video_id = parse_qs(parsed.query).get('v', [None])[0]
        if not video_id and parsed.path.startswith(('/shorts/', '/live/')):
            video_id = parsed.path.split('/')[2]
    elif host == 'youtu.be':
        video_id = parsed.path.strip('/')
    return video_id, ('youtube:' + video_id if video_id else 'url:' + hashlib.sha256((url or '').encode()).hexdigest())


class TaskStore:
    def __init__(self, directory, database_url=None, *, startup_report=None):
        self.db = Database(directory, database_url, startup_report=startup_report)
        self.directory = self.db.directory
        self.database = self.directory / 'tasks.sqlite3'
        self.owner = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}'
        self.fleet = None
        from webapp.modes import single_state
        self.single = single_state(self.directory)
        runtime = self.directory / 'fleet-runtime.json'
        if runtime.is_file():
            from webapp.fleet import Fleet
            saved = json.loads(runtime.read_text(encoding='utf-8'))
            fleet = Fleet(self, saved['role'], identity=saved['id'])
            fleet.boot_id = saved['boot_id']
        from webapp.statistics import DownloadStatistics
        self.statistics = DownloadStatistics(self)
        from webapp.lists import DownloadLists
        self.lists = DownloadLists(self)

    def connect(self):
        return self.db.engine.begin()

    def decode(self, row, private=False, db=None):
        if row is None:
            return None
        task = dict(row)
        payload = json.loads(task.pop('payload'))
        task['result'] = json.loads(task['result']) if task.get('result') else None
        task['progress'] = json.loads(task['progress']) if task.get('progress') else None
        url = task.get('url') or (payload.get('urls') or [None])[0]
        task.update(batch_id=task.get('batch_id') or payload.get('batch_id'), url=url,
                    video_id=task.get('video_id') or source_identity(url)[0])
        if private:
            snapshot_id = payload.pop('snapshot_id', None)
            if snapshot_id:
                if db is None:
                    with self.connect() as connection:
                        saved = connection.execute(select(snapshots.c.payload).where(snapshots.c.id == snapshot_id)).scalar_one()
                else:
                    saved = db.execute(select(snapshots.c.payload).where(snapshots.c.id == snapshot_id)).scalar_one()
                payload = {**json.loads(saved), **payload}
            task['payload'] = payload
        return task

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

    def prepare_snapshot(self, payload):
        common = {key: value for key, value in payload.items() if key not in ('urls', 'batch_id', 'import_id')}
        encoded = json.dumps(common, ensure_ascii=False, sort_keys=True)
        identity = hashlib.sha256(encoded.encode()).hexdigest()
        return identity, encoded

    def _pack(self, db, payload):
        identity, encoded = self.prepare_snapshot(payload)
        self.db.ensure(db, snapshots, {'id': identity, 'payload': encoded})
        return json.dumps({**{key: value for key, value in payload.items() if key in ('urls', 'batch_id', 'import_id')},
                           'snapshot_id': identity}, ensure_ascii=False)

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
                                        'created_at': now(), 'name': payload.get('batch_name', '')})
        values = dict(id=task_id, seq=seq, action=action, status='queued', created_at=now(),
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
                for old in db.execute(select(tasks).where(tasks.c.batch_id.in_(managed), tasks.c.action == 'download',
                        or_(tasks.c.video_id.in_(video_ids),tasks.c.url.in_(urls[offset:offset+1000]))).with_for_update()).mappings():
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
            rows.append(dict(id=task_id, seq=start + index, action='download', status='queued', created_at=now(),
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
            self.db.ensure(db, batches, {'id': batch_id, 'seq': self.db.sequence(db, 'batch'), 'created_at': now(),
                                        'name': payloads[0].get('batch_name', '')})
        db.execute(tasks.insert(), rows)
        self._counter(db, 'action:download', 'queued', len(rows))
        for batch_id, count in Counter(row['batch_id'] for row in rows if row['batch_id']).items():
            self._counter(db, 'batch:' + batch_id, 'queued', count)
        return restored_ids + [row['id'] for row in rows]

    def get(self, task_id, private=False):
        with self.connect() as db:
            return self.decode(db.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first(), private, db)

    def page(self, *, limit=50, cursor=None, actions=None, status=None, query='', batch_id=None, order='desc', finished_only=False):
        if not 1 <= limit <= 100:
            raise ValueError('每页条数必须在 1 到 100 之间。')
        statement = select(tasks)
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
            return {'items': [self.decode(row) for row in rows[:limit]],
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
            if self.downloads_paused(db):
                statement = statement.where(tasks.c.action != 'download')
            elif not self._released_queued_count(db):
                statement = statement.where(or_(tasks.c.action != 'download', tasks.c.status == 'running'))
            return db.execute(statement.limit(1)).first() is not None

    def _released_queued_count(self, db):
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
                return bool(running or (not self.fleet.row(db)['paused'] and not self.downloads_paused(db)
                                        and self._released_queued_count(db)))
        with self.connect() as db:
            downloads, checks = self._counts(db, ['action:download']), self._counts(db, ['action:check'])
            return bool(downloads.get('running', 0) or checks.get('running', 0) or checks.get('queued', 0)
                        or (not self.downloads_paused(db) and self._released_queued_count(db)))

    def runnable_roles(self):
        """Dispatch only queues with a due task, including after retry backoff."""
        with self.connect() as db:
            paused = self.downloads_paused(db)
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
            paused = self.downloads_paused(db)
            counts = {}
            stamp = self.fleet.clock(db) if self.fleet else time.time()
            for role, actions in (('download', ('download',)), ('maintenance', ('import', 'index')),
                                  ('regular', ('check',))):
                if self.fleet and role == 'download' and (self.fleet.role == 'manager' or not self.fleet.admission(db, lock=False)):
                    continue
                if not self._counts(db, ['action:' + action for action in actions]).get('queued', 0):
                    continue
                if role == 'download' and (paused or not self._released_queued_count(db)):
                    continue
                # Only the number of available worker slots matters, not the
                # size of a potentially multi-million-video backlog.
                statement = select(tasks.c.id).where(tasks.c.action.in_(actions),
                    tasks.c.status == 'queued', tasks.c.available_at <= stamp, self.released_condition()).limit(16)
                if self.fleet:
                    statement = statement.where(or_(tasks.c.target_instance_id == self.fleet.id,
                        and_(tasks.c.target_instance_id.is_(None), tasks.c.action.in_(self.fleet.untargeted_actions()))))
                count = len(db.execute(statement).all())
                if count:
                    counts[role] = count
            return counts

    def downloads_paused(self, db=None, *, lock=False):
        if db is None:
            with self.connect() as connection:
                return self.downloads_paused(connection)
        statement = select(settings.c.value).where(settings.c.key == 'downloads_paused')
        if lock and not self.db.sqlite:
            statement = statement.with_for_update(read=True)
        return db.execute(statement).scalar_one() == 'true'

    @staticmethod
    def released_condition():
        return or_(tasks.c.action != 'download', ~select(download_lists.c.id).where(
            download_lists.c.id == tasks.c.batch_id, download_lists.c.released == 0).exists())

    def set_downloads_paused(self, paused):
        with self.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='true' if paused else 'false'))
            if paused:
                self.statistics.settle(db, stop=True)

    def activate_download_lists(self, expected_policy_version=None):
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            db.execute(select(settings.c.key).where(settings.c.key == 'downloads_paused').with_for_update())
            if self.media_clear_busy(db):
                raise ValueError('素材库正在清空，请完成后再开始下载。')
            if self.fleet:
                self.fleet.activate(db, expected_policy_version)
            # Release the new lists and resume claims in one transaction.
            self.lists.release(db)
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='false'))

    def media_clear_busy(self, db=None):
        if db is None:
            with self.connect() as connection:
                return self.media_clear_busy(connection)
        counts = self._counts(db, ['action:clear_media'])
        return bool(counts.get('queued', 0) or counts.get('running', 0))

    def start_media_clear(self, payload):
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            db.execute(select(settings.c.key).where(settings.c.key == 'downloads_paused').with_for_update())
            if not self.downloads_paused(db) or self._counts(db, ['action:download']).get('running', 0):
                raise ValueError('请先暂停全部下载，并等待所有实例的下载任务停止后再清空素材库。')
            if self.media_clear_busy(db):
                raise ValueError('素材库正在清空，请勿重复提交。')
            indexing = self._counts(db, ['action:index'])
            if indexing.get('queued', 0) or indexing.get('running', 0):
                raise ValueError('请等待素材索引同步完成后再清空。')
            task_id = self._create(db, 'clear_media', payload, self.db.sequence(db, 'task'))
            row = db.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one()
            self._transition(db, row, 'running')
            db.execute(tasks.update().where(tasks.c.id == task_id).values(
                status='running', started_at=now(), worker_id=self.owner,
                instance_id=self.fleet.id if self.fleet else None,
                lease_until=(self.fleet.clock(db) if self.fleet else time.time()) + 60, attempts=1))
        return self.get(task_id)

    def download_queue(self):
        with self.connect() as db:
            counts = self._counts(db, ['action:download'])
            importing = self._counts(db, ['action:import'])
            waiting_lists = db.execute(select(func.count()).select_from(download_lists).where(
                download_lists.c.released == 0, or_(select(counters.c.scope).where(
                    counters.c.scope == 'batch:' + download_lists.c.id, counters.c.status == 'queued', counters.c.value > 0).exists(),
                    select(imports.c.id).where(imports.c.id == download_lists.c.id, imports.c.finished == 0).exists()))).scalar_one()
            return {'paused': self.downloads_paused(db), 'queued': counts.get('queued', 0), 'running': counts.get('running', 0),
                    'waiting_lists': waiting_lists, 'importing': importing.get('running', 0) + importing.get('queued', 0)}

    def apply_download_connection(self, config, clash=None):
        """Override existing jobs at execution time without rewriting their snapshots."""
        connection = {'proxy_mode': config.get('proxy_mode', 'manual'), 'proxy': config.get('proxy', '')}
        if connection['proxy_mode'] == 'clash' and not clash:
            raise ValueError('请先配置并启动 Clash 代理。')
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            if not self.downloads_paused(db, lock=True):
                raise ValueError('请先停止全部下载，再应用当前连接。')
            counts = self._counts(db, ['action:download'])
            if counts.get('running', 0):
                raise ValueError('当前下载进程尚未停止，请稍后再应用当前连接。')
            importing = self._counts(db, ['action:import'])
            if not (counts.get('queued', 0) or importing.get('queued', 0) or importing.get('running', 0)):
                raise ValueError('当前没有排队的下载或导入任务。')
            cutoff = db.execute(select(func.max(tasks.c.seq))).scalar_one() or 0
            value = {'cutoff_seq': cutoff, 'config': connection, 'updated_at': now()}
            if connection['proxy_mode'] == 'clash':
                value['clash'] = clash
            db.execute(self.db.upsert(settings, dict(key=self._connection_key(),
                value=json.dumps(value, ensure_ascii=False)), ['key']))
        return {'mode': connection['proxy_mode'], 'updated_at': value['updated_at']}

    def _connection_key(self):
        prefix = ('i:' + hashlib.sha256(self.fleet.id.encode()).hexdigest()[:16] + ':') if self.fleet else ''
        return prefix + 'download_connection_override'

    def download_connection(self):
        """Public summary; proxy credentials and controller paths stay private."""
        with self.connect() as db:
            saved = db.execute(select(settings.c.value).where(
                settings.c.key == self._connection_key())).scalar_one_or_none()
        value = json.loads(saved) if saved else None
        return {'mode': value['config']['proxy_mode'], 'updated_at': value['updated_at']} if value else None

    def set_download_settings(self, config, base, clash_directory):
        """One current configuration shared by all pending downloads."""
        value = {'config': config, 'base': str(base), 'clash_directory': str(clash_directory)}
        with self.connect() as db:
            db.execute(self.db.upsert(settings, dict(key='download_settings',
                value=json.dumps(value, ensure_ascii=False)), ['key']))

    def effective_download_payload(self, task_id, payload, db=None):
        # A claimed task keeps its start configuration until this attempt ends.
        if payload.get('_execution_settings'):
            return payload
        if not task_id:
            return payload
        if db is None:
            with self.connect() as connection:
                return self.effective_download_payload(task_id, payload, connection)
        if self.fleet:
            from webapp.jobs import apply_policy
            return apply_policy(self, db, task_id, self.fleet.execution(payload, db))
        current = db.execute(select(settings.c.value).where(settings.c.key == 'download_settings')).scalar_one_or_none()
        if current:
            value = json.loads(current)
            result = {**payload, 'config': value['config'], 'base': value['base']}
            result.pop('clash', None)
            if value['config'].get('proxy_mode') == 'clash':
                result['clash'] = {'directory': value['clash_directory']}
            from webapp.jobs import apply_policy
            return apply_policy(self, db, task_id, result)
        # Compatibility for standalone stores without a Web configuration.
        saved = db.execute(select(settings.c.value).where(
            settings.c.key == 'download_connection_override')).scalar_one_or_none()
        if not saved:
            return payload
        value = json.loads(saved)
        eligible = db.execute(select(tasks.c.id).where(tasks.c.id == task_id,
            tasks.c.seq <= value['cutoff_seq'], tasks.c.action.in_(('download', 'import')))).first()
        if not eligible:
            return payload
        result = {**payload, 'config': {**payload.get('config', {}), **value['config']}}
        result.pop('clash', None)
        if value.get('clash'):
            result['clash'] = value['clash']
        return result

    def stop_running_downloads(self, terminate):
        """Stop only verified owners, then release their tasks under the same row lock."""
        with self.connect() as db:
            statement = select(tasks.c.id, tasks.c.worker_id).where(tasks.c.status == 'running', tasks.c.action == 'download')
            if self.fleet:
                statement = statement.where(tasks.c.instance_id == self.fleet.id)
            owners = db.execute(statement.limit(64)).all()
        stopped = 0
        for task_id, owner in owners:
            with self.connect() as db:
                if self.db.sqlite:
                    db.exec_driver_sql('BEGIN IMMEDIATE')
                # Resume and new claims cannot pass until the old executor is gone.
                if not self.downloads_paused(db, lock=True) and not (self.fleet and self.fleet.row(db)['stop_requested']):
                    break
                row = db.execute(self._locked(select(tasks).where(tasks.c.id == task_id))).mappings().first()
                if not row or row['status'] != 'running' or row['worker_id'] != owner or not terminate(owner):
                    continue
                progress = json.loads(row['progress']) if row['progress'] else {}
                progress.update(stage='paused', speed=None, eta=None)
                self._transition(db, row, 'queued')
                db.execute(tasks.update().where(tasks.c.id == task_id).values(status='queued', worker_id=None,
                    lease_until=None, available_at=0, finished_at=None, error=None,
                    attempts=max(0, row['attempts'] - 1), progress=json.dumps(progress, ensure_ascii=False)))
                self.statistics.transition(db, row, 'paused')
                self.statistics.settle(db)
                if owner:
                    db.execute(workers.update().where(workers.c.id == owner).values(state='stopped', task_id=None, message='用户已停止下载。'))
                stopped += 1
        return stopped

    def rotation_completed(self, rotation_id, count_failures=True):
        with self.connect() as db:
            row = db.execute(select(rotations).where(rotations.c.id == rotation_id)).mappings().first()
            return (row['succeeded'] + (row['failed'] if count_failures else 0)) if row else 0

    def _locked(self, statement, *, skip=False):
        # MySQL 5.7 waits for the row lock, then rechecks the queued predicate.
        # Claim transactions commit before downloads start, so only dispatch
        # waits; workers still download concurrently without duplicate claims.
        return statement if self.db.sqlite else statement.with_for_update(
            skip_locked=skip and self.db.supports_skip_locked)

    def claim(self, worker_id=None, actions=None, exclude=None, lease_seconds=60):
        # A blocking 5.7 locking scan can deadlock with a transaction changing
        # the queue's status index. InnoDB rolls back the losing transaction;
        # retry the entire claim (including counters), never just its SELECT.
        for attempt in range(5):
            try:
                return self._claim(worker_id, actions, exclude, lease_seconds)
            except OperationalError as error:
                code = error.orig.args[0] if error.orig.args else None
                if not self.db.mysql or code != 1213 or attempt == 4:
                    raise
                time.sleep(0.01 * 2 ** attempt)

    def _claim(self, worker_id, actions, exclude, lease_seconds):
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            stamp = self.fleet.clock(db) if self.fleet else time.time()
            if self.fleet or self.single:
                db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget').with_for_update()).first()
            mode = db.execute(select(settings.c.value).where(settings.c.key == 'fleet_enabled')).scalar_one_or_none()
            if self.fleet and mode != 'true':
                return None
            if self.fleet:
                from webapp.modes import single_state
                if (single_state(self.directory) or {}).get('status') == 'requested':
                    return None  # Freeze local claims until the requested restart.
            if not self.fleet and mode is not None:
                from webapp.modes import mode_key
                if mode != mode_key(self.single):
                    return None
            statement = select(tasks).where(tasks.c.status == 'queued', tasks.c.available_at <= stamp)
            if self.fleet:
                statement = statement.where(or_(tasks.c.target_instance_id == self.fleet.id,
                    and_(tasks.c.target_instance_id.is_(None), tasks.c.action.in_(self.fleet.untargeted_actions()))))
                if self.fleet.role == 'manager' or not self.fleet.admission(db):
                    statement = statement.where(tasks.c.action != 'download')
            # Destructive cleanup runs only in the Web instance that accepted confirmation.
            statement = statement.where(tasks.c.action != 'clear_media')
            statement = statement.where(self.released_condition())
            if self.downloads_paused(db, lock=True) or not self._released_queued_count(db):
                statement = statement.where(tasks.c.action != 'download')
            from webapp.disk_budget import resource_blocked
            if resource_blocked(self, db):
                statement = statement.where(tasks.c.action != 'download')
            if self.media_clear_busy(db):
                statement = statement.where(~tasks.c.action.in_(('download', 'index')))
            if actions:
                statement = statement.where(tasks.c.action.in_(actions))
            if exclude:
                statement = statement.where(~tasks.c.action.in_(exclude))
            row = db.execute(self._locked(statement.order_by(tasks.c.available_at, tasks.c.seq).limit(1), skip=True)).mappings().first()
            if row is None:
                return None
            self._transition(db, row, 'running')
            values = dict(status='running', worker_id=worker_id or self.owner,
                          started_at=now(), finished_at=None, lease_until=time.time() + lease_seconds,
                          attempts=row['attempts'] + 1, error=None)
            if self.fleet:
                values.update(instance_id=self.fleet.id, lease_until=self.fleet.clock(db) + lease_seconds)
            if row['action'] == 'download':
                payload = self.decode(row, private=True, db=db)['payload']
                previous_config = (payload.get('config', {})
                                   if payload.get('_execution_settings') or row['attempts'] else None)
                previous_method = previous_config.get('download_method', 'yt_dlp') if previous_config is not None else None
                payload.pop('_execution_settings', None)
                payload.pop('download_session_id', None)
                payload = self.effective_download_payload(row['id'], payload, db)
                payload['_execution_settings'] = True
                transfer_keys = ('download_method', 'download_methods', 'download_format', 'yt5s_quality', 'downmp3_quality')
                payload['_restart_transfer'] = bool(payload.get('_restart_transfer') or (
                    previous_config is not None and any(
                        previous_config.get(key) != payload.get('config', {}).get(key) for key in transfer_keys)))
                if self.fleet and row.get('instance_id') and row['instance_id'] != self.fleet.id:
                    payload['_restart_transfer'] = True
                values['max_attempts'] = int(payload.get('config', {}).get('download_attempts', row['max_attempts']))
                values['policy_version'] = payload.get('policy_version')
                row = {**row, 'payload': self._pack(db, payload), '_previous_download_method': previous_method}
                identity = self.statistics.begin(db, row)
                values['payload'] = json.dumps({**json.loads(row['payload']), 'download_session_id': identity}, ensure_ascii=False)
                row.pop('_previous_download_method', None)
            db.execute(tasks.update().where(tasks.c.id == row['id']).values(**values))
            return self.decode({**row, **values}, private=True, db=db)

    def heartbeat(self, task_id, worker_id, lease_seconds=60):
        with self.connect() as db:
            if self.fleet and self.fleet.row(db)['boot_id'] != self.fleet.boot_id:
                return False
            stamp = self.fleet.clock(db) if self.fleet else time.time()
            statement = tasks.update().where(tasks.c.id == task_id, tasks.c.status == 'running',
                                             tasks.c.worker_id == worker_id)
            if self.fleet:
                statement = statement.where(tasks.c.lease_until > stamp)
            return db.execute(statement.values(lease_until=stamp + lease_seconds)).rowcount > 0

    def release(self, task_id, worker_id, reason, delay=30, *, resource=False):
        with self.connect() as db:
            row = db.execute(self._locked(select(tasks).where(tasks.c.id == task_id,
                             tasks.c.worker_id == worker_id, tasks.c.status == 'running'))).mappings().first()
            if row:
                if self.fleet and (self.fleet.row(db)['boot_id'] != self.fleet.boot_id
                                  or not row['lease_until'] or row['lease_until'] <= self.fleet.clock(db)):
                    return False
                self._transition(db, row, 'queued')
                db.execute(tasks.update().where(tasks.c.id == task_id).values(status='queued', worker_id=None,
                    lease_until=None, attempts=max(0, row['attempts'] - 1),
                    available_at=(self.fleet.clock(db) if self.fleet else time.time()) + delay, error=reason))
                if resource:
                    progress = json.loads(row['progress']) if row.get('progress') else {}
                    progress.update(stage='disk_wait', speed=None, eta=None)
                    db.execute(tasks.update().where(tasks.c.id == task_id).values(progress=json.dumps(progress)))
                self.statistics.transition(db, row, 'paused', reason)
                self.statistics.settle(db)

    def update_progress(self, task_id, progress, worker_id=None):
        statement = tasks.update().where(tasks.c.id == task_id)
        if worker_id:
            statement = statement.where(tasks.c.worker_id == worker_id, tasks.c.status == 'running')
        values = {'progress': json.dumps(progress, ensure_ascii=False)}
        if progress.get('title'):
            values['title'] = str(progress['title']).casefold()
        with self.connect() as db:
            if self.fleet and worker_id:
                if self.fleet.row(db)['boot_id'] != self.fleet.boot_id:
                    return False
                statement = statement.where(tasks.c.lease_until > self.fleet.clock(db))
            return db.execute(statement.values(**values)).rowcount > 0

    def finish(self, task_id, *, result=None, error=None, worker_id=None, retryable=False):
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            row = db.execute(self._locked(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            if not row or row['status'] not in ('queued', 'running'):
                return False
            if worker_id and (row['worker_id'] != worker_id or row['status'] != 'running'):
                return False
            if self.fleet and worker_id and (not row['lease_until'] or row['lease_until'] <= self.fleet.clock(db)
                    or self.fleet.row(db)['boot_id'] != self.fleet.boot_id):
                return False
            retry = bool(error and retryable and row['attempts'] < row['max_attempts'])
            status = 'queued' if retry else 'failed' if error else 'succeeded'
            progress = json.loads(row['progress']) if row['progress'] else {}
            if progress:
                progress.update(stage='retrying' if retry else 'failed' if error else
                                'skipped' if (result or {}).get('skipped') else 'completed', speed=None, eta=None)
                if not error:
                    progress['percent'] = 100
            self._transition(db, row, status)
            db.execute(tasks.update().where(tasks.c.id == task_id).values(
                status=status, finished_at=None if retry else now(), result=json.dumps(result, ensure_ascii=False) if result else None,
                error=error, progress=json.dumps(progress, ensure_ascii=False) if progress else None,
                available_at=(self.fleet.clock(db) if self.fleet else time.time()) + min(300, 5 * 2 ** max(0, row['attempts'] - 1)) if retry else 0,
                lease_until=None, worker_id=None))
            outcome = ('retrying' if retry else 'failed' if error else 'test_succeeded' if (result or {}).get('test_mode')
                       else 'skipped' if (result or {}).get('skipped') else 'succeeded')
            self.statistics.transition(db, row, outcome, error,
                fallback_used=bool(progress.get('fallback_used') or (result or {}).get('fallback_used')))
            if not retry and row['action'] == 'download':
                self.lists.outcome(db, row, outcome)
                if self.fleet:
                    self._counter(db, 'instance:' + self.fleet.id, outcome, 1)
            self.statistics.settle(db)
            if not retry:
                generation = progress.get('rotation_id')
                if generation and not (result or {}).get('skipped'):
                    field = 'failed' if error else 'succeeded'
                    self.db.ensure(db, rotations, {'id': generation, 'succeeded': 0, 'failed': 0})
                    db.execute(rotations.update().where(rotations.c.id == generation).values({field: rotations.c[field] + 1}))
                if row['source_key'] and not row['source_key'].startswith('job:') and (error or (result or {}).get('test_mode') or row['source_key'].startswith('test:')):
                    db.execute(reservations.delete().where(reservations.c.key == row['source_key'], reservations.c.task_id == task_id))
            return True

    def recover(self, *, expired_only=False):
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            statement = select(tasks).where(tasks.c.status == 'running')
            if expired_only:
                statement = statement.where(or_(tasks.c.lease_until < (self.fleet.clock(db) if self.fleet else time.time()), tasks.c.lease_until.is_(None)))
            rows = db.execute(self._locked(statement.limit(1000), skip=True)).mappings().all()
            for row in rows:
                retry = expired_only and row['action'] in ('download', 'import', 'index') and row['attempts'] < row['max_attempts']
                status = 'queued' if retry else 'interrupted'
                self._transition(db, row, status)
                db.execute(tasks.update().where(tasks.c.id == row['id']).values(status=status, lease_until=None, worker_id=None,
                    available_at=(self.fleet.clock(db) if self.fleet else time.time()) + 2 if retry else 0, finished_at=None if retry else now(),
                    error='执行进程已退出，等待恢复。' if retry else '执行进程已退出。'))
                self.statistics.transition(db, row, 'retrying' if retry else 'failed', '执行进程已退出。')
                if not retry and not (row['source_key'] or '').startswith('job:'):
                    db.execute(reservations.delete().where(reservations.c.task_id == row['id']))
            self.statistics.settle(db)
            return len(rows)

    def worker_status(self, worker_id, slot, state, task_id=None, message=''):
        with self.connect() as db:
            if self.fleet:
                slot = hashlib.sha256(self.fleet.id.encode()).hexdigest()[:16] + ':' + str(slot)
            values = dict(id=worker_id, slot=slot, pid=os.getpid(), heartbeat=time.time(), state=state, task_id=task_id, message=message)
            db.execute(self.db.upsert(workers, values, ['id']))

    def workers(self):
        with self.connect() as db:
            latest = {}
            statement = select(workers).where(workers.c.heartbeat > time.time() - 90)
            if self.fleet and self.fleet.can_download:
                prefix = hashlib.sha256(self.fleet.id.encode()).hexdigest()[:16] + ':'
                statement = statement.where(workers.c.slot.startswith(prefix, autoescape=True))
            for row in db.execute(statement.order_by(workers.c.heartbeat.desc()).limit(500)).mappings():
                latest.setdefault(row['slot'], dict(row))
            return list(latest.values())

    def set_runtime(self, config):
        from webapp.disk_budget import DISK_FIELDS
        keys = ('worker_count', 'yt5s_browser_count', 'min_free_gb', 'max_log_mb', 'log_retention_days', 'youtube_cookie', 'storage_mode')
        keys = (*keys, *DISK_FIELDS)
        value = json.dumps({key: config[key] for key in keys if key in config})
        with self.connect() as db:
            db.execute(self.db.upsert(settings, dict(key='runtime', value=value), ['key']))

    def runtime(self):
        if self.fleet:
            return self.fleet.local_settings()['config']
        with self.connect() as db:
            value = db.execute(select(settings.c.value).where(settings.c.key == 'runtime')).scalar_one_or_none()
            return json.loads(value) if value else {}

    def archive_has(self, key, job_id=None):
        if job_id:
            key = 'job:' + job_id + ':' + key
        with self.connect() as db:
            return db.execute(select(archive.c.key).where(archive.c.key == key)).first() is not None

    def archive_add(self, key, path=None, job_id=None):
        if job_id:
            key = 'job:' + job_id + ':' + key
        with self.connect() as db:
            self.db.ensure(db, archive, {'key': key, 'created_at': now(), 'path': path})

    def import_archive(self, base):
        path = Path(base) / 'videos.txt'
        if not path.is_file():
            return
        source = (self.fleet.id + '\0' if self.fleet else '') + str(path.resolve())
        key = 'archive:' + hashlib.sha256(source.encode()).hexdigest()
        with self.connect() as db:
            offset = db.execute(select(settings.c.value).where(settings.c.key == key)).scalar_one_or_none()
        offset = int(offset or 0)
        if offset > path.stat().st_size:
            offset = 0
        with path.open('rb') as stream:
            stream.seek(offset)
            while True:
                lines = []
                for _ in range(1000):
                    line = stream.readline(16385)
                    if not line:
                        break
                    if len(line) < 16385:
                        value = line.decode('utf-8', errors='replace').strip()
                        if value:
                            lines.append({'key': value, 'created_at': now()})
                if not lines:
                    break
                with self.connect() as db:
                    db.execute(self.db.ignore(archive, lines))
                    db.execute(self.db.upsert(settings, dict(key=key, value=str(stream.tell())), ['key']))

    def log_path(self, task_id):
        if len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
            raise ValueError('无效的任务 ID')
        legacy = self.directory / f'{task_id}.log'
        if legacy.is_file():
            return legacy
        path = self.directory / 'logs' / task_id[:2] / task_id[2:4] / f'{task_id}.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def logs(self, task_id, offset=0, size=65536):
        path = self.log_path(task_id)
        if not path.exists():
            return {'text': '', 'offset': 0}
        try:
            base = int(path.with_suffix('.offset').read_text())
        except (OSError, ValueError):
            base = 0
        with path.open('rb') as stream:
            stream.seek(min(max(offset - base, 0), path.stat().st_size))
            chunk = stream.read(size)
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            text = decoder.decode(chunk, final=False)
            pending, _ = decoder.getstate()
            return {'text': text, 'offset': base + stream.tell() - len(pending), 'pruned': offset < base}

