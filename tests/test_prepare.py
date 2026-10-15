from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from PIL import Image
from main import main, write_prepared_manifest, getFileNames, find_cover
from Utils.paths import io_path
from Utils.runtime import configure_runtime
from Utils.split_video import split_video_ffmpeg, get_video_duration

configure_runtime()


class PrepareTests(unittest.TestCase):
    def test_prepare_writes_media_and_manifest_without_any_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'video.mp4').write_bytes(b'fixture')
            Image.new('RGB', (30, 20)).save(root / 'video.png')
            config = root / 'config.yaml'
            config.write_text('path: .\nplatforms: [kuaishou]\ntime_list: [8, 11, 14]\n', encoding='utf-8')
            def split(source, destination, *args):
                Path(destination).mkdir()
                part = Path(destination) / 'part.mp4'
                part.write_bytes(b'prepared')
                return [part]
            with patch('main.get_video_duration', return_value=100):
                with patch('main.split_video_ffmpeg', side_effect=split):
                    with patch('main.publish_plan') as publish, redirect_stdout(io.StringIO()):
                        self.assertEqual(main(['--config', str(config), '--prepare']), 0)
                        publish.assert_not_called()
            state = json.loads((root / 'prepared-jobs.json').read_text(encoding='utf-8'))
            self.assertEqual(state['status'], 'prepared')
            self.assertEqual(state['platforms'], ['kuaishou'])
            self.assertEqual(len(state['jobs']), 1)
            self.assertTrue(Path(state['jobs'][0]['file']).is_file())
            with Image.open(state['jobs'][0]['covers']['kuaishou']) as cover:
                self.assertEqual(cover.size, (1280, 960))
            self.assertFalse((root / 'Cookies').exists())

    def test_bad_job_count_cannot_replace_previous_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'prepared-jobs.json'
            manifest.write_text('previous valid manifest', encoding='utf-8')
            with self.assertRaises(ValueError):
                write_prepared_manifest([], [datetime.now()], {'path': '.'}, root, ['douyin'])
            self.assertEqual(manifest.read_text(encoding='utf-8'), 'previous valid manifest')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
class LongPathTests(unittest.TestCase):
    def test_real_media_beyond_windows_max_path(self):
        parent = tempfile.gettempdir()
        if os.name == 'nt':
            parent = '\\\\?\\' + str(Path(parent).resolve())
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            root = Path(directory)
            nested = root / ('a' * 95) / ('b' * 95)
            nested.mkdir(parents=True)
            source = nested / 'long.title.v2.mp4'
            subprocess.run([
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-f', 'lavfi', '-i', 'testsrc=size=80x80:rate=10', '-t', '2',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source),
            ], check=True, capture_output=True)
            Image.new('RGB', (30, 20)).save(source.with_suffix('.png'))
            plain_source = str(source).removeprefix('\\\\?\\')
            plain_folder = str(nested / 'parts').removeprefix('\\\\?\\')
            parts = split_video_ffmpeg(plain_source, plain_folder, max_duration=1)
            self.assertEqual(len(parts), 2)
            for part in parts:
                self.assertTrue(part.is_file())
                self.assertGreater(get_video_duration(part), 0)
                subprocess.run(['ffmpeg', '-v', 'error', '-i', str(part), '-f', 'null', '-'],
                               check=True, capture_output=True)
            plain_root = str(nested).removeprefix('\\\\?\\')
            self.assertEqual(getFileNames(plain_root), ['long.title.v2.mp4'])
            self.assertTrue(find_cover(plain_source).is_file())
