"""Container bootstrap must preserve credentials and persistent user settings."""
import importlib.util
import errno
import hashlib
from http.client import RemoteDisconnected
from io import BytesIO
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from sqlalchemy.engine import make_url
import yaml

from webapp.control import ROOT, terminate_worker

spec = importlib.util.spec_from_file_location('docker_entrypoint', ROOT / 'docker/entrypoint.py')
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)

installer_spec = importlib.util.spec_from_file_location('docker_install_mihomo', ROOT / 'docker/install_mihomo.py')
installer = importlib.util.module_from_spec(installer_spec)
installer_spec.loader.exec_module(installer)

desktop_spec = importlib.util.spec_from_file_location('docker_browser_desktop', ROOT / 'docker/browser_desktop.py')
desktop = importlib.util.module_from_spec(desktop_spec)
desktop_spec.loader.exec_module(desktop)


class BrowserDesktopTests(unittest.TestCase):
    def setUp(self):
        # This Linux-only service is also unit-tested on Windows Python.
        unix = patch.object(desktop.socket, 'AF_UNIX', 1, create=True)
        unix.start()
        self.addCleanup(unix.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_desktop_logs_are_visible_before_the_child_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'desktop.log'
            child = desktop.start_logged([sys.executable, '-u', '-c',
                "import time; print('desktop ready', flush=True); time.sleep(10)"], log)
            try:
                deadline = time.monotonic() + 3
                while b'desktop ready' not in log.read_bytes() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIn(b'desktop ready', log.read_bytes())
                self.assertIsNone(child.poll())
            finally:
                child.terminate()
                child.wait(timeout=3)

    def test_restart_removes_stale_display_files_even_when_lock_pid_was_reused(self):
        for owner in ('gone', 'reused', 'no-lock'):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                endpoint = directory / '.X11-unix' / 'X99'
                endpoint.parent.mkdir()
                endpoint.touch()
                lock = directory / '.X99-lock'
                if owner != 'no-lock':
                    lock.write_text('       123\n', encoding='ascii')
                process = MagicMock()
                process.cmdline.return_value = ['/usr/local/bin/python', 'app.py']
                with patch.object(desktop.socket, 'socket') as connect, \
                        patch.object(desktop.psutil, 'Process', return_value=process) as lookup:
                    connect.return_value.__enter__.return_value.connect.side_effect = ConnectionRefusedError(
                        errno.ECONNREFUSED, 'stale socket')
                    if owner == 'gone':
                        lookup.side_effect = desktop.psutil.NoSuchProcess(123)
                    desktop.prepare_display(directory)
                self.assertFalse(endpoint.exists())
                self.assertFalse(lock.exists())

    def test_active_socket_is_preserved_and_no_process_is_started(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            lock = directory / '.X99-lock'
            lock.write_text('123', encoding='ascii')
            with patch.object(desktop.socket, 'socket'), patch.object(desktop, 'start_logged') as launch, \
                    patch.object(desktop, 'Path', return_value=directory):
                with self.assertRaisesRegex(OSError, 'already in use'):
                    desktop.start_desktop()
            launch.assert_not_called()
            self.assertEqual(lock.read_text(), '123')

    def test_live_or_unverifiable_lock_owner_is_preserved(self):
        for owner in ('xserver', 'unknown', 'denied', 'invalid'):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                lock = directory / '.X99-lock'
                lock.write_text('invalid' if owner == 'invalid' else '123', encoding='ascii')
                with patch.object(desktop.socket, 'socket') as connect, \
                        patch.object(desktop.psutil, 'Process') as lookup:
                    connect.return_value.__enter__.return_value.connect.side_effect = FileNotFoundError(
                        errno.ENOENT, 'no socket')
                    lookup.return_value.cmdline.return_value = ['/usr/bin/Xvfb', ':99'] if owner == 'xserver' else []
                    if owner == 'denied':
                        lookup.side_effect = desktop.psutil.AccessDenied(123)
                    with self.assertRaises(desktop.BrowserDesktopError):
                        desktop.prepare_display(directory)
                self.assertTrue(lock.exists())

    def test_unreachable_socket_is_not_removed_on_permission_error_or_timeout(self):
        for error in (PermissionError(errno.EACCES, 'denied'), TimeoutError('timeout')):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                endpoint = directory / '.X11-unix' / 'X99'
                endpoint.parent.mkdir()
                endpoint.touch()
                with patch.object(desktop.socket, 'socket') as connect:
                    connect.return_value.__enter__.return_value.connect.side_effect = error
                    with self.assertRaises(type(error)):
                        desktop.prepare_display(directory)
                self.assertTrue(endpoint.exists())

    def test_virtual_display_exit_identifies_component_and_reaps_child(self):
        display = MagicMock()
        display.poll.return_value = 1
        with patch.object(desktop, 'prepare_display'), \
                patch.object(desktop, 'start_logged', return_value=display) as launch, \
                patch.object(desktop.socket, 'socket') as connect, patch.dict(os.environ, {}, clear=True):
            connect.return_value.__enter__.return_value.connect.side_effect = ConnectionRefusedError()
            with self.assertRaisesRegex(desktop.BrowserDesktopError, r'Xvfb:.*exit code: 1'):
                desktop.start_desktop()
        self.assertEqual(launch.call_count, 1)
        display.wait.assert_called_once_with(timeout=3)

    def test_partial_startup_logs_xvfb_and_reaps_all_children_before_retry(self):
        display, vnc, novnc = MagicMock(), MagicMock(), MagicMock()
        for child in (display, vnc, novnc):
            child.stdout = BytesIO(b'fixture desktop log\n')
        display.poll.return_value = None
        vnc.poll.return_value = 1
        novnc.poll.return_value = None
        display.wait.side_effect = [desktop.subprocess.TimeoutExpired('Xvfb', 3), 0]
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(desktop, 'prepare_display'), \
                patch.object(desktop, 'Path', return_value=Path(temporary)), \
                patch.object(desktop.socket, 'AF_UNIX', 1, create=True), \
                patch.object(desktop.socket, 'socket'), \
                patch.object(desktop.time, 'sleep'), \
                patch.object(desktop.subprocess, 'Popen', side_effect=[display, vnc, novnc]) as launch:
            with self.assertRaisesRegex(OSError, r'x11vnc: Browser desktop startup failed.*exit code: 1'):
                desktop.start_desktop()
            self.assertTrue((Path(temporary) / 'xvfb.log').is_file())
            self.assertIsNot(launch.call_args_list[0].kwargs['stderr'], desktop.subprocess.DEVNULL)
        display.terminate.assert_called_once()
        novnc.terminate.assert_called_once()
        vnc.terminate.assert_not_called()
        display.kill.assert_called_once()
        self.assertEqual(display.wait.call_count, 2)
        vnc.wait.assert_called_once_with(timeout=3)
        novnc.wait.assert_called_once_with(timeout=3)


class MihomoDownloadTests(unittest.TestCase):
    def test_disconnect_and_bad_checksum_fall_back_to_verified_source(self):
        archive = b'fixture verified archive'
        with patch.object(installer, 'urlopen', side_effect=[RemoteDisconnected('closed'),
                          BytesIO(b'<html>not a release</html>'), BytesIO(archive)]) as request, \
                patch('builtins.print'):
            result = installer.download_archive('fixture.gz', hashlib.sha256(archive).hexdigest())
        self.assertEqual(result, archive)
        self.assertEqual([call.args[0].full_url for call in request.call_args_list],
                         [f'{base}/{installer.VERSION}/fixture.gz' for base in installer.DOWNLOAD_BASES])
        self.assertTrue(all(call.kwargs['timeout'] == 30 for call in request.call_args_list))

    def test_custom_source_has_priority(self):
        archive = b'fixture archive'
        with patch.object(installer, 'urlopen', return_value=BytesIO(archive)) as request, patch('builtins.print'):
            installer.download_archive('fixture.gz', hashlib.sha256(archive).hexdigest(), 'https://example.org/releases/')
        self.assertEqual(request.call_args.args[0].full_url,
                         f'https://example.org/releases/{installer.VERSION}/fixture.gz')
        self.assertEqual(request.call_count, 1)

    def test_unavailable_sources_retry_then_fail_with_configuration_hint(self):
        with patch.object(installer, 'urlopen', side_effect=URLError('offline')) as request, \
                patch.object(installer.time, 'sleep') as sleep, patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'MIHOMO_DOWNLOAD_BASE_URL'):
                installer.download_archive('fixture.gz', 'unused')
        self.assertEqual(request.call_count, len(installer.DOWNLOAD_BASES) * 2)
        sleep.assert_called_once_with(2)

    def test_oversized_archives_are_rejected_even_with_matching_hash(self):
        archive = b'1234'
        with patch.object(installer, 'MAX_ARCHIVE_BYTES', 3), \
                patch.object(installer, 'urlopen', side_effect=lambda *args, **kwargs: BytesIO(archive)), \
                patch.object(installer.time, 'sleep'), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'all sources'):
                installer.download_archive('fixture.gz', hashlib.sha256(archive).hexdigest())


