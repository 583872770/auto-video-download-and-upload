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
    digest.update(Path(__file__).read_bytes())
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
    Column('created_at', String(40), nullable=False), Column('started_at', String(40)), Column('finished_at', String(40)),
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
Index('ix_tasks_finished_time', tasks.c.finished_at, tasks.c.seq)
Index('ix_tasks_history_order', tasks.c.status, tasks.c.finished_at, tasks.c.seq)
Index('ix_tasks_batch_seq', tasks.c.batch_id, tasks.c.seq)
Index('ix_tasks_batch_ready', tasks.c.batch_id, tasks.c.status, tasks.c.seq)
Index('ix_tasks_video', tasks.c.video_id)
Index('ix_tasks_job_video', tasks.c.batch_id, tasks.c.action, tasks.c.video_id, tasks.c.seq)
# Production title search is independent of ID lookup. SQLite only hosts tests.
Index('ft_tasks_title', tasks.c.title, mysql_prefix='FULLTEXT',
      mysql_with_parser='ngram').ddl_if(dialect='mysql')
Index('ix_tasks_url_prefix', tasks.c.batch_id, tasks.c.url, mysql_length={'url': 128})
Index('ix_tasks_title_prefix', tasks.c.title, mysql_length=191)
Index('ix_tasks_lease', tasks.c.status, tasks.c.lease_until)
Index('ix_tasks_instance_status', tasks.c.instance_id, tasks.c.status, tasks.c.action)
Index('ix_tasks_instance_lease', tasks.c.instance_id, tasks.c.status, tasks.c.lease_until)
Index('ix_tasks_target_queue', tasks.c.target_instance_id, tasks.c.status, tasks.c.available_at)
Index('ix_tasks_log_retention', tasks.c.logs_pruned, tasks.c.finished_at)

# Event-maintained incident groups. Reading global notifications never scans tasks.
notifications = Table('download_notifications', metadata,
    Column('id', String(64), primary_key=True), Column('instance_id', String(64), nullable=False),
    Column('job_id', String(32)), Column('code', String(64), nullable=False),
    Column('status', String(16), nullable=False),
    Column('active_count', BigInteger, nullable=False), Column('action_count', BigInteger, nullable=False),
    Column('first_seen', Float, nullable=False), Column('updated_at', Float, nullable=False),
    Column('resolved_at', Float), Column('detail', Text, nullable=False))
Index('ix_notifications_status_recent', notifications.c.status, notifications.c.updated_at, notifications.c.id)
Index('ix_notifications_instance_status', notifications.c.instance_id, notifications.c.status, notifications.c.updated_at)
Index('ix_notifications_job', notifications.c.job_id, notifications.c.status)
notification_tasks = Table('download_notification_tasks', metadata,
    Column('task_id', String(32), primary_key=True), Column('group_id', String(64), nullable=False),
    Column('state', String(16), nullable=False), Column('first_seen', Float, nullable=False),
    Column('updated_at', Float, nullable=False), Column('retry_at', Float), Column('escalate_at', Float),
    Column('detail', Text, nullable=False))
Index('ix_notification_tasks_group', notification_tasks.c.group_id, notification_tasks.c.updated_at, notification_tasks.c.task_id)
Index('ix_notification_tasks_escalation', notification_tasks.c.escalate_at, notification_tasks.c.task_id)
snapshots = Table('task_snapshots', metadata, Column('id', String(64), primary_key=True), Column('payload', Text, nullable=False))
counters = Table('task_counters', metadata, Column('scope', String(80), primary_key=True),
                 Column('status', String(24), primary_key=True), Column('value', BigInteger, nullable=False))
rotations = Table('rotation_counters', metadata, Column('id', String(128), primary_key=True),
                  Column('succeeded', BigInteger, nullable=False, server_default='0'),
                  Column('failed', BigInteger, nullable=False, server_default='0'))
sequences = Table('sequence_counters', metadata, Column('key', String(32), primary_key=True),
                  Column('value', BigInteger, nullable=False))
settings = Table('schema_settings', metadata, Column('key', String(128), primary_key=True), Column('value', Text))
# None denotes the timestamp at database initialization, not a stored NULL.
# Database copy admission distinguishes bootstrap from application data.
BOOTSTRAP_SETTINGS = {
    'downloads_paused': 'false',
    'download_statistics_active:local': '',
    'download_statistics_since': None,
    'download_test_statistics_since': None,
}
cookie_profiles = Table('youtube_cookie_profiles', metadata,
    Column('scope', String(64), primary_key=True), Column('id', String(32), primary_key=True),
    Column('name', String(80), nullable=False), Column('content', Text, nullable=False),
    Column('created_at', String(40), nullable=False),
    Column('deleted', Integer, nullable=False, server_default='0'))
