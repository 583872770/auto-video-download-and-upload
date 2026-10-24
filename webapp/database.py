"""Shared schema and transactional SQL for MySQL, PostgreSQL and SQLite."""
import json
import os
from pathlib import Path
import threading

from sqlalchemy import (BigInteger, Column, Float as SAFloat, Index, Integer, MetaData, String,
                        Table, Text as SAText, create_engine, event, inspect, select, text)
from sqlalchemy.dialects.mysql import DOUBLE, LONGTEXT, insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
# MySQL FLOAT loses seconds at Unix-epoch magnitudes; leases and media cursors
# require double precision. JSON snapshots can exceed MySQL TEXT's 64 KiB.
Float = SAFloat().with_variant(DOUBLE(asdecimal=False), 'mysql')
Text = SAText().with_variant(LONGTEXT(), 'mysql')
RootPath = SAText().with_variant(String(512), 'mysql')
metadata = MetaData()
tasks = Table('tasks', metadata,
    Column('id', String(32), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('action', String(32), nullable=False), Column('status', String(24), nullable=False),
    Column('created_at', Text, nullable=False), Column('started_at', Text), Column('finished_at', Text),
    Column('payload', Text, nullable=False), Column('result', Text), Column('error', Text), Column('progress', Text),
    Column('batch_id', String(32)), Column('video_id', String(256)), Column('source_key', String(128)),
    Column('url', Text), Column('title', Text, nullable=False, default='', server_default=text("('')")),
    Column('worker_id', String(128)), Column('lease_until', Float),
    Column('attempts', Integer, nullable=False, server_default='0'),
    Column('max_attempts', Integer, nullable=False, server_default='3'),
    Column('logs_pruned', Integer, nullable=False, server_default='0'),
    Column('available_at', Float, nullable=False, server_default='0'))
Index('ix_tasks_queue', tasks.c.status, tasks.c.available_at, tasks.c.seq)
Index('ix_tasks_action_seq', tasks.c.action, tasks.c.seq)
Index('ix_tasks_status_seq', tasks.c.status, tasks.c.seq)
_finished = tasks.c.status.in_(('succeeded', 'failed', 'interrupted'))
Index('ix_tasks_finished_seq', tasks.c.seq, postgresql_where=_finished, sqlite_where=_finished).ddl_if(dialect=('postgresql', 'sqlite'))
Index('ix_tasks_finished_time', tasks.c.finished_at, tasks.c.seq,
      sqlite_where=_finished).ddl_if(dialect='sqlite')
Index('ix_tasks_finished_time_desc', tasks.c.finished_at.desc().nulls_last(), tasks.c.seq.desc(),
      postgresql_where=_finished).ddl_if(dialect='postgresql')
Index('ix_tasks_status_finished_time', tasks.c.status, tasks.c.finished_at, tasks.c.seq,
      mysql_length={'finished_at': 40}).ddl_if(dialect='mysql')
Index('ix_tasks_batch_seq', tasks.c.batch_id, tasks.c.seq)
Index('ix_tasks_video', tasks.c.video_id)
Index('ix_tasks_video_prefix', tasks.c.video_id, postgresql_ops={'video_id': 'varchar_pattern_ops'}).ddl_if(dialect=('postgresql', 'sqlite'))
Index('ix_tasks_id_prefix', tasks.c.id, postgresql_ops={'id': 'varchar_pattern_ops'}).ddl_if(dialect=('postgresql', 'sqlite'))
Index('ix_tasks_title_prefix', tasks.c.title, postgresql_ops={'title': 'text_pattern_ops'}, mysql_length=191)
Index('ix_tasks_lease', tasks.c.status, tasks.c.lease_until)
Index('ix_tasks_seq', tasks.c.seq, unique=True)
Index('ix_tasks_log_retention', tasks.c.logs_pruned, tasks.c.finished_at, mysql_length={'finished_at': 40})
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
                Column('name', Text, nullable=False, default='', server_default=text("('')")))
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
                Column('samples', Text, nullable=False, default='[]', server_default=text("('[]')")),
                Column('finished', Integer, nullable=False, server_default='0'))
