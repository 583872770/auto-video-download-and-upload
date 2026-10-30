"""Small, additive migrations; MySQL initialization holds the schema advisory lock.

DDL may auto-commit on MySQL. Each step is inspectable and safe to resume before
its version is recorded. Large queue tables are never rewritten here.
"""
from datetime import datetime, timezone

from sqlalchemy import inspect, select

from webapp.database import deliveries, download_lists, job_profiles, schema_migrations, test_files


def _applied(engine, version):
    with engine.connect() as db:
        return bool(db.execute(select(schema_migrations.c.version).where(schema_migrations.c.version == version)).first())


def _record(engine, version):
    with engine.begin() as db:
        db.execute(schema_migrations.insert().values(version=version,
            applied_at=datetime.now(timezone.utc).isoformat(timespec='seconds')))


def _job_edit_versions(engine):
    columns = {column['name'] for column in inspect(engine).get_columns('job_profiles')}
    with engine.begin() as db:
        for name in ('edit_version', 'policy_version'):
            if name not in columns:
                db.exec_driver_sql(f'ALTER TABLE job_profiles ADD COLUMN {name} BIGINT NOT NULL DEFAULT 1')
    for table in (deliveries, test_files):
        for index in table.indexes:
            if index.name.endswith('_task_seq'):
                index.create(engine, checkfirst=True)


def _task_center_indexes(engine):
    # create_all only creates missing tables; existing job tables need these
    # indexes added explicitly. Both tables hold one row per customer task.
    for table, name in ((download_lists, 'ix_download_lists_released'), (job_profiles, 'ix_job_profiles_lifecycle')):
        next(index for index in table.indexes if index.name == name).create(engine, checkfirst=True)


def _add_url_prefix_index(engine):
    """Add prefix index on tasks.url to avoid full table scan in batch deduplication.

    Uses 128-char prefix (YouTube URLs are ~43 chars, sufficient for uniqueness).
    MySQL TEXT columns require explicit length for indexes.
    """
    inspector = inspect(engine)
    existing_indexes = {idx['name'] for idx in inspector.get_indexes('tasks')}

    if 'ix_tasks_url_prefix' not in existing_indexes:
        with engine.begin() as db:
            # MySQL syntax for prefix index
            if 'mysql' in engine.dialect.name:
                db.exec_driver_sql('CREATE INDEX ix_tasks_url_prefix ON tasks (batch_id, url(128))')
            # SQLite doesn't support prefix indexes, but url queries are less critical in local dev
            else:
                db.exec_driver_sql('CREATE INDEX ix_tasks_url_prefix ON tasks (batch_id, url)')


def _optimize_indexes(engine):
    """删除冗余索引，添加缺失索引。

    删除：
    - ix_tasks_seq: unique 列自动有索引
    - ix_tasks_id_prefix: 主键已是聚簇索引
    - ix_tasks_video_prefix: 与 ix_tasks_video 重复

    新增：
    - ix_tasks_instance_lease: 用于 heartbeat 和 recover 查询
    - ix_reservations_source: reservations 表外键
    """
    inspector = inspect(engine)
    existing_indexes = {idx['name'] for idx in inspector.get_indexes('tasks')}

    with engine.begin() as db:
        # 删除冗余索引（仅 MySQL）
        if 'mysql' in engine.dialect.name:
            for idx_name in ('ix_tasks_seq', 'ix_tasks_id_prefix', 'ix_tasks_video_prefix'):
                if idx_name in existing_indexes:
                    db.exec_driver_sql(f'DROP INDEX {idx_name} ON tasks')

        # 添加 instance_lease 索引
        if 'ix_tasks_instance_lease' not in existing_indexes:
            db.exec_driver_sql('CREATE INDEX ix_tasks_instance_lease ON tasks (instance_id, status, lease_until)')

        # 添加 reservations source 索引
        reservations_indexes = {idx['name'] for idx in inspector.get_indexes('download_reservations')}
        if 'ix_reservations_source' not in reservations_indexes:
            db.exec_driver_sql('CREATE INDEX ix_reservations_source ON download_reservations (`key`)')


STEPS = (('20261026_job_edit_versions', _job_edit_versions),
         ('20261027_task_center_indexes', _task_center_indexes),
         ('20261030_url_prefix_index', _add_url_prefix_index),
         ('20261030_optimize_indexes', _optimize_indexes))


def upgrade(engine):
    for version, step in STEPS:
        if not _applied(engine, version):
            step(engine)
            _record(engine, version)
