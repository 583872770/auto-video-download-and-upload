"""Shared SQLAlchemy schema; PostgreSQL in production, SQLite for portable tests."""
import json
import os
from pathlib import Path
import threading

from sqlalchemy import (BigInteger, Column, Float, Index, Integer, MetaData, String,
                        Table, Text, create_engine, event, inspect, select)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
metadata = MetaData()
tasks = Table('tasks', metadata,
    Column('id', String(32), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('action', String(32), nullable=False), Column('status', String(24), nullable=False),
    Column('created_at', Text, nullable=False), Column('started_at', Text), Column('finished_at', Text),
    Column('payload', Text, nullable=False), Column('result', Text), Column('error', Text), Column('progress', Text),
    Column('batch_id', String(32)), Column('video_id', String(256)), Column('source_key', String(128)),
    Column('url', Text), Column('title', Text, nullable=False, server_default=''),
    Column('worker_id', String(128)), Column('lease_until', Float),
    Column('attempts', Integer, nullable=False, server_default='0'),
    Column('max_attempts', Integer, nullable=False, server_default='3'),
    Column('logs_pruned', Integer, nullable=False, server_default='0'),
    Column('available_at', Float, nullable=False, server_default='0'))
Index('ix_tasks_queue', tasks.c.status, tasks.c.available_at, tasks.c.seq)
Index('ix_tasks_action_seq', tasks.c.action, tasks.c.seq)
Index('ix_tasks_status_seq', tasks.c.status, tasks.c.seq)
_finished = tasks.c.status.in_(('succeeded', 'failed', 'interrupted'))
Index('ix_tasks_finished_seq', tasks.c.seq, postgresql_where=_finished, sqlite_where=_finished)
Index('ix_tasks_batch_seq', tasks.c.batch_id, tasks.c.seq)
Index('ix_tasks_video', tasks.c.video_id)
Index('ix_tasks_video_prefix', tasks.c.video_id, postgresql_ops={'video_id': 'varchar_pattern_ops'})
Index('ix_tasks_id_prefix', tasks.c.id, postgresql_ops={'id': 'varchar_pattern_ops'})
Index('ix_tasks_title_prefix', tasks.c.title, postgresql_ops={'title': 'text_pattern_ops'})
Index('ix_tasks_lease', tasks.c.status, tasks.c.lease_until)
Index('ix_tasks_seq', tasks.c.seq, unique=True)
Index('ix_tasks_log_retention', tasks.c.logs_pruned, tasks.c.finished_at)
snapshots = Table('task_snapshots', metadata, Column('id', String(64), primary_key=True), Column('payload', Text, nullable=False))
counters = Table('task_counters', metadata, Column('scope', String(80), primary_key=True),
                 Column('status', String(24), primary_key=True), Column('value', BigInteger, nullable=False))
rotations = Table('rotation_counters', metadata, Column('id', String(128), primary_key=True),
                  Column('succeeded', BigInteger, nullable=False, server_default='0'),
                  Column('failed', BigInteger, nullable=False, server_default='0'))
sequences = Table('sequence_counters', metadata, Column('key', String(32), primary_key=True),
                  Column('value', BigInteger, nullable=False))
settings = Table('schema_settings', metadata, Column('key', String(128), primary_key=True), Column('value', Text))
batches = Table('batches', metadata, Column('id', String(32), primary_key=True),
                Column('seq', BigInteger, nullable=False, unique=True), Column('created_at', Text, nullable=False),
                Column('name', Text, nullable=False, server_default=''))
reservations = Table('download_reservations', metadata, Column('key', String(128), primary_key=True),
                     Column('task_id', String(32), nullable=False))
archive = Table('download_archive', metadata, Column('key', String(512), primary_key=True),
                Column('created_at', Text, nullable=False), Column('path', Text))
workers = Table('workers', metadata, Column('id', String(128), primary_key=True), Column('slot', String(64)),
                Column('pid', Integer), Column('heartbeat', Float), Column('task_id', String(32)),
                Column('state', String(24)), Column('message', Text))
imports = Table('imports', metadata, Column('id', String(32), primary_key=True),
                Column('path', Text, nullable=False), Column('name', Text), Column('kind', String(8)),
                Column('offset', BigInteger, nullable=False, server_default='0'),
                Column('column_index', Integer, nullable=False, server_default='0'),
                Column('processed', BigInteger, nullable=False, server_default='0'),
                Column('added', BigInteger, nullable=False, server_default='0'),
                Column('duplicates', BigInteger, nullable=False, server_default='0'),
                Column('invalid', BigInteger, nullable=False, server_default='0'),
                Column('samples', Text, nullable=False, server_default='[]'),
                Column('finished', Integer, nullable=False, server_default='0'))
media = Table('media_catalog', metadata, Column('id', String(64), primary_key=True),
              Column('root', Text, nullable=False), Column('name', Text, nullable=False),
              Column('logical_key', Text, nullable=False), Column('title', Text, nullable=False),
              Column('search_title', Text, nullable=False), Column('video_id', String(256)),
              Column('size', BigInteger, nullable=False), Column('modified', Float, nullable=False),
              Column('cover', Text), Column('playable', Integer, nullable=False),
              Column('prepared', Integer, nullable=False), Column('status', String(24)),
              Column('scan_id', String(32)), Column('indexed_at', Float, nullable=False, server_default='0'))
Index('ix_media_root_recent', media.c.root, media.c.modified, media.c.id)
Index('ix_media_root_video', media.c.root, media.c.video_id)
Index('ix_media_root_scan', media.c.root, media.c.scan_id)
Index('ix_media_root_title', media.c.root, media.c.search_title,
      postgresql_ops={'search_title': 'text_pattern_ops'})
roots = Table('media_roots', metadata, Column('root', Text, primary_key=True),
              Column('count', BigInteger, nullable=False, server_default='0'),
              Column('bytes', BigInteger, nullable=False, server_default='0'),
              Column('last_indexed', Text), Column('scan_id', String(32)),
              Column('scanned', BigInteger, nullable=False, server_default='0'))

_lock = threading.Lock()
_engines = {}


class Database:
    def __init__(self, directory, url=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        config = self.directory / 'database.json'
        if url is None and self.directory == ROOT / '.cache' / 'web':
            url = os.environ.get('VIDEO_DATABASE_URL')
        if url is None and config.is_file():
            url = json.loads(config.read_text(encoding='utf-8'))['url']
        self.url = url or 'sqlite:///' + (self.directory / 'tasks.sqlite3').as_posix()
        if self.url.startswith('postgresql://'):
            self.url = self.url.replace('postgresql://', 'postgresql+psycopg://', 1)
        if not self.url.startswith(('sqlite:///', 'postgresql+psycopg://')):
            raise ValueError('数据库仅支持 PostgreSQL 或 SQLite。')
        with _lock:
            if self.url not in _engines:
                engine = create_engine(self.url, poolclass=NullPool, hide_parameters=True,
                                       connect_args={'timeout': 30} if self.url.startswith('sqlite:') else {})
                if engine.dialect.name == 'sqlite':
                    @event.listens_for(engine, 'connect')
                    def configure(dbapi, _):
                        dbapi.execute('PRAGMA busy_timeout=30000')
                        dbapi.execute('PRAGMA journal_mode=WAL')
                self.engine = engine
                self.initialize()
                _engines[self.url] = engine
            self.engine = _engines[self.url]

    @property
    def postgres(self):
        return self.engine.dialect.name == 'postgresql'

    def insert(self, table):
        return (pg_insert if self.postgres else sqlite_insert)(table)

    def ensure(self, db, table, values):
        return db.execute(self.insert(table).values(**values).on_conflict_do_nothing())

    def sequence(self, db, key, count=1):
        self.ensure(db, sequences, {'key': key, 'value': 0})
        end = db.execute(sequences.update().where(sequences.c.key == key)
                         .values(value=sequences.c.value + count).returning(sequences.c.value)).scalar_one()
        return end - count + 1

    def initialize(self):
        old = False
        if self.engine.dialect.name == 'sqlite' and inspect(self.engine).has_table('tasks'):
            existing = {col['name'] for col in inspect(self.engine).get_columns('tasks')}
            additions = {'seq': 'BIGINT', 'progress': 'TEXT', 'batch_id': 'VARCHAR(32)',
                         'video_id': 'VARCHAR(256)', 'source_key': 'VARCHAR(128)', 'url': 'TEXT',
                         'title': "TEXT NOT NULL DEFAULT ''", 'worker_id': 'VARCHAR(128)', 'lease_until': 'FLOAT',
                         'attempts': 'INTEGER NOT NULL DEFAULT 0', 'max_attempts': 'INTEGER NOT NULL DEFAULT 3',
                         'logs_pruned': 'INTEGER NOT NULL DEFAULT 0',
                         'available_at': 'FLOAT NOT NULL DEFAULT 0'}
            with self.engine.begin() as db:
                for key, sqltype in additions.items():
                    if key not in existing:
                        db.exec_driver_sql(f'ALTER TABLE tasks ADD COLUMN {key} {sqltype}')
                if 'seq' not in existing:
                    db.exec_driver_sql('UPDATE tasks SET seq = rowid')
                    old = True
        metadata.create_all(self.engine)
        for index in tasks.indexes:
            index.create(self.engine, checkfirst=True)
        with self.engine.begin() as db:
            self.ensure(db, settings, {'key': 'downloads_paused', 'value': 'false'})
        if old:
            # One-time upgrade only; regular requests never enumerate task history.
            with self.engine.begin() as db:
                maximum = db.exec_driver_sql('SELECT COALESCE(MAX(seq), 0) FROM tasks').scalar_one()
                self.ensure(db, sequences, {'key': 'task', 'value': maximum})
                for row in db.exec_driver_sql('SELECT action, status, COUNT(*) AS n FROM tasks GROUP BY action, status').mappings():
                    self.ensure(db, counters, {'scope': 'action:' + row['action'], 'status': row['status'], 'value': row['n']})
                from webapp.store import source_identity
                for row in db.execute(select(tasks)).mappings().yield_per(500):
                    payload = json.loads(row['payload'])
                    urls = payload.get('urls') or []
                    progress = json.loads(row['progress']) if row['progress'] else {}
                    result = json.loads(row['result']) if row['result'] else {}
                    video_id, source_key = source_identity(urls[0] if urls else None)
                    batch_id = payload.get('batch_id')
                    db.execute(tasks.update().where(tasks.c.id == row['id']).values(
                        batch_id=batch_id, video_id=video_id, source_key=source_key, url=urls[0] if urls else None,
                        title=str(progress.get('title') or '').casefold()))
                    if batch_id:
                        self.ensure(db, batches, {'id': batch_id, 'seq': self.sequence(db, 'batch'),
                                                 'created_at': row['created_at'], 'name': ''})
                        counter = self.insert(counters).values(scope='batch:' + batch_id, status=row['status'], value=1)
                        db.execute(counter.on_conflict_do_update(index_elements=['scope', 'status'],
                                                                set_={'value': counters.c.value + 1}))
                    if row['action'] == 'download' and row['status'] not in ('failed', 'interrupted'):
                        self.ensure(db, reservations, {'key': source_key, 'task_id': row['id']})
                    if progress.get('rotation_id') and row['status'] in ('succeeded', 'failed') and not result.get('skipped'):
                        identity = progress['rotation_id']
                        field = 'failed' if row['status'] == 'failed' else 'succeeded'
                        self.ensure(db, rotations, {'id': identity, 'succeeded': 0, 'failed': 0})
                        db.execute(rotations.update().where(rotations.c.id == identity).values({field: rotations.c[field] + 1}))