cookie_bindings = Table('youtube_cookie_bindings', metadata,
    Column('credential', String(64), primary_key=True),
    Column('scope', String(64), nullable=False))

# Global account metadata is independent of executor-local credentials. Secrets
# are authenticated ciphertext; ordinary queries never need the encryption key.
youtube_accounts = Table('youtube_accounts', metadata,
    Column('id', String(32), primary_key=True),
    Column('email', String(254), nullable=False),
    Column('normalized_email', String(254), nullable=False, unique=True),
    Column('secrets_ciphertext', Text, nullable=False), Column('key_id', String(32), nullable=False),
    Column('note', Text, nullable=False), Column('instance_id', String(64)),
    Column('version', Integer, nullable=False),
    Column('created_at', Float, nullable=False), Column('updated_at', Float, nullable=False))
Index('ix_youtube_accounts_instance', youtube_accounts.c.instance_id)
youtube_account_owners = Table('youtube_account_owners', metadata,
    Column('normalized_email', String(254), primary_key=True), Column('instance_id', String(64), nullable=False))
youtube_account_cookies = Table('youtube_account_cookies', metadata,
    Column('instance_id', String(64), primary_key=True), Column('profile_id', String(32), primary_key=True),
    Column('account_id', String(32), nullable=False), Column('confirmed_by', String(64), nullable=False),
    Column('created_at', Float, nullable=False))
Index('uq_youtube_account_cookie', youtube_account_cookies.c.account_id, unique=True)
youtube_account_audit = Table('youtube_account_audit', metadata,
    Column('id', String(32), primary_key=True), Column('account_id', String(32), nullable=False),
    Column('actor', String(64), nullable=False), Column('action', String(32), nullable=False),
    Column('occurred_at', Float, nullable=False))
youtube_profile_auth = Table('youtube_profile_auth', metadata,
    Column('instance_id', String(64), primary_key=True), Column('profile_id', String(32), primary_key=True),
    Column('fingerprint', String(64), nullable=False), Column('context', String(64), nullable=False),
    Column('auth', Text, nullable=False), Column('last_valid_at', Float, nullable=False))

# Updated in the existing fenced settlement transaction, without extra commits.
runtime_hours = Table('download_runtime_hours', metadata,
    Column('scope', String(64), primary_key=True), Column('hour', BigInteger, primary_key=True),
    Column('outcome', String(24), primary_key=True), Column('reason', String(64), primary_key=True),
    Column('task_count', BigInteger, nullable=False), Column('file_count', BigInteger, nullable=False),
    Column('file_bytes', BigInteger, nullable=False), Column('elapsed_seconds', Float, nullable=False))

# Durable, secret-free audit trail for Cookie authentication, cooldown and
# adaptive pacing decisions.  Cookie values are never stored here.
youtube_cookie_events = Table('youtube_cookie_events', metadata,
    Column('id', String(32), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('scope', String(128), nullable=False), Column('profile_id', String(32)),
    Column('profile_name', String(80)), Column('incident_id', String(32)),
    Column('event_type', String(32), nullable=False), Column('phase', String(32)),
    Column('kind', String(64)), Column('message', Text, nullable=False),
    Column('http_status', Integer), Column('retry_after', Float),
    Column('occurred_at', Float, nullable=False), Column('previous_interval', Integer),
    Column('new_interval', Integer), Column('previous_state', String(32)),
    Column('new_state', String(32)), Column('cooldown_until', Float),
    Column('task_id', String(32)), Column('metadata', Text, nullable=False))
Index('ix_youtube_cookie_events_profile_time', youtube_cookie_events.c.profile_id,
      youtube_cookie_events.c.occurred_at)
Index('ix_youtube_cookie_events_incident', youtube_cookie_events.c.incident_id)

download_resources = Table('download_resources', metadata,
    Column('key', String(64), primary_key=True), Column('state', Text, nullable=False))
download_permits = Table('download_permits', metadata,
    Column('token', String(32), primary_key=True), Column('resource', String(64), primary_key=True),
    Column('task_id', String(32), nullable=False), Column('owner', String(128), nullable=False),
    Column('units', Integer, nullable=False))
Index('ix_download_permits_resource', download_permits.c.resource)
batches = Table('batches', metadata, Column('id', String(32), primary_key=True),
                Column('seq', BigInteger, nullable=False, unique=True), Column('created_at', String(40), nullable=False),
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
                Column('created_at', String(40), nullable=False), Column('path', Text))
job_profiles = Table('job_profiles', metadata,
    Column('id', String(32), primary_key=True), Column('note', Text, nullable=False),
    Column('revision', Integer, nullable=False),
    Column('archived', Integer, nullable=False), Column('deleted', Integer, nullable=False),
    Column('price_set', Integer, nullable=False), Column('updated_at', String(40), nullable=False),
    Column('edit_version', BigInteger, nullable=False, server_default='1'))
Index('ix_job_profiles_lifecycle', job_profiles.c.deleted, job_profiles.c.archived, job_profiles.c.id)
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
              Column('downloaded_at', Float, nullable=False, server_default='0'),
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
              Column('last_indexed', String(40)), Column('scan_id', String(32)),
              Column('scanned', BigInteger, nullable=False, server_default='0'))

