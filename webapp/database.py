"""Shared schema and transactional SQL for MySQL (with SQLite test support)."""
import hashlib
import json
import os
from pathlib import Path
import threading
from datetime import datetime

from sqlalchemy import (BigInteger, Column, Float as SAFloat, Index, Integer, MetaData, String,
                        Table, Text as SAText, create_engine, event, func, inspect, select, text)
from sqlalchemy.dialects.mysql import DOUBLE, LONGTEXT, insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]


def schema_token(url):
    """Only a supervisor using these exact schema sources can skip child DDL."""
    digest = hashlib.sha256(url.encode('utf-8'))
    for path in (Path(__file__), Path(__file__).with_name('migrations.py')):
        digest.update(path.read_bytes())
    return digest.hexdigest()
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
    Column('instance_id', String(64)), Column('target_instance_id', String(64)), Column('policy_version', BigInteger),
    Column('attempts', Integer, nullable=False, server_default='0'),
    Column('max_attempts', Integer, nullable=False, server_default='3'),
    Column('logs_pruned', Integer, nullable=False, server_default='0'),
    Column('available_at', Float, nullable=False, server_default='0'))
Index('ix_tasks_queue', tasks.c.status, tasks.c.available_at, tasks.c.seq)
Index('ix_tasks_dispatch', tasks.c.action, tasks.c.status, tasks.c.available_at)
Index('ix_tasks_action_seq', tasks.c.action, tasks.c.seq)
Index('ix_tasks_status_seq', tasks.c.status, tasks.c.seq)
Index('ix_tasks_finished_time', tasks.c.finished_at, tasks.c.seq,
      mysql_length={'finished_at': 40})
Index('ix_tasks_status_finished_time', tasks.c.status, tasks.c.finished_at, tasks.c.seq,
      mysql_length={'finished_at': 40})
Index('ix_tasks_batch_seq', tasks.c.batch_id, tasks.c.seq)
Index('ix_tasks_video', tasks.c.video_id)
Index('ix_tasks_url_prefix', tasks.c.batch_id, tasks.c.url, mysql_length={'url': 128})
Index('ix_tasks_title_prefix', tasks.c.title, mysql_length=191)
Index('ix_tasks_lease', tasks.c.status, tasks.c.lease_until)
Index('ix_tasks_instance_status', tasks.c.instance_id, tasks.c.status, tasks.c.action)
Index('ix_tasks_target_queue', tasks.c.target_instance_id, tasks.c.status, tasks.c.available_at)
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
download_resources = Table('download_resources', metadata,
    Column('key', String(64), primary_key=True), Column('state', Text, nullable=False))
download_permits = Table('download_permits', metadata,
    Column('token', String(32), primary_key=True), Column('resource', String(64), primary_key=True),
    Column('task_id', String(32), nullable=False), Column('owner', String(128), nullable=False),
    Column('units', Integer, nullable=False))
Index('ix_download_permits_resource', download_permits.c.resource)
batches = Table('batches', metadata, Column('id', String(32), primary_key=True),
                Column('seq', BigInteger, nullable=False, unique=True), Column('created_at', Text, nullable=False),
                Column('name', Text, nullable=False, default='', server_default=text("('')")))
download_lists = Table('download_lists', metadata,
    Column('id', String(32), primary_key=True), Column('customer', String(200), nullable=False),
    Column('name', String(200), nullable=False), Column('price', String(32), nullable=False),
    Column('unit', String(8), nullable=False), Column('released', Integer, nullable=False, server_default='0'),
    Column('tracking_since', String(40), nullable=False),
    *(Column(name, BigInteger, nullable=False, server_default='0') for name in
      ('file_count', 'file_bytes', 'test_file_count', 'test_file_bytes', 'test_succeeded', 'skipped')))
Index('ix_download_lists_released', download_lists.c.released, download_lists.c.id)
reservations = Table('download_reservations', metadata, Column('key', String(128), primary_key=True),
                     Column('task_id', String(32), nullable=False))
