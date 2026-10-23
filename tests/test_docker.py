"""Container bootstrap must preserve credentials and persistent user settings."""
import importlib.util
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url
import yaml

from webapp.control import ROOT, terminate_worker

spec = importlib.util.spec_from_file_location('docker_entrypoint', ROOT / 'docker/entrypoint.py')
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class DockerBootstrapTests(unittest.TestCase):
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