media = Table('media_catalog', metadata, Column('id', String(64), primary_key=True),
              Column('root', RootPath, nullable=False), Column('name', Text, nullable=False),
              Column('logical_key', Text, nullable=False), Column('title', Text, nullable=False),
              Column('search_title', Text, nullable=False), Column('video_id', String(256)),
              Column('size', BigInteger, nullable=False), Column('modified', Float, nullable=False),
              Column('cover', Text), Column('playable', Integer, nullable=False),
              Column('prepared', Integer, nullable=False), Column('status', String(24)),
              Column('scan_id', String(32)), Column('indexed_at', Float, nullable=False, server_default='0'))
Index('ix_media_root_recent', media.c.root, media.c.modified, media.c.id, mysql_length={'root': 191})
Index('ix_media_root_video', media.c.root, media.c.video_id, mysql_length={'root': 191})
Index('ix_media_root_scan', media.c.root, media.c.scan_id, mysql_length={'root': 191})
Index('ix_media_root_title', media.c.root, media.c.search_title,
      postgresql_ops={'search_title': 'text_pattern_ops'}, mysql_length={'root': 191, 'search_title': 191})
roots = Table('media_roots', metadata, Column('root', RootPath, primary_key=True),
              Column('count', BigInteger, nullable=False, server_default='0'),
              Column('bytes', BigInteger, nullable=False, server_default='0'),
              Column('last_indexed', Text), Column('scan_id', String(32)),
              Column('scanned', BigInteger, nullable=False, server_default='0'))

