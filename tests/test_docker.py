"""Container bootstrap must preserve credentials and persistent user settings."""
import importlib.util
import hashlib
from http.client import RemoteDisconnected
from io import BytesIO
import os
from pathlib import Path
import socket
import tempfile
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
    def test_partial_startup_logs_xvfb_and_reaps_all_children_before_retry(self):
        display, vnc, novnc = MagicMock(), MagicMock(), MagicMock()
        display.poll.return_value = None
        vnc.poll.return_value = 1
        novnc.poll.return_value = None
        display.wait.side_effect = [desktop.subprocess.TimeoutExpired('Xvfb', 3), 0]
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(desktop, 'Path', return_value=Path(temporary)), \
                patch.object(desktop.socket, 'AF_UNIX', 1, create=True), \
                patch.object(desktop.socket, 'socket'), \
                patch.object(desktop.time, 'sleep'), \
                patch.object(desktop.subprocess, 'Popen', side_effect=[display, vnc, novnc]) as launch:
            with self.assertRaisesRegex(OSError, 'Browser desktop startup failed'):
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