download_sessions = Table('download_sessions', metadata,
    Column('id', String(32), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('started_at', String(40), nullable=False), Column('ended_at', String(40)), Column('stop_requested_at', String(40)),
    Column('status', String(24), nullable=False), Column('label', Text, nullable=False),
    *(Column(name, BigInteger, nullable=False, server_default='0') for name in
      ('attempted', 'running', 'succeeded', 'failed', 'skipped', 'test_succeeded', 'file_count', 'file_bytes')))
session_tasks = Table('download_session_tasks', metadata,
    Column('session_id', String(32), primary_key=True), Column('task_id', String(32), primary_key=True),
    Column('seq', BigInteger, nullable=False), Column('video_id', String(256)), Column('batch_id', String(32)),
    Column('status', String(24), nullable=False), Column('started_at', String(40), nullable=False),
    Column('finished_at', String(40)), Column('error', Text))
Index('ix_session_tasks_recent', session_tasks.c.session_id, session_tasks.c.seq)
Index('ix_session_tasks_status', session_tasks.c.session_id, session_tasks.c.status, session_tasks.c.seq)
deliveries = Table('download_deliveries', metadata,
    Column('id', String(64), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('session_id', String(32), nullable=False), Column('task_id', String(32), nullable=False),
    Column('video_id', String(256)), Column('title', Text, nullable=False), Column('path', Text, nullable=False),
    Column('size', BigInteger, nullable=False), Column('created_at', String(40), nullable=False),
    Column('created_epoch', Float))
Index('ix_deliveries_session', deliveries.c.session_id, deliveries.c.seq)
Index('ix_deliveries_task_seq', deliveries.c.task_id, deliveries.c.seq)
Index('ix_deliveries_time', deliveries.c.session_id, deliveries.c.created_epoch)

# Multi-output requirements and immutable delivery artifacts.
delivery_contracts = Table('job_delivery_contracts', metadata,
    Column('job_id', String(32), primary_key=True), Column('version', Integer, primary_key=True),
    Column('spec', Text, nullable=False), Column('created_at', String(40), nullable=False))
delivery_runs = Table('delivery_runs', metadata,
    Column('task_id', String(32), primary_key=True), Column('job_id', String(32), nullable=False),
    Column('version', Integer, nullable=False), Column('spec', Text, nullable=False),
    Column('status', String(24), nullable=False), Column('test_mode', Integer, nullable=False, server_default='0'))
Index('ix_delivery_runs_job_status', delivery_runs.c.job_id, delivery_runs.c.status, delivery_runs.c.task_id)
delivery_requirements = Table('delivery_requirements', metadata,
    Column('task_id', String(32), primary_key=True), Column('key', String(64), primary_key=True),
    Column('kind', String(24), nullable=False), Column('language', String(32), nullable=False),
    Column('variant', String(64), nullable=False), Column('status', String(24), nullable=False),
    Column('error', Text), Column('artifact_id', String(64)), Column('waived_by', String(64)),
    Column('waived_reason', Text), Column('updated_at', String(40), nullable=False))
delivery_work = Table('delivery_work', metadata,
    Column('task_id', String(32), primary_key=True),
    Column('instance_id', String(64), nullable=False),
    Column('workspace', Text, nullable=False), Column('disk_lease', String(32), nullable=False),
    Column('plan', Text, nullable=False), Column('created_at', Float, nullable=False),
    Column('attempts', Integer, nullable=False, server_default='0'),
    Column('source_attempts', Integer, nullable=False))
Index('ix_delivery_work_instance', delivery_work.c.instance_id, delivery_work.c.created_at)
delivery_artifacts = Table('delivery_artifacts', metadata,
    Column('id', String(64), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('job_id', String(32), nullable=False), Column('task_id', String(32), nullable=False),
    Column('contract_version', Integer, nullable=False),
    Column('video_id', String(256)), Column('title', Text, nullable=False),
    Column('kind', String(24), nullable=False), Column('language', String(32), nullable=False),
    Column('subtitle_source', String(24), nullable=False, server_default=''),
    Column('variant', String(64), nullable=False), Column('path', Text, nullable=False),
    Column('size', BigInteger, nullable=False), Column('instance_id', String(64), nullable=False),
    Column('storage', String(16), nullable=False), Column('object_key', Text),
    Column('available', Integer, nullable=False), Column('test_mode', Integer, nullable=False),
    Column('created_at', String(40), nullable=False))
Index('ix_delivery_artifacts_job_seq', delivery_artifacts.c.job_id, delivery_artifacts.c.seq)
Index('ix_delivery_artifacts_instance_seq', delivery_artifacts.c.instance_id, delivery_artifacts.c.seq)
delivery_totals = Table('delivery_totals', metadata,
    Column('job_id', String(32), primary_key=True), Column('kind', String(24), primary_key=True),
    Column('test_mode', Integer, primary_key=True),
    Column('file_count', BigInteger, nullable=False), Column('file_bytes', BigInteger, nullable=False))

# Separate ledgers keep deleted test files out of delivery billing and avoid
# mixing test receipts with billable production deliveries.
test_files = Table('download_test_files', metadata,
    Column('id', String(64), primary_key=True), Column('seq', BigInteger, nullable=False, unique=True),
    Column('session_id', String(32), nullable=False), Column('task_id', String(32), nullable=False),
    Column('video_id', String(256)), Column('title', Text, nullable=False), Column('path', Text, nullable=False),
    Column('size', BigInteger, nullable=False), Column('created_at', String(40), nullable=False),
    Column('created_epoch', Float))
Index('ix_test_files_session', test_files.c.session_id, test_files.c.seq)
Index('ix_test_files_task_seq', test_files.c.task_id, test_files.c.seq)
Index('ix_test_files_time', test_files.c.session_id, test_files.c.created_epoch)
test_totals = Table('download_test_totals', metadata,
    Column('session_id', String(32), primary_key=True),
    Column('test_file_count', BigInteger, nullable=False, server_default='0'),
    Column('test_file_bytes', BigInteger, nullable=False, server_default='0'))

for table in metadata.tables.values():
    # YouTube IDs and file paths are case sensitive. InnoDB supplies row locks;
    # the NO PAD binary collation also preserves distinct trailing characters.
    table.dialect_options['mysql'].update(
        engine='InnoDB', charset='utf8mb4', collate='utf8mb4_0900_bin')

_lock = threading.Lock()
_engines = {}


class SchemaInitializationBusy(RuntimeError):
    """Another initializer holds the schema advisory lock."""


class SchemaMismatch(ValueError):
    """An existing database does not implement the current schema."""


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
                            if engine.dialect.is_mariadb or engine.dialect.server_version_info < (8, 4):
                                raise ValueError('请使用 MySQL 8.4 或更新版本，不支持 MariaDB。')
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
        return self.mysql

    def schema(self):
        return metadata

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
            updates = {column.name: incoming[column.name] for column in table.c
                       if column.name not in keys and column.computed is None}
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
        # Serialize first-time table creation across instances.
        engine = create_engine(self.url, poolclass=NullPool, hide_parameters=True,
            isolation_level='READ COMMITTED', connect_args={
                'charset': 'utf8mb4', 'connect_timeout': 10, 'read_timeout': 1800, 'write_timeout': 60})
        lock_name = 'video_schema_' + hashlib.sha256(engine.url.database.encode('utf-8')).hexdigest()[:40]
        try:
            with engine.connect() as guard:
                self.startup_report('正在检查数据库初始化状态…')
                acquired = guard.execute(text('SELECT GET_LOCK(:name, 10)'), {'name': lock_name}).scalar_one()
                if acquired != 1:
                    raise SchemaInitializationBusy('另一个实例正在初始化数据库。')
                try:
                    self._initialize_schema(engine)
                finally:
                    try:
                        guard.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': lock_name})
                    except SQLAlchemyError:
                        # A disconnected guard releases the named lock itself.
                        pass
        finally:
            engine.dispose()

    def validate_schema(self, engine):
        """Reject incompatible structures before any DDL or application writes.

        Development databases must use the current schema. Initialization never
        repairs old tables, backfills history, or deletes existing application data.
        """
        inspector = inspect(engine)
        existing = set(inspector.get_table_names())
        problems = []
        for name in sorted(existing - set(metadata.tables)):
            problems.append(f'{name}：不属于当前结构的表')
        for table in metadata.tables.values():
            if table.name not in existing:
                continue
            columns = {column['name']: column for column in inspector.get_columns(table.name)}
            if columns.keys() != set(table.c.keys()):
                problems.append(f'{table.name}：字段集合不匹配')
                continue
            for column in table.c:
                actual = columns[column.name]
                expected = column.type.dialect_impl(engine.dialect)
                actual_type = actual['type'].copy()
                # MySQL reflection attaches the table's charset/collation to
                # text types; compare storage independently of those options.
                for option in ('charset', 'collation'):
                    if hasattr(actual_type, option):
                        setattr(actual_type, option, None)
                if (actual_type.compile(dialect=engine.dialect) != expected.compile(dialect=engine.dialect)
                        or (engine.dialect.name == 'mysql' and
                            getattr(actual['type'], 'collation', None) not in (None, 'utf8mb4_0900_bin'))
                        or bool(actual.get('computed')) != bool(column.computed)
                        or actual['nullable'] != column.nullable):
                    problems.append(f'{table.name}.{column.name}：类型或约束不匹配')
            if inspector.get_pk_constraint(table.name)['constrained_columns'] != [c.name for c in table.primary_key]:
                problems.append(f'{table.name}：主键不匹配')
            indexes = {index['name']: index for index in inspector.get_indexes(table.name)}
            for index in table.indexes:
                fulltext = index.dialect_options['mysql']['prefix'] == 'FULLTEXT'
                if fulltext and engine.dialect.name != 'mysql':
                    continue
                actual = indexes.get(index.name)
                prefix = index.dialect_options['mysql']['length'] or {}
                if isinstance(prefix, int):
                    prefix = {c.name: prefix for c in index.columns}
                if (not actual or actual['column_names'] != [c.name for c in index.columns]
                        or bool(actual['unique']) != bool(index.unique)
                        or (engine.dialect.name == 'mysql' and
                            (actual.get('dialect_options', {}).get('mysql_length', {}) != prefix
                             or actual.get('dialect_options', {}).get('mysql_prefix') !=
                                index.dialect_options['mysql']['prefix']
                             or actual.get('dialect_options', {}).get('mysql_with_parser') !=
                                index.dialect_options['mysql']['with_parser']))):
                    problems.append(f'{table.name}.{index.name}：索引不匹配')
            if engine.dialect.name == 'mysql':
                options = inspector.get_table_options(table.name)
                if (options.get('mysql_engine', '').lower() != 'innodb'
                        or options.get('mysql_collate') != 'utf8mb4_0900_bin'):
                    problems.append(f'{table.name}：需要 InnoDB 和 utf8mb4_0900_bin 排序规则')
            unique = {tuple(item['column_names']) for item in inspector.get_unique_constraints(table.name)}
            unique.update(tuple(item['column_names']) for item in indexes.values() if item['unique'])
            for constraint in table.constraints:
                if constraint.__class__.__name__ == 'UniqueConstraint' and tuple(c.name for c in constraint.columns) not in unique:
                    problems.append(f'{table.name}：唯一约束不匹配')
        if problems:
            raise SchemaMismatch('数据库结构与当前代码不一致；开发阶段不执行历史升级。'
                '请备份后使用当前结构重新初始化测试数据库。现有数据未被修改。详情：' + '；'.join(problems[:12]))

    def _initialize_schema(self, engine):
        self.startup_report('正在检查当前数据库表和索引…')
        self.validate_schema(engine)
        metadata.create_all(engine)
        with engine.begin() as db:
            stamp = datetime.now().astimezone().isoformat(timespec='seconds')
            for key, value in BOOTSTRAP_SETTINGS.items():
                self.ensure(db, settings, {'key': key, 'value': stamp if value is None else value})
