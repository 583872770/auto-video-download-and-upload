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


STEPS = (('20261026_job_edit_versions', _job_edit_versions),
         ('20261027_task_center_indexes', _task_center_indexes))


def upgrade(engine):
    for version, step in STEPS:
        if not _applied(engine, version):
            step(engine)
            _record(engine, version)
