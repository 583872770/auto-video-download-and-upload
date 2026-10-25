"""Local launches must select MySQL without exposing credentials or altering old state."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.engine import make_url

from webapp.bootstrap import configure_database_environment
from webapp.database import Database


class LocalBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / '.env'
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def write_env(self):
        self.path.write_text("MYSQL_HOST=db.example.test\nMYSQL_DATABASE=videos\n"
                             "MYSQL_USER=video\nMYSQL_PASSWORD='p@ss:#/$ ${SECRET} 秘密'\n",
                             encoding='utf-8-sig')

    def test_default_file_overrides_stale_connection_and_preserves_original(self):
        self.write_env()
        old = self.root / 'database.json'
        old.write_text(json.dumps({'url': 'postgresql+psycopg://old-host/old-db'}), encoding='utf-8')
        original = old.read_bytes()
        with patch('webapp.bootstrap.ROOT', self.root):
            configure_database_environment()
        url = make_url(os.environ['VIDEO_DATABASE_URL'])
        self.assertEqual(url.drivername, 'mysql+pymysql')
        self.assertEqual(url.password, 'p@ss:#/$ ${SECRET} 秘密')
        self.assertEqual((url.host, url.port, url.database), ('db.example.test', 3306, 'videos'))
        # Exercise the same default-state URL selection, without connecting to MySQL.
        state = self.root / '.cache' / 'web'
        state.mkdir(parents=True)
        connection = state / 'database.json'
        connection.write_bytes(original)
        with patch('webapp.database.ROOT', self.root), patch('webapp.database.create_engine') as engine:
            engine.return_value.dialect.name = 'mysql'
            engine.return_value.dialect.is_mariadb = False
            engine.return_value.dialect.server_version_info = (8, 4, 0)
            with patch.object(Database, 'initialize'), patch('webapp.database._engines', {}):
                database = Database(state)
                self.assertEqual(database.url, os.environ['VIDEO_DATABASE_URL'])
        self.assertEqual(connection.read_bytes(), original)
        self.assertEqual(old.read_bytes(), original)

    def test_explicit_file_and_process_variable_priority(self):
        self.write_env()
        os.environ['MYSQL_HOST'] = 'process.example.test'
        os.environ['MYSQL_PORT'] = '3307'
        configure_database_environment(self.path)
        url = make_url(os.environ['VIDEO_DATABASE_URL'])
        self.assertEqual((url.host, url.port), ('process.example.test', 3307))

    def test_explicit_url_has_priority(self):
        self.write_env()
        os.environ['VIDEO_DATABASE_URL'] = 'mysql+pymysql://custom-host/custom-db'
        configure_database_environment(self.path)
        self.assertEqual(os.environ['VIDEO_DATABASE_URL'], 'mysql+pymysql://custom-host/custom-db')

    def test_missing_explicit_file_fails(self):
        with self.assertRaisesRegex(ValueError, '--env-file'):
            configure_database_environment(self.path)

    def test_missing_default_file_preserves_unconfigured_development_mode(self):
        with patch('webapp.bootstrap.ROOT', self.root):
            configure_database_environment()
        self.assertNotIn('VIDEO_DATABASE_URL', os.environ)

    def test_partial_credentials_fail_without_leaking_values(self):
        self.path.write_text('MYSQL_HOST=private-host\nMYSQL_PASSWORD=private-secret\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'MySQL 配置不完整') as failure:
            configure_database_environment(self.path)
        self.assertNotIn('private-host', str(failure.exception))
        self.assertNotIn('private-secret', str(failure.exception))

    def test_invalid_ports_fail_with_safe_message(self):
        self.write_env()
        for port in ('0', '65536', 'not-a-port'):
            with self.subTest(port=port):
                os.environ['MYSQL_PORT'] = port
                with self.assertRaisesRegex(ValueError, 'MYSQL_PORT'):
                    configure_database_environment(self.path)


if __name__ == '__main__':
    unittest.main()
