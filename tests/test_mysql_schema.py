"""Check MySQL index DDL offline, for the current MySQL 8.4 schema."""
import unittest
from types import SimpleNamespace

from sqlalchemy import Text, LargeBinary
from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.schema import CreateIndex

from tests.test_mysql import StandaloneMySQLFixture
from webapp.database import Database, SchemaMismatch


class MySQLSchemaTests(unittest.TestCase):
    def schema(self, version):
        mysql = dialect()
        mysql.server_version_info = version
        database = Database.__new__(Database)
        database.engine = SimpleNamespace(dialect=mysql)
        return database.schema(), mysql

    def test_text_and_blob_indexes_have_mysql_prefixes(self):
        for version in ((8, 4, 0),):
            schema, mysql = self.schema(version)
            for table in schema.tables.values():
                for index in table.indexes:
                    with self.subTest(version=version, index=index.name):
                        ddl = str(CreateIndex(index).compile(dialect=mysql))
                        if index.dialect_options['mysql']['prefix'] == 'FULLTEXT':
                            self.assertIn('CREATE FULLTEXT INDEX', ddl)
                            self.assertIn('WITH PARSER ngram', ddl)
                            continue
                        lengths = index.dialect_options['mysql']['length']
                        for column in index.columns:
                            if isinstance(column.type.dialect_impl(mysql), (Text, LargeBinary)):
                                length = lengths.get(column.name) if isinstance(lengths, dict) else lengths
                                self.assertIsInstance(length, int, ddl)
                                self.assertGreater(length, 0, ddl)
                                self.assertIn(f'{column.name}({length})', ddl)

    def test_finished_time_index_ddl(self):
        for version in ((8, 4, 0),):
            with self.subTest(version=version):
                schema, mysql = self.schema(version)
                index = next(index for index in schema.tables['tasks'].indexes
                             if index.name == 'ix_tasks_finished_time')
                self.assertEqual(str(CreateIndex(index).compile(dialect=mysql)),
                                 'CREATE INDEX ix_tasks_finished_time ON tasks (finished_at, seq)')

    def test_job_video_index_ddl(self):
        schema, mysql = self.schema((8, 4, 0))
        index = next(i for i in schema.tables['tasks'].indexes if i.name == 'ix_tasks_job_video')
        self.assertEqual(str(CreateIndex(index).compile(dialect=mysql)),
            'CREATE INDEX ix_tasks_job_video ON tasks (batch_id, action, video_id, seq)')


class MySQLCurrentSchemaTests(StandaloneMySQLFixture, unittest.TestCase):
    def test_reopen_accepts_reflected_current_types_and_index_prefixes(self):
        self.store.db.initialize()

    def test_reduced_lease_precision_is_rejected(self):
        with self.store.db.engine.begin() as db:
            db.exec_driver_sql('ALTER TABLE tasks MODIFY lease_until FLOAT NULL')
        with self.assertRaisesRegex(SchemaMismatch, 'tasks.lease_until'):
            self.store.db.initialize()

    def test_reduced_sequence_range_is_rejected(self):
        with self.store.db.engine.begin() as db:
            db.exec_driver_sql('ALTER TABLE tasks MODIFY seq INT NOT NULL')
        with self.assertRaisesRegex(SchemaMismatch, 'tasks.seq'):
            self.store.db.initialize()

    def test_short_json_storage_is_rejected(self):
        with self.store.db.engine.begin() as db:
            db.exec_driver_sql('ALTER TABLE tasks MODIFY payload TEXT NOT NULL')
        with self.assertRaisesRegex(SchemaMismatch, 'tasks.payload'):
            self.store.db.initialize()

    def test_case_insensitive_identifiers_are_rejected(self):
        with self.store.db.engine.begin() as db:
            db.exec_driver_sql('ALTER TABLE download_archive CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci')
        with self.assertRaisesRegex(SchemaMismatch, 'download_archive'):
            self.store.db.initialize()

    def test_wrong_index_prefix_is_rejected(self):
        with self.store.db.engine.begin() as db:
            db.exec_driver_sql('DROP INDEX ix_tasks_title_prefix ON tasks')
            db.exec_driver_sql('CREATE INDEX ix_tasks_title_prefix ON tasks (title(100))')
        with self.assertRaisesRegex(SchemaMismatch, 'ix_tasks_title_prefix'):
            self.store.db.initialize()