archive = Table('download_archive', metadata, Column('key', String(512), primary_key=True),
                Column('created_at', Text, nullable=False), Column('path', Text))
job_profiles = Table('job_profiles', metadata,
    Column('id', String(32), primary_key=True), Column('note', Text, nullable=False),
    Column('policy', Text, nullable=False), Column('revision', Integer, nullable=False),
    Column('archived', Integer, nullable=False), Column('deleted', Integer, nullable=False),
    Column('price_set', Integer, nullable=False), Column('updated_at', String(40), nullable=False),
    Column('edit_version', BigInteger, nullable=False, server_default='1'),
    Column('policy_version', BigInteger, nullable=False, server_default='1'))
Index('ix_job_profiles_lifecycle', job_profiles.c.deleted, job_profiles.c.archived, job_profiles.c.id)
schema_migrations = Table('schema_migrations', metadata,
    Column('version', String(80), primary_key=True), Column('applied_at', String(40), nullable=False))
console_users = Table('console_users', metadata,
    Column('username', String(64), primary_key=True), Column('password_hash', String(512), nullable=False),
    Column('role', String(16), nullable=False), Column('enabled', Integer, nullable=False, server_default='1'),
    Column('revision', BigInteger, nullable=False, server_default='1'))
job_imports = Table('job_imports', metadata, Column('id', String(32), primary_key=True),
    Column('job_id', String(32), nullable=False), Column('revision', Integer, nullable=False), Column('task_id', String(32)))
Index('ix_job_imports_job', job_imports.c.job_id)
job_removed = Table('job_removed', metadata, Column('task_id', String(32), primary_key=True),
    Column('previous_status', String(24), nullable=False))
job_events = Table('job_events', metadata, Column('id', String(32), primary_key=True),
    Column('seq', BigInteger, nullable=False, unique=True), Column('job_id', String(32), nullable=False),
    Column('created_at', String(40), nullable=False), Column('message', Text, nullable=False))
Index('ix_job_events_page', job_events.c.job_id, job_events.c.seq)
workers = Table('workers', metadata, Column('id', String(128), primary_key=True), Column('slot', String(64)),
                Column('pid', Integer), Column('heartbeat', Float), Column('task_id', String(32)),
                Column('state', String(24)), Column('message', Text))
instances = Table('download_instances', metadata,
    Column('id', String(64), primary_key=True), Column('name', String(100), nullable=False),
    Column('role', String(16), nullable=False), Column('host', String(200), nullable=False),
    Column('pid', Integer, nullable=False), Column('boot_id', String(32), nullable=False),
    Column('heartbeat', Float, nullable=False), Column('url', Text, nullable=False), Column('token', Text, nullable=False),
    Column('config', Text, nullable=False), Column('limits', Text, nullable=False),
    Column('paused', Integer, nullable=False, server_default='0'),
    Column('stop_requested', Integer, nullable=False, server_default='0'),
    Column('error', Text, nullable=False))
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
      mysql_length={'root': 191, 'search_title': 191})
roots = Table('media_roots', metadata, Column('root', RootPath, primary_key=True),
              Column('count', BigInteger, nullable=False, server_default='0'),
              Column('bytes', BigInteger, nullable=False, server_default='0'),
              Column('last_indexed', Text), Column('scan_id', String(32)),
              Column('scanned', BigInteger, nullable=False, server_default='0'))

