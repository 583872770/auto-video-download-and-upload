"""Small, additive migrations; MySQL initialization holds the schema advisory lock.

DDL may auto-commit on MySQL. Each step is inspectable and safe to resume before
its version is recorded. Large queue tables are never rewritten here.
"""
from datetime import datetime, timezone

from sqlalchemy import inspect, select, tuple_

from webapp.database import deliveries, download_lists, job_profiles, media, schema_migrations, test_files


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


def _completed_file_times(engine):
    # Schema changes remain resumable; test data is not backfilled.
    for table in (deliveries, test_files):
        if 'created_epoch' not in {c['name'] for c in inspect(engine).get_columns(table.name)}:
            with engine.begin() as db:
                db.exec_driver_sql('ALTER TABLE ' + table.name + ' ADD COLUMN created_epoch DOUBLE')
        for index in table.indexes:
            if index.name.endswith('_time'):
                index.create(engine, checkfirst=True)


def _cookie_ownership(engine):
    from webapp.cookie_binding import claim, credential_keys
    from webapp.database import cookie_profiles
    order = (cookie_profiles.c.created_at, cookie_profiles.c.scope, cookie_profiles.c.id)
    cursor = None
    while True:
        statement = select(cookie_profiles).where(cookie_profiles.c.deleted == 0).order_by(*order).limit(50)
        if cursor is not None:
            statement = statement.where(tuple_(*order) > cursor)
        with engine.connect() as db:
            rows = db.execute(statement).mappings().all()
        if not rows:
            break
        for row in rows:
            # Existing duplicates remain visible/exportable, but cannot execute
            # on the losing instance. Keep memory and transactions bounded.
            try:
                keys = credential_keys(row['content'].encode())
                with engine.begin() as db:
                    claim(db, row['scope'], keys)
            except ValueError:
                continue
        cursor = tuple(rows[-1][column.name] for column in order)


def _account_credentials(engine):
    from webapp.account_migration import upgrade_account_credentials
    upgrade_account_credentials(engine)


def _delivery_outputs(engine):
    from webapp.database import (delivery_contracts, delivery_runs, delivery_requirements,
                                delivery_artifacts, delivery_totals)
    for table in (delivery_contracts, delivery_runs, delivery_requirements, delivery_artifacts, delivery_totals):
        table.create(engine, checkfirst=True)


def _media_download_times(engine):
    """Add the completion time written by MediaCatalog.index_file."""
    columns = {column['name'] for column in inspect(engine).get_columns(media.name)}
    if 'downloaded_at' not in columns:
        with engine.begin() as db:
            db.exec_driver_sql('ALTER TABLE ' + media.name +
                               ' ADD COLUMN downloaded_at DOUBLE NOT NULL DEFAULT 0')


STEPS = (('20261026_job_edit_versions', _job_edit_versions),
         ('20261027_task_center_indexes', _task_center_indexes),
         ('20261030_url_prefix_index', _add_url_prefix_index),
         ('20261030_optimize_indexes', _optimize_indexes),
         ('20261106_completed_file_times', _completed_file_times),
         ('20261107_cookie_ownership', _cookie_ownership),
         ('20261113_account_credentials', _account_credentials),
         ('20261113_delivery_outputs', _delivery_outputs),
         ('20261114_media_download_times', _media_download_times))


def upgrade(engine):
    for version, step in STEPS:
        if not _applied(engine, version):
            step(engine)
            _record(engine, version)
