"""Validate both deployment plans with real Compose, without contacting Docker."""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ComposeDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('docker'):
            raise unittest.SkipTest('Docker Compose CLI is not installed')
        version = subprocess.run(['docker', 'compose', 'version'], capture_output=True, timeout=30)
        if version.returncode:
            raise unittest.SkipTest('Docker Compose CLI is not available')
        cls.variables = set()
        for mode in ('local', 'remote'):
            source = (ROOT / f'compose.{mode}.yaml').read_text(encoding='utf-8')
            cls.variables.update(re.findall(r'\$\{([A-Z][A-Z0-9_]*)', source))

    def render(self, mode, values=None, *, template=False, env_text='', valid=True):
        # Explicit env file and clean interpolation environment avoid reading the
        # developer's .env or exposing inherited production credentials.
        environment = {key: value for key, value in os.environ.items()
                       if key not in self.variables and not key.startswith('COMPOSE_')}
        environment.update(values or {})
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            environment['DOCKER_CONFIG'] = str(folder)
            config = folder / 'fixture.env'
            source = (ROOT / f'.env.{mode}.example').read_text(encoding='utf-8') if template else ''
            config.write_text(source + '\n' + env_text, encoding='utf-8')
            result = subprocess.run(
                ['docker', 'compose', '--env-file', str(config), '-f',
                 str(ROOT / f'compose.{mode}.yaml'), 'config', '--format', 'json'],
                cwd=ROOT, env=environment, capture_output=True, text=True,
                encoding='utf-8', timeout=45)
        if valid:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result.stderr

    @staticmethod
    def remote_values():
        return dict(MYSQL_HOST='mysql.example.test', MYSQL_PASSWORD='fixture-only-password',
                    REDIS_URL='redis://redis.example.test:6379/0')

    def test_local_services_wait_for_health_and_keep_existing_data_locations(self):
        config = self.render('local', dict(MYSQL_PASSWORD='fixture-only-password',
                                         MYSQL_HOST='ignored.example.test', MYSQL_PORT='9999',
                                         REDIS_URL='redis://ignored.example.test:9999/7'))
        self.assertEqual(config['name'], 'youtube-downloader-local')
        self.assertEqual(set(config['services']), {'app', 'mysql', 'redis'})
        app = config['services']['app']
        self.assertEqual(app['environment']['MYSQL_HOST'], 'mysql')
        self.assertEqual(app['environment']['MYSQL_PORT'], '3306')
        self.assertEqual(app['environment']['REDIS_URL'], 'redis://redis:6379/0')
        self.assertEqual(set(app['depends_on']), {'mysql', 'redis'})
        for name in ('mysql', 'redis'):
            self.assertEqual(app['depends_on'][name]['condition'], 'service_healthy')
            self.assertNotIn('ports', config['services'][name])
        for name in ('app_config', 'app_state', 'mysql_data'):
            self.assertEqual(config['volumes'][name]['name'], f'youtube-downloader-local_{name}')
        mounts = {item['target']: item['source'] for item in app['volumes']}
        self.assertEqual(mounts['/data/config'], 'app_config')
        self.assertEqual(mounts['/app/.cache/web'], 'app_state')
        self.assertEqual(config['services']['mysql']['volumes'][0]['target'], '/var/lib/mysql')

    def test_remote_runs_only_app_and_keeps_existing_volume_names(self):
        values = self.remote_values()
        config = self.render('remote', values)
        self.assertEqual(config['name'], 'youtube-downloader')
        self.assertEqual(set(config['services']), {'app'})
        self.assertNotIn('depends_on', config['services']['app'])
        for key, value in values.items():
            self.assertEqual(config['services']['app']['environment'][key], value)
        self.assertEqual(set(config['volumes']), {'app_config', 'app_state'})
        for name in config['volumes']:
            self.assertEqual(config['volumes'][name]['name'], f'youtube-downloader_{name}')

    def test_missing_connections_fail_before_startup(self):
        self.assertIn('MYSQL_PASSWORD', self.render('local', valid=False))
        for key in ('MYSQL_HOST', 'MYSQL_PASSWORD', 'REDIS_URL'):
            with self.subTest(key=key):
                values = self.remote_values()
                values[key] = ''
                self.assertIn(key, self.render('remote', values, valid=False))

    def test_templates_accept_credentials_without_corrupting_special_characters(self):
        password = 'fixture$p#ss:@/%'
        redis = 'rediss://cache:fixture%24p%23ss%40%2F@redis.example.test:6380/2'
        for mode in ('local', 'remote'):
            with self.subTest(mode=mode):
                self.render(mode, template=True, valid=False)
                text = f"MYSQL_PASSWORD='{password}'\n"
                if mode == 'remote':
                    text += f"MYSQL_HOST=mysql.example.test\nREDIS_URL='{redis}'\n"
                config = self.render(mode, template=True, env_text=text)
                # Compose escapes literal dollars in rendered output so that
                # `config` can itself be used as a Compose file without expanding them.
                serialized_password = password.replace('$', '$$')
                self.assertEqual(config['services']['app']['environment']['MYSQL_PASSWORD'], serialized_password)
                if mode == 'local':
                    self.assertEqual(config['services']['mysql']['environment']['MYSQL_PASSWORD'], serialized_password)
                else:
                    self.assertEqual(config['services']['app']['environment']['REDIS_URL'], redis)

    def test_application_runtime_settings_match_between_deployments(self):
        local = self.render('local', dict(MYSQL_PASSWORD='fixture-only-password'))['services']['app']
        remote = self.render('remote', self.remote_values())['services']['app']
        self.assertEqual(local['stop_grace_period'], '0s')
        self.assertEqual(remote['stop_grace_period'], '0s')
        self.assertEqual(local['build']['args']['WITH_YT5S_BROWSER'], '1')
        self.assertTrue(local['healthcheck']['test'])
        for port in local['ports']:
            self.assertEqual(port['host_ip'], '127.0.0.1')
        local = copy.deepcopy(local)
        local.pop('depends_on')
        for key in ('MYSQL_HOST', 'MYSQL_PORT', 'REDIS_URL'):
            local['environment'].pop(key)
            remote['environment'].pop(key)
        self.assertEqual(local, remote)


if __name__ == '__main__':
    unittest.main()
