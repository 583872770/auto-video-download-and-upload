"""Current schema initialization must never convert or erase existing data."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import inspect, select
from sqlalchemy.engine import make_url

from webapp.database import (Database, SchemaInitializationBusy, SchemaMismatch,
                             metadata, settings, tasks)


class CurrentSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Database(Path(temporary.name))
        self.addCleanup(self.database.engine.dispose)

    def test_current_database_reopens_without_rewriting_application_data(self):
        with self.database.engine.begin() as db:
            db.execute(settings.insert().values(key='application-data', value='keep'))
        self.database.initialize()
        self.database.initialize()
        with self.database.engine.connect() as db:
            self.assertEqual(db.execute(select(settings.c.value).where(
                settings.c.key == 'application-data')).scalar_one(), 'keep')
        self.assertNotIn('schema_migrations', inspect(self.database.engine).get_table_names())

    def test_incomplete_old_table_is_rejected_before_ddl_or_bootstrap_writes(self):
        with self.database.engine.begin() as db:
            db.exec_driver_sql('ALTER TABLE tasks DROP COLUMN policy_version')
            db.execute(settings.delete().where(settings.c.key == 'downloads_paused'))
            db.execute(settings.insert().values(key='keep', value='test data'))
        with self.assertRaises(SchemaMismatch):
            self.database.initialize()
        self.assertNotIn('policy_version', {c['name'] for c in inspect(self.database.engine).get_columns('tasks')})
        with self.database.engine.connect() as db:
            self.assertIsNone(db.execute(select(settings.c.value).where(settings.c.key == 'downloads_paused')).scalar())
            self.assertEqual(db.execute(select(settings.c.value).where(settings.c.key == 'keep')).scalar_one(), 'test data')

    def test_missing_unique_account_constraint_is_rejected(self):
        with self.database.engine.begin() as db:
            db.exec_driver_sql('DROP INDEX uq_youtube_account_cookie')
        with self.assertRaisesRegex(SchemaMismatch, 'uq_youtube_account_cookie'):
            self.database.initialize()

    def test_time_fields_use_one_direct_order_index_without_redundant_indexes(self):
        self.assertNotIn('finished_order', tasks.c)
        for name in ('created_at', 'started_at', 'finished_at'):
            self.assertEqual(tasks.c[name].type.length, 40)
        indexes = {i.name: tuple(c.name for c in i.columns) for i in tasks.indexes}
        self.assertEqual(indexes['ix_tasks_history_order'], ('status', 'finished_at', 'seq'))
        self.assertEqual(indexes['ix_tasks_instance_lease'], ('instance_id', 'status', 'lease_until'))
        for table in metadata.tables.values():
            keys = [(tuple(c.name for c in i.columns), bool(i.unique)) for i in table.indexes]
            self.assertEqual(len(keys), len(set(keys)), table.name)


class SchemaLockTests(unittest.TestCase):
    def test_initializer_holding_lock_prevents_parallel_schema_work(self):
        database = object.__new__(Database)
        database.url = 'mysql+pymysql://fixture:fixture@127.0.0.1/fixture'
        database.engine = MagicMock()
        database.engine.dialect.name = 'mysql'
        database.startup_report = MagicMock()
        engine = MagicMock()
        engine.url = make_url(database.url)
        guard = engine.connect.return_value.__enter__.return_value
        guard.execute.return_value.scalar_one.return_value = 0
        with patch('webapp.database.create_engine', return_value=engine), \
                patch.object(database, '_initialize_schema') as initialize:
            with self.assertRaises(SchemaInitializationBusy):
                database.initialize()
        initialize.assert_not_called()
        engine.dispose.assert_called_once()

    def test_initialization_failure_releases_advisory_lock(self):
        database = object.__new__(Database)
        database.url = 'mysql+pymysql://fixture:fixture@127.0.0.1/fixture'
        database.engine = MagicMock()
        database.engine.dialect.name = 'mysql'
        database.startup_report = MagicMock()
        engine = MagicMock()
        engine.url = make_url(database.url)
        guard = engine.connect.return_value.__enter__.return_value
        guard.execute.return_value.scalar_one.return_value = 1
        with patch('webapp.database.create_engine', return_value=engine), \
                patch.object(database, '_initialize_schema', side_effect=RuntimeError('fixture')):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                database.initialize()
        self.assertIn('RELEASE_LOCK', str(guard.execute.call_args.args[0]))
        engine.dispose.assert_called_once()