class DockerBootstrapTests(unittest.TestCase):
    def test_packaged_sources_initialize_without_host_checkout_or_database(self):
        """Exercise only the files shipped by Dockerfile COPY instructions."""
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            for line in (ROOT / 'Dockerfile').read_text(encoding='utf-8').splitlines():
                if not line.startswith('COPY ') or line.startswith('COPY --from='):
                    continue
                *sources, destination = shlex.split(line)[1:]
                # Only application copies relative to WORKDIR enter this check.
                if destination.startswith('/'):
                    continue
                for source in sources:
                    path = ROOT / source
                    target = staging / destination
                    if path.is_dir():
                        shutil.copytree(path, target, dirs_exist_ok=True,
                                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log'))
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if destination.endswith('/'):
                            target.mkdir(parents=True, exist_ok=True)
                            target /= path.name
                        shutil.copy2(path, target)
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_', 'PYTHONPATH'))}
            result = subprocess.run([sys.executable, str(staging / 'docker/check_startup.py')],
                                    cwd=staging, env=environment, capture_output=True,
                                    text=True, encoding='utf-8', timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Packaged application startup verified.', result.stdout)

    def test_empty_database_config_enters_setup_without_starting_browser(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(entrypoint, 'initialize_config') as initialize, \
                patch.object(entrypoint.os, 'umask'), patch.object(entrypoint.os, 'chdir'), \
                patch.object(entrypoint.os, 'execvpe') as execute, \
                patch.object(entrypoint.sys, 'argv', ['entrypoint.py', 'python', 'app.py']):
            entrypoint.main()
            self.assertNotIn('VIDEO_DATABASE_URL', execute.call_args.args[2])
            self.assertEqual(execute.call_args.args[:2], ('python', ['python', 'app.py']))
            initialize.assert_called_once_with('/data/config')

    def test_password_special_characters_survive_url_encoding(self):
        env = {'MYSQL_DATABASE': 'video_downloads', 'MYSQL_USER': 'video',
               'MYSQL_PASSWORD': 'p@ss:/?#%$ &+秘密'}
        parsed = make_url(entrypoint.database_url(env))
        self.assertEqual(parsed.password, env['MYSQL_PASSWORD'])
        self.assertEqual((parsed.host, parsed.port, parsed.drivername),
                         ('mysql', 3306, 'mysql+pymysql'))
        self.assertEqual(parsed.query['charset'], 'utf8mb4')
        self.assertNotIn(env['MYSQL_PASSWORD'], str(parsed))

    def test_missing_database_credentials_fail_instead_of_falling_back_to_sqlite(self):
        for missing in ('MYSQL_DATABASE', 'MYSQL_USER', 'MYSQL_PASSWORD'):
            env = dict(MYSQL_DATABASE='video_downloads', MYSQL_USER='video', MYSQL_PASSWORD='secret')
            env.pop(missing)
            with self.assertRaises(ValueError):
                entrypoint.database_url(env)

    def test_initialization_preserves_web_settings_cookie_and_archive_on_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'config'
            config = entrypoint.initialize_config(directory)
            defaults = yaml.safe_load(config.read_text(encoding='utf-8'))
            self.assertEqual(defaults['path'], '/data/videos')
            self.assertEqual(defaults['proxy_mode'], 'direct')
            defaults.update(worker_count=4, youtube_cookie='Cookies/youtube.txt')
            config.write_text(yaml.safe_dump(defaults), encoding='utf-8')
            cookie = directory / 'Cookies/youtube.txt'
            cookie.write_text('uploaded-cookie', encoding='utf-8')
            archive = directory / 'videos.txt'
            archive.write_text('youtube downloaded-id', encoding='utf-8')
            saved = config.read_bytes()
            entrypoint.initialize_config(directory)
            self.assertEqual(config.read_bytes(), saved)
            self.assertEqual(cookie.read_text(encoding='utf-8'), 'uploaded-cookie')
            self.assertEqual(archive.read_text(encoding='utf-8'), 'youtube downloaded-id')
            self.assertFalse((directory / '.docker-config.tmp').exists())

    def test_stop_terminates_media_children_and_preserves_platform_mihomo(self):
        worker, proxy, media = MagicMock(), MagicMock(), MagicMock()
        worker.cmdline.return_value = ['python', '-u', str(ROOT / 'web_worker.py'), '--state', str(ROOT / '.cache/web')]
        worker.cwd.return_value = str(ROOT)
        worker.children.return_value = [proxy, media]
        binary = ROOT / '.tools/mihomo' / ('mihomo.exe' if os.name == 'nt' else 'mihomo')
        proxy.exe.return_value = str(binary)
        media.exe.return_value = str(ROOT / 'ffmpeg')
        with patch('webapp.control.psutil.Process', return_value=worker), \
                patch('webapp.control.psutil.wait_procs', return_value=([], [])):
            self.assertTrue(terminate_worker(f'{socket.gethostname()}:123:test', ROOT / '.cache/web'))
        worker.kill.assert_called_once()
        media.kill.assert_called_once()
        proxy.kill.assert_not_called()

