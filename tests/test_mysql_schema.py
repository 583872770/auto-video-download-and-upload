"""Check MySQL index DDL offline, including the MySQL 5.7 schema copy."""
import unittest
from types import SimpleNamespace

from sqlalchemy import Text, LargeBinary
from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.schema import CreateIndex

from webapp.database import Database


class MySQLSchemaTests(unittest.TestCase):
    def schema(self, version):
        mysql = dialect()
        mysql.server_version_info = version
        database = Database.__new__(Database)
        database.engine = SimpleNamespace(dialect=mysql)
        return database.schema(), mysql

    def test_text_and_blob_indexes_have_mysql_prefixes(self):
        for version in ((5, 7, 44), (8, 0, 20), (8, 4, 0)):
            schema, mysql = self.schema(version)
            for table in schema.tables.values():
                for index in table.indexes:
                    with self.subTest(version=version, index=index.name):
                        ddl = str(CreateIndex(index).compile(dialect=mysql))
                        lengths = index.dialect_options['mysql']['length']
                        for column in index.columns:
                            if isinstance(column.type.dialect_impl(mysql), (Text, LargeBinary)):
                                length = lengths.get(column.name) if isinstance(lengths, dict) else lengths
                                self.assertIsInstance(length, int, ddl)
                                self.assertGreater(length, 0, ddl)
                                self.assertIn(f'{column.name}({length})', ddl)

    def test_finished_time_index_ddl_for_new_and_existing_tables(self):
        for version in ((5, 7, 44), (8, 0, 20), (8, 4, 0)):
            with self.subTest(version=version):
                schema, mysql = self.schema(version)
                index = next(index for index in schema.tables['tasks'].indexes
                             if index.name == 'ix_tasks_finished_time')
                # Both create_all for a new table and initialize's explicit
                # index.create for an existing table compile this statement.
                self.assertEqual(str(CreateIndex(index).compile(dialect=mysql)),
                                 'CREATE INDEX ix_tasks_finished_time ON tasks (finished_at(40), seq)')
