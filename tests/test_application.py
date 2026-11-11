"""Application lifecycle contracts across restarts and isolated instances."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import create_app


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
        isolated = patch.dict(os.environ, environment, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)

    def application(self, name, *, start_worker=False):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        config = directory / 'config.yaml'
        if not config.exists():
            config.write_text('path: videos\nproxy_mode: direct\nworker_count: 2\n',
                              encoding='utf-8')
        app = create_app(config, directory / 'state', start_worker=start_worker)
        app.testing = True
        self.addCleanup(app.extensions['task_store'].db.engine.dispose)
        return app

    def test_browser_session_survives_restart_but_cannot_access_another_instance(self):
        first = self.application('first')
        client = first.test_client()
        self.assertEqual(client.get('/').status_code, 200)
        with client.session_transaction() as session:
            token = session['csrf']
        cookie = client.get_cookie(first.config['SESSION_COOKIE_NAME']).value

        restarted = self.application('first')
        client = restarted.test_client()
        client.set_cookie(restarted.config['SESSION_COOKIE_NAME'], cookie)
        self.assertEqual(client.put('/api/config', json={'worker_count': 3},
                                    headers={'X-CSRF-Token': token}).status_code, 200)
        self.assertEqual(first.config['SECRET_KEY'], restarted.config['SECRET_KEY'])

        other = self.application('other')
        client = other.test_client()
        client.set_cookie(other.config['SESSION_COOKIE_NAME'], cookie)
        self.assertEqual(client.put('/api/config', json={'worker_count': 4},
                                    headers={'X-CSRF-Token': token}).status_code, 403)
        self.assertNotEqual(first.config['SECRET_KEY'], other.config['SECRET_KEY'])

    def test_settings_are_reloaded_from_each_instances_own_file(self):
        first = self.application('first')
        other = self.application('other')
        config = self.root / 'first' / 'config.yaml'
        config.write_text('path: updated\nworker_count: 5\n', encoding='utf-8')
        settings, base = first.extensions['settings_func']()
        self.assertEqual((settings['path'], settings['worker_count']), ('updated', 5))
        self.assertEqual(base, config.parent)
        self.assertEqual(first.extensions['disk_config']()['worker_count'], 5)
        self.assertEqual(other.extensions['settings_func']()[0]['worker_count'], 2)

    def test_worker_switch_preserves_settings_and_resume_callback(self):
        with patch('webapp.application.TaskRunner') as runner_type, \
                patch('webapp.application.threading.Thread') as thread_type:
            disabled = self.application('disabled')
            self.assertNotIn('task_runner', disabled.extensions)
            runner_type.assert_not_called()
            thread_type.assert_not_called()

            enabled = self.application('enabled', start_worker=True)
            runner = runner_type.return_value
            self.assertIs(enabled.extensions['task_runner'], runner)
            runner.start.assert_called_once_with()
            args, kwargs = runner_type.call_args
            self.assertIs(args[0], enabled.extensions['task_store'])
            self.assertEqual(args[1](), enabled.extensions['settings_func']()[0])
            self.assertEqual(kwargs['base'], self.root / 'enabled')
            self.assertTrue(callable(enabled.extensions['execution_control'].prepare_resume))
            thread_type.return_value.start.assert_called_once_with()

    def test_dashboard_releases_database_before_local_health_checks(self):
        from sqlalchemy import event
        from webapp.risk_diagnostics import local_health
        app = self.application('dashboard')
        engine = app.extensions['task_store'].db.engine
        active = set()

        def checkout(connection, record, proxy):
            active.add(id(record))

        def checkin(connection, record):
            active.discard(id(record))

        def inspect_health(*args, **kwargs):
            self.assertFalse(active, 'Local health inspection must not retain a database connection')
            return local_health(*args, **kwargs)

        event.listen(engine, 'checkout', checkout)
        event.listen(engine, 'checkin', checkin)
        self.addCleanup(event.remove, engine, 'checkout', checkout)
        self.addCleanup(event.remove, engine, 'checkin', checkin)
        with patch('webapp.risk_diagnostics.local_health', side_effect=inspect_health) as inspect:
            response = app.test_client().get('/api/status')
        self.assertEqual(response.status_code, 200)
        inspect.assert_called_once()
        self.assertIn('local_health', response.json['concurrency'])


if __name__ == '__main__':
    unittest.main()