for table in metadata.tables.values():
    # YouTube IDs and file paths are case sensitive. InnoDB supplies row locks;
    # the NO PAD binary collation also preserves distinct trailing characters.
    table.dialect_options['mysql'].update(
        engine='InnoDB', charset='utf8mb4', collate='utf8mb4_0900_bin')

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
        if self.url.startswith('mysql://'):
            self.url = self.url.replace('mysql://', 'mysql+pymysql://', 1)
        if not self.url.startswith(('sqlite:///', 'postgresql+psycopg://', 'mysql+pymysql://')):
            raise ValueError('数据库仅支持 MySQL、PostgreSQL 或 SQLite。')
        with _lock:
            if self.url not in _engines:
                options = {'connect_args': {'timeout': 30}} if self.url.startswith('sqlite:') else {}
                if self.url.startswith('mysql:') or self.url.startswith('mysql+pymysql:'):
                    options.update(isolation_level='READ COMMITTED', connect_args={
                        'charset': 'utf8mb4', 'connect_timeout': 10, 'read_timeout': 60, 'write_timeout': 60})
                engine = create_engine(self.url, poolclass=NullPool, hide_parameters=True, **options)
                if engine.dialect.name == 'sqlite':
                    @event.listens_for(engine, 'connect')
                    def configure(dbapi, _):
                        dbapi.execute('PRAGMA busy_timeout=30000')
                        dbapi.execute('PRAGMA journal_mode=WAL')
                self.engine = engine
                try:
                    if self.mysql:
                        with engine.connect():
                            if engine.dialect.is_mariadb or engine.dialect.server_version_info < (5, 7, 9):
                                raise ValueError('请使用 MySQL 5.7.9 或更新版本，推荐 MySQL 8.4。')
                    self.initialize()
                except Exception:
                    engine.dispose()
                    raise
                _engines[self.url] = engine
            self.engine = _engines[self.url]

    @property
    def postgres(self):
        return self.engine.dialect.name == 'postgresql'

    @property
    def mysql(self):
        return self.engine.dialect.name == 'mysql'

    @property
    def sqlite(self):
        return self.engine.dialect.name == 'sqlite'

    @property
    def supports_skip_locked(self):
        return self.postgres or (self.mysql and self.engine.dialect.server_version_info >= (8, 0, 1))

    def schema(self):
        if not self.mysql or self.engine.dialect.server_version_info >= (8, 0, 21):
            return metadata
        # Keep shared metadata immutable: this process may open 5.7, 8.x and
        # SQLite databases together (including during a database migration).
        schema = MetaData()
        for original in metadata.tables.values():
            table = original.to_metadata(schema)
            table.dialect_options['mysql'].update(collate='utf8mb4_bin', row_format='DYNAMIC')
            for column in table.c:
                if isinstance(column.type.dialect_impl(self.engine.dialect), LONGTEXT):
                    # 5.7 cannot give TEXT a server default. Python defaults on
                    # the source columns supply values for application inserts.
                    column.server_default = None
            original_indexes = {index.name: index for index in original.indexes}
            for index in table.indexes:
                # SQLAlchemy to_metadata does not copy conditional index DDL.
                index._ddl_if = original_indexes[index.name]._ddl_if
        return schema

    def insert(self, table):
        return (mysql_insert if self.mysql else pg_insert if self.postgres else sqlite_insert)(table)

    def ignore(self, table, values):
        statement = self.insert(table).values(values)
        if self.mysql:
            # Do not use INSERT IGNORE: it also hides invalid/oversized data.
            column = next(iter(table.primary_key))
            return statement.on_duplicate_key_update({column.name: column})
        return statement.on_conflict_do_nothing()

    def upsert(self, table, values, keys, updates=None):
        statement = self.insert(table).values(values)
        if updates is None:
            incoming = statement.inserted if self.mysql else statement.excluded
            updates = {column.name: incoming[column.name] for column in table.c if column.name not in keys}
        if self.mysql:
            return statement.on_duplicate_key_update(updates)
        return statement.on_conflict_do_update(index_elements=keys, set_=updates)

    def ensure(self, db, table, values):
        return db.execute(self.ignore(table, values))

    def reserve_downloads(self, db, rows):
        statement = self.ignore(reservations, rows)
        if not self.mysql:
            return set(db.execute(statement.returning(reservations.c.task_id)).scalars())
        db.execute(statement)
        # MySQL has no INSERT RETURNING, and FOUND_ROWS makes a duplicate no-op
        # report rowcount=1. Compare UUID ownership inside the same transaction.
        return set(db.execute(select(reservations.c.task_id).where(
            reservations.c.key.in_([row['key'] for row in rows]),
            reservations.c.task_id.in_([row['task_id'] for row in rows]))).scalars())

    def sequence(self, db, key, count=1):
        self.ensure(db, sequences, {'key': key, 'value': 0})
        statement = sequences.update().where(sequences.c.key == key).values(value=sequences.c.value + count)
        if self.mysql:
            db.execute(statement)
            end = db.execute(select(sequences.c.value).where(sequences.c.key == key)).scalar_one()
        else:
            end = db.execute(statement.returning(sequences.c.value)).scalar_one()
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
        schema = self.schema()
        schema.create_all(self.engine)
        for index in schema.tables['tasks'].indexes:
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
                        db.execute(self.upsert(counters, dict(scope='batch:' + batch_id, status=row['status'], value=1),
                                               ['scope', 'status'], {'value': counters.c.value + 1}))
                    if row['action'] == 'download' and row['status'] not in ('failed', 'interrupted'):
                        self.ensure(db, reservations, {'key': source_key, 'task_id': row['id']})
                    if progress.get('rotation_id') and row['status'] in ('succeeded', 'failed') and not result.get('skipped'):
                        identity = progress['rotation_id']
                        field = 'failed' if row['status'] == 'failed' else 'succeeded'
                        self.ensure(db, rotations, {'id': identity, 'succeeded': 0, 'failed': 0})
                        db.execute(rotations.update().where(rotations.c.id == identity).values({field: rotations.c[field] + 1}))