download_sessions = Table('download_sessions', metadata,
    Column('id', String(32), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('started_at', Text, nullable=False), Column('ended_at', Text), Column('stop_requested_at', Text),
    Column('status', String(24), nullable=False), Column('label', Text, nullable=False),
    Column('price', String(32), nullable=False), Column('unit', String(8), nullable=False),
    *(Column(name, BigInteger, nullable=False, server_default='0') for name in
      ('attempted', 'running', 'succeeded', 'failed', 'skipped', 'test_succeeded', 'file_count', 'file_bytes')))
session_tasks = Table('download_session_tasks', metadata,
    Column('session_id', String(32), primary_key=True), Column('task_id', String(32), primary_key=True),
    Column('seq', BigInteger, nullable=False), Column('video_id', String(256)), Column('batch_id', String(32)),
    Column('status', String(24), nullable=False), Column('started_at', Text, nullable=False),
    Column('finished_at', Text), Column('error', Text))
Index('ix_session_tasks_recent', session_tasks.c.session_id, session_tasks.c.seq)
Index('ix_session_tasks_status', session_tasks.c.session_id, session_tasks.c.status, session_tasks.c.seq)
deliveries = Table('download_deliveries', metadata,
    Column('id', String(64), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('session_id', String(32), nullable=False), Column('task_id', String(32), nullable=False),
    Column('video_id', String(256)), Column('title', Text, nullable=False), Column('path', Text, nullable=False),
    Column('size', BigInteger, nullable=False), Column('created_at', Text, nullable=False))
Index('ix_deliveries_session', deliveries.c.session_id, deliveries.c.seq)
Index('ix_deliveries_task_seq', deliveries.c.task_id, deliveries.c.seq)

# Separate ledgers keep deleted test files out of delivery billing and avoid
# altering existing task/delivery tables during upgrades with large queues.
test_files = Table('download_test_files', metadata,
    Column('id', String(64), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('session_id', String(32), nullable=False), Column('task_id', String(32), nullable=False),
    Column('video_id', String(256)), Column('title', Text, nullable=False), Column('path', Text, nullable=False),
    Column('size', BigInteger, nullable=False), Column('created_at', Text, nullable=False))
Index('ix_test_files_session', test_files.c.session_id, test_files.c.seq)
Index('ix_test_files_task_seq', test_files.c.task_id, test_files.c.seq)
test_totals = Table('download_test_totals', metadata,
    Column('session_id', String(32), primary_key=True),
    Column('test_file_count', BigInteger, nullable=False, server_default='0'),
    Column('test_file_bytes', BigInteger, nullable=False, server_default='0'))

method_totals = Table('download_method_totals', metadata,
    Column('session_id', String(32), primary_key=True), Column('method', String(32), primary_key=True),
    *(Column(name, BigInteger, nullable=False, server_default='0') for name in
      ('attempted', 'running', 'succeeded', 'failed', 'skipped', 'test_succeeded', 'file_count', 'file_bytes',
       'test_file_count', 'test_file_bytes', 'fallback_tasks')))
method_tasks = Table('download_method_tasks', metadata,
    Column('session_id', String(32), primary_key=True), Column('task_id', String(32), primary_key=True),
    Column('method', String(32), nullable=False), Column('status', String(24), nullable=False),
    Column('fallback_used', Integer, nullable=False, server_default='0'))

# One row per provider/video/session keeps arbitrary fallback chains idempotent
# across retries without rewriting existing ledgers or large task queues.
provider_tasks = Table('download_provider_tasks', metadata,
    Column('session_id', String(32), primary_key=True), Column('task_id', String(32), primary_key=True),
    Column('method', String(32), primary_key=True), Column('status', String(24), nullable=False),
    Column('fallback_used', Integer, nullable=False, server_default='0'))

for table in metadata.tables.values():
    # YouTube IDs and file paths are case sensitive. InnoDB supplies row locks;
    # the NO PAD binary collation also preserves distinct trailing characters.
    table.dialect_options['mysql'].update(
        engine='InnoDB', charset='utf8mb4', collate='utf8mb4_0900_bin')

_lock = threading.Lock()
_engines = {}


class SchemaUpgradeInProgress(RuntimeError):
    """Another initializer or an older server-side DDL is still running."""


class Database:
    def __init__(self, directory, url=None, *, startup_report=None):
        self.startup_report = startup_report or (lambda message: None)
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        config = self.directory / 'database.json'
        if url is None and self.directory == ROOT / '.cache' / 'web':
            url = os.environ.get('VIDEO_DATABASE_URL')
        if url is None and config.is_file():
            url = json.loads(config.read_text(encoding='utf-8'))['url']
        self.url = url or 'sqlite:///' + (self.directory / 'tasks.sqlite3').as_posix()
        if self.url.startswith('mysql://'):
            self.url = self.url.replace('mysql://', 'mysql+pymysql://', 1)
        if not self.url.startswith(('sqlite:///', 'mysql+pymysql://')):
            raise ValueError('数据库仅支持 MySQL。请检查 VIDEO_DATABASE_URL 或 .cache/web/database.json 中的旧连接；本地启动可在项目 .env 中配置 MYSQL_HOST、MYSQL_DATABASE、MYSQL_USER、MYSQL_PASSWORD。')
        with _lock:
            if self.url not in _engines:
                options = {'connect_args': {'timeout': 30}} if self.url.startswith('sqlite:') else {}
                if self.url.startswith('mysql:') or self.url.startswith('mysql+pymysql:'):
                    options.update(isolation_level='READ COMMITTED', connect_args={
                        'charset': 'utf8mb4', 'connect_timeout': 10, 'read_timeout': 60, 'write_timeout': 60})
                if self.url.startswith('sqlite:'):
                    options['poolclass'] = NullPool
                else:
                    # Engines are process-local: bound both idle and busy connections.
                    # Dynamic pool sizing based on role (main vs worker) and total budget
                    from webapp.db_pool_calculator import calculate_pool_size
                    is_worker = os.environ.get('VIDEO_WORKER_PROCESS') == 'true'
                    pool_size, max_overflow = calculate_pool_size(is_worker=is_worker)
                    options.update(pool_size=pool_size, max_overflow=max_overflow,
                                   pool_timeout=15, pool_pre_ping=True, pool_recycle=300)
                engine = create_engine(self.url, hide_parameters=True, **options)
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
                    if not self.mysql or os.environ.get('VIDEO_WORKER_SCHEMA_READY') != schema_token(self.url):
                        self.initialize()
                except Exception:
                    engine.dispose()
                    raise
                _engines[self.url] = engine
            self.engine = _engines[self.url]

    @property
    def mysql(self):
        return self.engine.dialect.name == 'mysql'

    @property
    def sqlite(self):
        return self.engine.dialect.name == 'sqlite'

    @property
    def supports_skip_locked(self):
        return self.mysql and self.engine.dialect.server_version_info >= (8, 0, 1)

    def schema(self):
        if not self.mysql or self.engine.dialect.server_version_info >= (8, 0, 21):
            return metadata
        # Keep shared metadata immutable: this process may open MySQL 5.7, 8.x
        # and SQLite databases together during a migration or test run.
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
        return (mysql_insert if self.mysql else sqlite_insert)(table)

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
        if self.mysql:
            # LAST_INSERT_ID(expr) is connection-local, and the OK packet
            # exposes its value as lastrowid without a separate SELECT.
            statement = self.insert(sequences).values(key=key, value=func.last_insert_id(count))
            result = db.execute(statement.on_duplicate_key_update(
                value=func.last_insert_id(sequences.c.value + count)))
            return result.lastrowid - count + 1
        self.ensure(db, sequences, {'key': key, 'value': 0})
        statement = sequences.update().where(sequences.c.key == key).values(value=sequences.c.value + count)
        end = db.execute(statement.returning(sequences.c.value)).scalar_one()
        return end - count + 1

    def initialize(self):
        if not self.mysql:
            return self._initialize_schema(self.engine)
        # MySQL 5.7 may rebuild a large table for ADD COLUMN. Its DDL connection
        # needs a separate timeout, while normal download queries stay bounded.
        engine = create_engine(self.url, poolclass=NullPool, hide_parameters=True,
            isolation_level='READ COMMITTED', connect_args={
                'charset': 'utf8mb4', 'connect_timeout': 10, 'read_timeout': 1800, 'write_timeout': 60})
        lock_name = 'video_schema_' + hashlib.sha256(engine.url.database.encode('utf-8')).hexdigest()[:40]
        try:
            with engine.connect() as guard:
                self.startup_report('正在检查数据库升级状态…')
                acquired = guard.execute(text('SELECT GET_LOCK(:name, 10)'), {'name': lock_name}).scalar_one()
                if acquired != 1:
                    raise SchemaUpgradeInProgress('另一个实例正在升级数据库。')
                try:
                    # Old versions did not take our advisory lock. An ALTER can
                    # continue on MySQL after that client hit its read timeout.
                    pending = guard.execute(text("SELECT ID FROM information_schema.PROCESSLIST "
                        "WHERE DB=DATABASE() AND ID<>CONNECTION_ID() AND LOWER(INFO) REGEXP "
                        "'^[[:space:]]*(alter[[:space:]]+table|create[[:space:]]+(unique[[:space:]]+)?index)' "
                        "LIMIT 1")).first()
                    if pending:
                        raise SchemaUpgradeInProgress('MySQL 中仍有表结构升级正在执行或等待锁。')
                    self._initialize_schema(engine)
                finally:
                    try:
                        guard.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': lock_name})
                    except SQLAlchemyError:
                        # A disconnected guard releases the named lock itself.
                        pass
        finally:
            engine.dispose()

    def _initialize_schema(self, engine):
        old = False
        if inspect(engine).has_table('tasks'):
            existing_columns = {col['name'] for col in inspect(engine).get_columns('tasks')}
            additions = [f'ADD COLUMN {key} {sqltype}' for key, sqltype in
                         {'instance_id': 'VARCHAR(64)', 'target_instance_id': 'VARCHAR(64)',
                          'policy_version': 'BIGINT'}.items() if key not in existing_columns]
            with engine.begin() as db:
                if additions and self.mysql:
                    self.startup_report('正在升级任务表；MySQL 5.7 大表可能需要数分钟，请等待，不要重复启动…')
                    db.exec_driver_sql('ALTER TABLE tasks ' + ', '.join(additions))
                elif additions:
                    for addition in additions:
                        db.exec_driver_sql('ALTER TABLE tasks ' + addition)
        if engine.dialect.name == 'sqlite' and inspect(engine).has_table('tasks'):
            existing = {col['name'] for col in inspect(engine).get_columns('tasks')}
            additions = {'seq': 'BIGINT', 'progress': 'TEXT', 'batch_id': 'VARCHAR(32)',
                         'video_id': 'VARCHAR(256)', 'source_key': 'VARCHAR(128)', 'url': 'TEXT',
                         'title': "TEXT NOT NULL DEFAULT ''", 'worker_id': 'VARCHAR(128)', 'lease_until': 'FLOAT',
                         'attempts': 'INTEGER NOT NULL DEFAULT 0', 'max_attempts': 'INTEGER NOT NULL DEFAULT 3',
                         'logs_pruned': 'INTEGER NOT NULL DEFAULT 0',
                         'available_at': 'FLOAT NOT NULL DEFAULT 0'}
            with engine.begin() as db:
                for key, sqltype in additions.items():
                    if key not in existing:
                        db.exec_driver_sql(f'ALTER TABLE tasks ADD COLUMN {key} {sqltype}')
                if 'seq' not in existing:
                    db.exec_driver_sql('UPDATE tasks SET seq = rowid')
                    old = True
        schema = self.schema()
        self.startup_report('正在检查数据库表和索引；首次升级期间请等待…')
        schema.create_all(engine)
        from webapp.migrations import upgrade
        upgrade(engine)
        for index in schema.tables['tasks'].indexes:
            index.create(engine, checkfirst=True)
        with engine.begin() as db:
            self.ensure(db, settings, {'key': 'downloads_paused', 'value': 'false'})
            self.ensure(db, settings, {'key': 'download_statistics_active', 'value': ''})
            self.ensure(db, settings, {'key': 'download_statistics_since',
                                      'value': datetime.now().astimezone().isoformat(timespec='seconds')})
            self.ensure(db, settings, {'key': 'download_test_statistics_since',
                                      'value': datetime.now().astimezone().isoformat(timespec='seconds')})
            self.ensure(db, settings, {'key': 'download_method_statistics_since',
                                      'value': datetime.now().astimezone().isoformat(timespec='seconds')})
        if old:
            # One-time upgrade only; regular requests never enumerate task history.
            with engine.begin() as db:
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
