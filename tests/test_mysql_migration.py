"""Migration destination admission must distinguish bootstrap from real data."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from scripts.migrate_mysql import counts, pristine_target
from webapp.database import BOOTSTRAP_SETTINGS, Database, cookie_bindings, schema_migrations, settings


class MigrationDestinationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Database(Path(temporary.name))
        self.addCleanup(self.database.engine.dispose)

    def test_initialized_empty_database_is_accepted_without_changing_rows(self):
        with self.database.engine.begin() as db:
            before = counts(db)
            self.assertTrue(pristine_target(db))
            self.assertEqual(counts(db), before)
            self.assertEqual(before[settings.name], len(BOOTSTRAP_SETTINGS))
            self.assertGreater(before[schema_migrations.name], 0)

    def test_custom_settings_are_rejected_and_preserved(self):
        with self.database.engine.begin() as db:
            db.execute(settings.insert().values(key='runtime', value='custom configuration'))
            self.assertFalse(pristine_target(db))
            self.assertEqual(db.execute(select(settings.c.value).where(
                settings.c.key == 'runtime')).scalar_one(), 'custom configuration')

    def test_modified_bootstrap_values_and_invalid_timestamps_are_rejected(self):
        with self.database.engine.begin() as db:
            for key, value in [('downloads_paused', 'true'), ('download_statistics_active', 'existing-session'),
                               ('download_statistics_since', 'not a timestamp')]:
                with self.subTest(key=key):
                    original = db.execute(select(settings.c.value).where(settings.c.key == key)).scalar_one()
                    db.execute(settings.update().where(settings.c.key == key).values(value=value))
                    self.assertFalse(pristine_target(db))
                    db.execute(settings.update().where(settings.c.key == key).values(value=original))
            self.assertTrue(pristine_target(db))

    def test_missing_bootstrap_metadata_is_rejected(self):
        with self.database.engine.begin() as db:
            db.execute(settings.delete().where(settings.c.key == 'download_statistics_active'))
            self.assertFalse(pristine_target(db))

    def test_account_alias_history_is_not_mistaken_for_empty_metadata(self):
        with self.database.engine.begin() as db:
            for value in ('{"node-a":{"old":"current"}}', '[]', 'invalid'):
                with self.subTest(value=value):
                    db.execute(settings.update().where(
                        settings.c.key == 'youtube_account_cookie_aliases').values(value=value))
                    self.assertFalse(pristine_target(db))
                    self.assertEqual(db.execute(select(settings.c.value).where(
                        settings.c.key == 'youtube_account_cookie_aliases')).scalar_one(), value)

    def test_unknown_schema_version_is_rejected(self):
        with self.database.engine.begin() as db:
            db.execute(schema_migrations.insert().values(version='future-unknown', applied_at='2026-11-08'))
            self.assertFalse(pristine_target(db))

    def test_cookie_ownership_data_is_not_mistaken_for_bootstrap(self):
        with self.database.engine.begin() as db:
            db.execute(cookie_bindings.insert().values(credential='a' * 64, scope='node-a'))
            self.assertFalse(pristine_target(db))
            self.assertEqual(db.execute(select(cookie_bindings.c.scope)).scalar_one(), 'node-a')
