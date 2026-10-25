"""Visual setup must work before MySQL exists and never expose stored secrets."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import ProxyHandler, build_opener

from flask import Flask, jsonify
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from pymysql.err import OperationalError as MySQLError
from werkzeug.test import Client
from werkzeug.wrappers import Response

from webapp.deployment import DeploymentGateway, test_mysql
from webapp.database import SchemaUpgradeInProgress
from webapp.deployment_errors import ClusterRoleConflict, explain_startup_error


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\n', encoding='utf-8')
        self.process_environment = {key: value for key, value in os.environ.items()
                                    if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.calls = []
        self.gateway = self.make_gateway()
        self.client = Client(self.gateway, Response)
        self.headers = self.unlock(self.client)

    def factory(self, *args, **kwargs):
        self.calls.append(kwargs)
        app = Flask('configured-test-app')
        app.add_url_rule('/', view_func=lambda: jsonify(role=kwargs['role']))
        return app

    def make_gateway(self, *, start=True):
        gateway = DeploymentGateway(self.config, self.root / 'state', load_environment=False, app_factory=self.factory)
        if start:
            gateway.start()
            if gateway.initialization:
                gateway.initialization.join(timeout=5)
                self.assertFalse(gateway.initialization.is_alive())
        return gateway

    def unlock(self, client):
        page = client.get('/setup')
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        headers = {'X-CSRF-Token': csrf}
        result = client.post('/api/deployment/unlock', json={'code': self.gateway.access_code}, headers=headers)
        self.assertEqual(result.status_code, 200)
        return headers

    def values(self, **extra):
        return {'role': 'manager', 'instance_id': '', 'instance_name': 'Test node', 'instance_url': '',
                'mysql_host': 'mysql.example.test', 'mysql_port': 3306, 'mysql_database': 'video_downloads',
                'mysql_user': 'video', 'mysql_password': 'p@ss:$ ${literal}秘密', 'web_hosts': 'localhost,127.0.0.1', **extra}

    def save(self, **extra):
        with patch('webapp.deployment.test_mysql'):
            response = self.client.post('/api/deployment/save', json=self.values(**extra), headers=self.headers)
        self.wait_for_initialization(self.gateway)
        return response

    def wait_for_initialization(self, gateway):
        if gateway.initialization:
            gateway.initialization.join(timeout=5)
            self.assertFalse(gateway.initialization.is_alive())

    def diagnostic(self):
        return self.client.get('/api/deployment')

    def test_unconfigured_service_only_serves_setup_without_creating_local_database(self):
        self.assertEqual(self.client.get('/').headers['Location'], '/setup')
        self.assertEqual(self.client.get('/health').json['status'], 'setup')
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.root.rglob('*.sqlite3')), [])

    def test_code_csrf_and_rate_limit_protect_deployment_details(self):
        client = Client(self.gateway, Response)
        self.assertEqual(client.get('/api/deployment').status_code, 403)
        self.assertEqual(client.post('/api/deployment/save', json=self.values()).status_code, 403)
        page = client.get('/setup')
        self.assertNotIn(self.gateway.access_code, page.text)
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        for _ in range(8):
            result = client.post('/api/deployment/unlock', json={'code': '无效'}, headers={'X-CSRF-Token': csrf})
            self.assertEqual(result.status_code, 403)
        self.assertEqual(client.post('/api/deployment/unlock', json={'code': 'wrong'}, headers={'X-CSRF-Token': csrf}).status_code, 429)

    def test_first_save_activates_and_restart_recovers_profile_without_parameters(self):
        response = self.save()
        self.assertEqual(response.status_code, 202, response.json)
        self.assertFalse(response.json['pending_restart'])
        self.assertEqual(self.client.get('/').json['role'], 'manager')
        self.assertEqual(len(self.calls), 1)
        url = make_url(self.calls[0]['database_url'])
        self.assertEqual(url.password, self.values()['mysql_password'])
        self.assertRegex(self.calls[0]['instance_id'], r'^[a-f0-9]{32}$')
        self.assertTrue(self.gateway.path.is_file())
        restarted = self.make_gateway()
        self.assertIsNotNone(restarted.active)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(make_url(self.calls[-1]['database_url']), url)
        self.assertEqual(restarted.access_code, self.gateway.access_code)
        self.assertEqual(self.calls[0]['instance_id'], self.calls[-1]['instance_id'])

    def test_saved_configuration_overrides_old_environment(self):
        self.assertEqual(self.save().status_code, 202)
        with patch.dict(os.environ, {'MYSQL_HOST': 'old.example.test', 'MYSQL_PASSWORD': 'old-password',
                                     'VIDEO_SERVICE_ROLE': 'all'}):
            restarted = self.make_gateway()
        self.assertEqual(restarted.profile['role'], 'manager')
        self.assertEqual(restarted.profile['mysql_host'], 'mysql.example.test')

    def test_password_never_returns_and_blank_retains_existing_value(self):
        self.save()
        result = self.client.get('/api/deployment')
        self.assertTrue(result.json['password_saved'])
        self.assertNotIn('mysql_password', result.json['config'])
        self.assertNotIn(self.values()['mysql_password'], result.text)
        response = self.save(mysql_password='', instance_name='Changed name')
        self.assertEqual(response.status_code, 200, response.json)
        saved = json.loads(self.gateway.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['mysql_password'], self.values()['mysql_password'])
        self.assertEqual(saved['instance_name'], 'Changed name')

    def test_live_changes_are_saved_for_restart_without_replacing_running_application(self):
        self.save()
        active = self.gateway.active
        response = self.save(role='worker', instance_url='http://10.0.0.12:5000')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['pending_restart'])
        self.assertIs(self.gateway.active, active)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.client.get('/').json['role'], 'manager')
        self.assertEqual(self.client.get('/api/deployment').json['running_role'], 'manager')
        restarted = self.make_gateway()
        self.assertEqual(restarted.profile['role'], 'worker')
        self.assertEqual(self.calls[-1]['instance_url'], 'http://10.0.0.12:5000')

    def test_failed_database_test_keeps_previous_settings_and_does_not_activate(self):
        with patch('webapp.deployment.test_mysql', side_effect=ValueError('MySQL 连接失败')):
            result = self.client.post('/api/deployment/save', json=self.values(), headers=self.headers)
        self.assertEqual(result.status_code, 400)
        self.assertFalse(self.gateway.path.exists())
        self.assertIsNone(self.gateway.active)

    def test_setup_remains_available_after_application_initialization_failure(self):
        self.gateway.app_factory = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('private-password'))
        result = self.save()
        self.assertEqual(result.status_code, 202)
        self.assertNotIn('private-password', self.diagnostic().text)
        self.assertIsNone(self.gateway.active)
        self.assertEqual(self.client.get('/setup').status_code, 200)

    def test_upgrade_in_progress_shows_reason_and_next_step_without_generic_id_guess(self):
        self.gateway.app_factory = lambda *args, **kwargs: (_ for _ in ()).throw(SchemaUpgradeInProgress())
        result = self.save()
        self.assertEqual(result.status_code, 202)
        status = self.client.get('/api/deployment').json
        self.assertIn('数据库仍在升级', status['error'])
        self.assertIn('不要反复提交', status['diagnostic']['remedy'])
        diagnostic = json.loads((self.gateway.directory / 'deployment-error.json').read_text(encoding='utf-8'))
        self.assertIn('数据库仍在升级', diagnostic['summary'])
        self.assertNotIn(self.values()['mysql_password'], json.dumps(diagnostic))

    def test_mysql_schema_timeout_has_error_code_and_redacts_driver_message_password(self):
        password = self.values()['mysql_password']
        failure = OperationalError('private SQL', {'password': password}, MySQLError(2013, 'timeout ' + password))
        self.gateway.app_factory = lambda *args, **kwargs: (_ for _ in ()).throw(failure)
        self.save()
        response = self.diagnostic()
        self.assertIn('升级等待超时', response.json['error'])
        self.assertEqual(response.json['diagnostic']['mysql_code'], 2013)
        self.assertNotIn(password, response.text)
        self.assertNotIn('private SQL', response.text)
        self.assertIn('[已隐藏]', response.json['diagnostic']['detail'])

    def test_permission_denied_has_specific_grant_guidance(self):
        failure = OperationalError('private SQL', {}, MySQLError(1142, 'ALTER command denied'))
        self.gateway.app_factory = lambda *args, **kwargs: (_ for _ in ()).throw(failure)
        self.save()
        response = self.diagnostic()
        self.assertIn('权限不足', response.json['error'])
        self.assertIn('CREATE、ALTER、INDEX', response.json['diagnostic']['remedy'])

    def test_unwritable_profile_never_starts_service(self):
        with patch('webapp.deployment.write_profile', side_effect=OSError('fixture')):
            response = self.save()
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.gateway.active)
        self.assertEqual(self.calls, [])

    def test_invalid_saved_profile_does_not_connect_to_another_database_from_environment(self):
        self.gateway.path.write_text('{broken', encoding='utf-8')
        with patch.dict(os.environ, {'MYSQL_HOST': 'old.example.test', 'MYSQL_PASSWORD': 'secret'}):
            restarted = self.make_gateway()
        self.assertIsNone(restarted.active)
        self.assertEqual(self.calls, [])
        self.assertIn('无法读取', restarted.startup_error)

    def test_blank_identity_uses_existing_state_instead_of_environment_override(self):
        (self.gateway.directory / 'instance.json').write_text(json.dumps({'id': 'saved-node'}), encoding='utf-8')
        with patch.dict(os.environ, {'VIDEO_INSTANCE_ID': 'other-node'}):
            self.assertEqual(self.save().status_code, 202)
        self.assertEqual(self.calls[-1]['instance_id'], 'saved-node')

    def cluster_fixture(self, role='worker'):
        (self.gateway.directory / 'fleet-runtime.json').write_text(json.dumps({
            'role': role, 'id': 'original-node', 'boot_id': 'private-boot-id'}), encoding='utf-8')
        (self.gateway.directory / 'instance.json').write_text(json.dumps({
            'id': 'old-generated-node', 'token': 'private-agent-token'}), encoding='utf-8')

    def test_cluster_cannot_save_single_mode_or_probe_database_or_start_desktop(self):
        self.cluster_fixture()
        with patch('webapp.deployment.test_mysql') as probe:
            result = self.client.post('/api/deployment/save', json=self.values(role='all'), headers=self.headers)
        self.assertEqual(result.status_code, 400)
        self.assertIn('原角色为“下载实例”', result.json['error'])
        probe.assert_not_called()
        self.assertFalse(self.gateway.path.exists())
        self.assertIsNone(self.gateway.active)
        with patch.dict(os.environ, {'YT5S_BROWSER_ENABLED': '1'}), \
                patch('webapp.deployment.importlib.util.spec_from_file_location') as desktop:
            with self.assertRaises(ClusterRoleConflict):
                self.gateway.activate(self.values(role='all'))
        desktop.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_rejecting_single_mode_preserves_previously_saved_cluster_profile(self):
        self.save()
        self.cluster_fixture('manager')
        original = self.gateway.path.read_bytes()
        self.assertEqual(self.save(role='all').status_code, 400)
        self.assertEqual(self.gateway.path.read_bytes(), original)
        self.assertEqual(self.gateway.running_role, 'manager')

    def test_cluster_metadata_is_public_without_tokens_and_restored_identity_is_reused(self):
        self.cluster_fixture()
        response = self.diagnostic()
        self.assertEqual(response.json['cluster_state'], {'role': 'worker', 'instance_id': 'original-node'})
        self.assertNotIn('private-agent-token', response.text)
        self.assertNotIn('private-boot-id', response.text)
        self.assertEqual(self.save(role='worker', instance_url='http://10.0.0.12:5000').status_code, 202)
        self.assertEqual(self.calls[-1]['instance_id'], 'original-node')

    def test_legacy_cluster_role_is_used_when_no_saved_deployment_or_explicit_role_exists(self):
        self.cluster_fixture()
        restarted = self.make_gateway()
        self.assertEqual(restarted.profile['role'], 'worker')

    def test_saved_single_mode_recovers_original_manager_and_persists_identity_across_restarts(self):
        self.cluster_fixture('manager')
        original = self.values(role='all', instance_id='incorrect-node')
        self.gateway.path.write_text(json.dumps(original), encoding='utf-8')
        runtime = (self.gateway.directory / 'fleet-runtime.json').read_bytes()
        identity = (self.gateway.directory / 'instance.json').read_bytes()
        with patch.dict(os.environ, {'YT5S_BROWSER_ENABLED': '1'}), \
                patch('webapp.deployment.importlib.util.spec_from_file_location') as desktop:
            restarted = self.make_gateway()
        self.assertIsNotNone(restarted.active)
        self.assertEqual(restarted.running_role, 'manager')
        self.assertEqual(self.calls[-1]['instance_id'], 'original-node')
        desktop.assert_not_called()
        saved = json.loads(restarted.path.read_text(encoding='utf-8'))
        self.assertEqual(saved, {**original, 'role': 'manager', 'instance_id': 'original-node'})
        self.assertEqual((self.gateway.directory / 'fleet-runtime.json').read_bytes(), runtime)
        self.assertEqual((self.gateway.directory / 'instance.json').read_bytes(), identity)
        again = self.make_gateway()
        self.assertEqual(again.running_role, 'manager')
        self.assertEqual(self.calls[-1]['instance_id'], 'original-node')

    def test_explicit_single_mode_override_is_rejected_instead_of_automatically_changed(self):
        self.cluster_fixture('manager')
        self.gateway.path.write_text(json.dumps(self.values(role='all')), encoding='utf-8')
        original = self.gateway.path.read_bytes()
        for override in ({'role': 'all'}, {'instance_id': 'explicit-node'}):
            with self.subTest(override=override):
                restarted = DeploymentGateway(self.config, self.gateway.directory, **override,
                                              load_environment=False, app_factory=self.factory)
                self.assertFalse(restarted.auto_start)
                self.assertIsNone(restarted.active)
                self.assertIsNotNone(restarted.failure)
                self.assertEqual(self.gateway.path.read_bytes(), original)
                self.assertEqual(self.calls, [])

    def test_saved_single_mode_without_cluster_identity_remains_single(self):
        self.assertEqual(self.save(role='all').status_code, 202)
        restarted = self.make_gateway()
        self.assertEqual(restarted.running_role, 'all')
        self.assertIsNone(restarted.cluster_state())

    def test_automatic_recovery_still_requires_valid_worker_address(self):
        self.cluster_fixture('worker')
        self.gateway.path.write_text(json.dumps(self.values(role='all')), encoding='utf-8')
        original = self.gateway.path.read_bytes()
        restarted = self.make_gateway()
        self.assertFalse(restarted.auto_start)
        self.assertIsNone(restarted.active)
        self.assertIn('恢复原实例配置', restarted.failure['remedy'])
        self.assertIn('实例地址', restarted.failure['remedy'])
        self.assertEqual(self.gateway.path.read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_automatic_recovery_reuses_valid_worker_address(self):
        self.cluster_fixture('worker')
        self.gateway.path.write_text(json.dumps(self.values(role='all', instance_url='http://10.0.0.12:5000')), encoding='utf-8')
        restarted = self.make_gateway()
        self.assertEqual(restarted.running_role, 'worker')
        self.assertEqual(self.calls[-1]['instance_id'], 'original-node')
        self.assertEqual(self.calls[-1]['instance_url'], 'http://10.0.0.12:5000')

    def test_recovery_write_failure_never_starts_service_or_overwrites_old_profile(self):
        self.cluster_fixture('manager')
        self.gateway.path.write_text(json.dumps(self.values(role='all')), encoding='utf-8')
        original = self.gateway.path.read_bytes()
        with patch('webapp.deployment.write_profile', side_effect=PermissionError('private-password')):
            restarted = self.make_gateway()
        self.assertFalse(restarted.auto_start)
        self.assertIsNone(restarted.active)
        self.assertIn('读写权限', restarted.startup_error)
        self.assertNotIn('private-password', restarted.startup_error)
        self.assertEqual(self.gateway.path.read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_confirmed_single_transition_is_saved_for_restart_and_not_automatically_reverted(self):
        self.save()
        self.cluster_fixture('manager')
        active = self.gateway.active
        result = self.save(role='all', single_transition=True)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['pending_restart'])
        self.assertIs(self.gateway.active, active)
        self.assertEqual(self.gateway.running_role, 'manager')
        self.assertEqual(self.diagnostic().json['single_conversion'], 'requested')
        self.assertTrue((self.gateway.directory / 'fleet-runtime.json').exists())
        restarted = self.make_gateway()
        self.assertEqual(restarted.running_role, 'all')
        self.assertEqual(restarted.profile['instance_id'], 'original-node')
        self.assertEqual(json.loads(restarted.path.read_text(encoding='utf-8'))['role'], 'all')

    def test_transition_does_not_allow_changing_database_at_the_same_time(self):
        self.save()
        self.cluster_fixture('manager')
        result = self.save(role='all', single_transition=True, mysql_host='another.example.test')
        self.assertEqual(result.status_code, 400)
        self.assertIn('原 MySQL', result.json['error'])
        self.assertFalse((self.gateway.directory / 'single-mode.json').exists())

    def test_pending_conversion_can_be_cancelled_by_saving_original_cluster_role(self):
        self.save()
        self.cluster_fixture('manager')
        self.assertEqual(self.save(role='all', single_transition=True).status_code, 200)
        self.assertTrue((self.gateway.directory / 'single-mode.json').exists())
        self.assertEqual(self.save(role='manager').status_code, 200)
        self.assertFalse((self.gateway.directory / 'single-mode.json').exists())
        self.assertTrue((self.gateway.directory / 'fleet-runtime.json').exists())

    def test_failed_transition_profile_write_rolls_back_new_request(self):
        self.save()
        self.cluster_fixture('manager')
        original = self.gateway.path.read_bytes()
        from webapp.deployment import write_profile

        def fail_profile(path, value):
            if path == self.gateway.path:
                raise PermissionError('fixture')
            return write_profile(path, value)

        with patch('webapp.deployment.write_profile', side_effect=fail_profile):
            result = self.save(role='all', single_transition=True)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.gateway.path.read_bytes(), original)
        self.assertFalse((self.gateway.directory / 'single-mode.json').exists())

    def test_desktop_failure_explains_log_locations_without_leaking_os_error(self):
        error = OSError('private-agent-token')
        diagnostic = explain_startup_error(error, '正在启动浏览器桌面环境…', self.values())
        self.assertEqual(diagnostic['summary'], '浏览器桌面环境启动失败。')
        self.assertIn('/tmp/xvfb.log', diagnostic['remedy'])
        self.assertNotIn('private-agent-token', json.dumps(diagnostic))

    def test_database_probe_sanitizes_driver_failures_and_disposes_engine(self):
        for failure in (RuntimeError, ValueError):
            with self.subTest(failure=failure):
                engine = MagicMock()
                engine.connect.side_effect = failure('private-password')
                with patch('webapp.deployment.create_engine', return_value=engine):
                    with self.assertRaisesRegex(ValueError, '^MySQL 连接失败') as raised:
                        test_mysql(self.values())
                self.assertNotIn('private-password', str(raised.exception))
                engine.dispose.assert_called_once()

    def test_database_probe_accepts_mysql_57_and_rejects_mariadb(self):
        for version, mariadb, valid in (((5, 7, 44), False, True), ((5, 6, 51), False, False), ((10, 6), True, False)):
            with self.subTest(version=version, mariadb=mariadb):
                engine = MagicMock()
                engine.dialect.server_version_info = version
                engine.dialect.is_mariadb = mariadb
                with patch('webapp.deployment.create_engine', return_value=engine):
                    if valid:
                        test_mysql(self.values())
                    else:
                        with self.assertRaisesRegex(ValueError, 'MySQL 5.7.9'):
                            test_mysql(self.values())
                engine.dispose.assert_called_once()

    def test_worker_address_and_ports_are_validated_before_database_access(self):
        for extra in ({'role': 'worker'}, {'mysql_port': True}, {'mysql_port': 65536},
                      {'instance_id': '../invalid'}, {'instance_url': 'http://user:password@host:5000'},
                      {'instance_url': 'http://0.0.0.0:5000'},
                      {'mysql_host': 'mysql://invalid'}):
            with self.subTest(extra=extra), patch('webapp.deployment.test_mysql') as probe:
                response = self.client.post('/api/deployment/test', json=self.values(**extra), headers=self.headers)
                self.assertEqual(response.status_code, 400)
                probe.assert_not_called()

    def test_concurrent_save_is_rejected_while_first_setup_initializes(self):
        client = Client(self.gateway, Response)
        headers = self.unlock(client)
        entered, release = threading.Event(), threading.Event()
        def factory(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.factory(*args, **kwargs)
        self.gateway.app_factory = factory
        with patch('webapp.deployment.test_mysql'), ThreadPoolExecutor(1) as pool:
            first = pool.submit(client.post, '/api/deployment/save', json=self.values(), headers=headers)
            try:
                self.assertTrue(entered.wait(3))
                accepted = first.result(timeout=1)
                self.assertEqual(accepted.status_code, 202)
                self.assertTrue(self.client.get('/api/deployment').json['starting'])
                second = self.client.post('/api/deployment/save', json=self.values(), headers=self.headers)
                self.assertEqual(second.status_code, 409)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).status_code, 202)
            self.wait_for_initialization(self.gateway)
        self.assertEqual(len(self.calls), 1)

    def test_slow_database_initialization_never_blocks_setup_or_allows_duplicate_start(self):
        self.gateway.path.write_text(json.dumps(self.values()), encoding='utf-8')
        gateway = self.make_gateway(start=False)
        self.assertIsNone(gateway.active)
        self.assertEqual(self.calls, [])
        entered, release = threading.Event(), threading.Event()

        def slow_factory(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.factory(*args, **kwargs)

        gateway.app_factory = slow_factory
        gateway.start()
        try:
            self.assertTrue(entered.wait(3))
            client = Client(gateway, Response)
            headers = self.unlock(client)
            self.assertEqual(client.get('/setup').status_code, 200)
            self.assertEqual(client.get('/health').json['status'], 'starting')
            status = client.get('/api/deployment').json
            self.assertTrue(status['starting'])
            self.assertIn('MySQL', status['stage'])
            self.assertFalse(status['running'])
            self.assertNotIn(self.values()['mysql_password'], json.dumps(status))
            self.assertEqual(client.post('/api/deployment/save', json=self.values(), headers=headers).status_code, 409)
            self.assertEqual(client.post('/api/deployment/test', json=self.values(), headers=headers).status_code, 409)
            gateway.start()
            self.assertEqual(self.calls, [])
        finally:
            release.set()
            gateway.initialization.join(timeout=5)
        self.assertFalse(gateway.starting)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(client.get('/').json['role'], 'manager')

    def test_background_database_failure_keeps_setup_available_for_correction(self):
        self.gateway.path.write_text(json.dumps(self.values()), encoding='utf-8')
        gateway = self.make_gateway(start=False)
        gateway.app_factory = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('private-password'))
        gateway.start()
        gateway.initialization.join(timeout=5)
        self.assertFalse(gateway.starting)
        self.assertIsNone(gateway.active)
        client = Client(gateway, Response)
        headers = self.unlock(client)
        status = client.get('/api/deployment')
        self.assertIn('初始化失败', status.json['error'])
        self.assertNotIn('private-password', status.text)
        gateway.app_factory = self.factory
        with patch('webapp.deployment.test_mysql'):
            response = client.post('/api/deployment/save', json=self.values(), headers=headers)
        self.assertEqual(response.status_code, 202, response.json)
        self.wait_for_initialization(gateway)
        self.assertIsNotNone(gateway.active)

    def test_cli_opens_setup_before_mysql_handshake_finishes(self):
        """A real CLI process must serve HTTP even when a MySQL socket stalls."""
        accepted, release = threading.Event(), threading.Event()
        with socket.socket() as mysql, socket.socket() as port_probe:
            mysql.bind(('127.0.0.1', 0))
            mysql.listen()
            mysql.settimeout(12)
            port_probe.bind(('127.0.0.1', 0))
            port = port_probe.getsockname()[1]
            port_probe.close()

            def stalled_mysql():
                try:
                    connection, _ = mysql.accept()
                    with connection:
                        accepted.set()
                        release.wait(12)
                except OSError:
                    pass

            fake_server = threading.Thread(target=stalled_mysql, daemon=True)
            fake_server.start()
            environment = dict(self.process_environment, PYTHONUTF8='1', MYSQL_HOST='127.0.0.1',
                               MYSQL_PORT=str(mysql.getsockname()[1]), MYSQL_DATABASE='fixture',
                               MYSQL_USER='fixture', MYSQL_PASSWORD='fixture-password')
            env_file = self.root / 'empty.env'
            env_file.write_text('', encoding='utf-8')
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve().parents[1] / 'app.py'),
                '--port', str(port), '--config', str(self.config), '--state', str(self.root / 'cli-state'),
                '--env-file', str(env_file), '--role', 'all'], env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
            try:
                opener = build_opener(ProxyHandler({}))
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with opener.open(f'http://127.0.0.1:{port}/setup', timeout=.5) as response:
                            self.assertEqual(response.status, 200)
                        break
                    except OSError:
                        if process.poll() is not None:
                            self.fail(process.communicate(timeout=2)[0].decode('utf-8', errors='replace'))
                        if time.monotonic() > deadline:
                            self.fail('Setup listener did not start before MySQL initialized')
                        time.sleep(.05)
                self.assertTrue(accepted.wait(3))
                with opener.open(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                    self.assertEqual(json.load(response)['status'], 'starting')
            finally:
                if process.poll() is None:
                    process.kill()
                logs = process.communicate(timeout=5)[0].decode('utf-8', errors='replace')
                release.set()
                mysql.close()
                fake_server.join(timeout=3)
            self.assertIn('部署设置访问码：', logs)
            self.assertLess(logs.index('部署设置访问码：'), logs.index('[服务启动]'))


if __name__ == '__main__':
    unittest.main()
