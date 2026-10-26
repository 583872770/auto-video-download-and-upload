"""Small, additive migrations; MySQL initialization holds the schema advisory lock.

DDL may auto-commit on MySQL. Each step is inspectable and safe to resume before
its version is recorded. Large queue tables are never rewritten here.
"""
from datetime import datetime, timezone

from sqlalchemy import inspect, select

from webapp.database import deliveries, schema_migrations, test_files


def upgrade(engine):
    version = '20261026_job_edit_versions'
    with engine.connect() as db:
        if db.execute(select(schema_migrations.c.version).where(schema_migrations.c.version == version)).first():
            return
    columns = {column['name'] for column in inspect(engine).get_columns('job_profiles')}
    with engine.begin() as db:
        for name in ('edit_version', 'policy_version'):
            if name not in columns:
                db.exec_driver_sql(f'ALTER TABLE job_profiles ADD COLUMN {name} BIGINT NOT NULL DEFAULT 1')
    for table in (deliveries, test_files):
        for index in table.indexes:
            if index.name.endswith('_task_seq'):
                index.create(engine, checkfirst=True)
    with engine.begin() as db:
        db.execute(schema_migrations.insert().values(version=version,
            applied_at=datetime.now(timezone.utc).isoformat(timespec='seconds')))
