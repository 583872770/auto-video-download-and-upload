"""Schema upgrades must serialize and must not repeat a server-side ALTER."""
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url

from webapp.database import Database, SchemaUpgradeInProgress


class SchemaUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.database = object.__new__(Database)
        self.database.url = 'mysql+pymysql://fixture:fixture@127.0.0.1/fixture'
        self.database.engine = MagicMock()
        self.database.engine.dialect.name = 'mysql'
        self.database.startup_report = MagicMock()
        self.engine = MagicMock()
        self.engine.url = make_url(self.database.url)
        self.guard = self.engine.connect.return_value.__enter__.return_value

    def results(self, *, locked=1, pending=None):
        acquired, running, released = MagicMock(), MagicMock(), MagicMock()
        acquired.scalar_one.return_value = locked
        running.first.return_value = pending
        self.guard.execute.side_effect = [acquired, running, released]

    def test_other_instance_holds_upgrade_lock_and_no_schema_work_runs(self):
        self.results(locked=0)
        with patch('webapp.database.create_engine', return_value=self.engine), \
                patch.object(self.database, '_initialize_schema') as migrate:
            with self.assertRaises(SchemaUpgradeInProgress):
                self.database.initialize()
        migrate.assert_not_called()
        self.engine.dispose.assert_called_once()
        self.assertEqual(self.guard.execute.call_count, 1)

    def test_old_alter_is_detected_without_repeating_it_and_lock_is_released(self):
        self.results(pending=(123,))
        with patch('webapp.database.create_engine', return_value=self.engine), \
                patch.object(self.database, '_initialize_schema') as migrate:
            with self.assertRaises(SchemaUpgradeInProgress):
                self.database.initialize()
        migrate.assert_not_called()
        self.assertIn('RELEASE_LOCK', str(self.guard.execute.call_args.args[0]))
        self.engine.dispose.assert_called_once()

    def test_upgrade_uses_separate_long_timeout_connection_and_releases_lock(self):
        self.results()
        original_engine = self.database.engine
        with patch('webapp.database.create_engine', return_value=self.engine) as create, \
                patch.object(self.database, '_initialize_schema') as migrate:
            self.database.initialize()
        migrate.assert_called_once_with(self.engine)
        self.assertGreater(create.call_args.kwargs['connect_args']['read_timeout'], 60)
        self.assertIs(self.database.engine, original_engine)
        self.assertIn('RELEASE_LOCK', str(self.guard.execute.call_args.args[0]))
        self.engine.dispose.assert_called_once()

    def test_upgrade_failure_still_releases_lock_and_disposes_ddl_connections(self):
        self.results()
        with patch('webapp.database.create_engine', return_value=self.engine), \
                patch.object(self.database, '_initialize_schema', side_effect=RuntimeError('fixture')):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                self.database.initialize()
        self.assertIn('RELEASE_LOCK', str(self.guard.execute.call_args.args[0]))
        self.engine.dispose.assert_called_once()

    def test_missing_columns_share_one_alter_instead_of_three_table_rebuilds(self):
        inspector, schema = MagicMock(), MagicMock()
        inspector.has_table.return_value = True
        inspector.get_columns.return_value = [{'name': 'id'}]
        schema.tables['tasks'].indexes = []
        connection = self.engine.begin.return_value.__enter__.return_value
        with patch('webapp.database.inspect', return_value=inspector), \
                patch.object(self.database, 'schema', return_value=schema), \
                patch.object(self.database, 'ensure'):
            self.database._initialize_schema(self.engine)
        connection.exec_driver_sql.assert_called_once()
        sql = connection.exec_driver_sql.call_args.args[0]
        self.assertEqual(sql.count('ADD COLUMN'), 3)
        self.assertIn('policy_version', sql)


if __name__ == '__main__':
    unittest.main()
