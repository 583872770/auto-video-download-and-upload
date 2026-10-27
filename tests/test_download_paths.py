"""Deep job directories leave room for yt-dlp sidecars and temporary names."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import yt_dlp

from Utils.runtime import configure_runtime
from webapp.downloader import build_ydl_options

VIDEO_ID = 'zINaJUSzm28'
TITLE = ('Bushcraft SURVIVAL Shelter. Building a Secret Dugout UNDER FALLEN TREE. '
         'Cooking Camping')


class DownloadPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'path': 'videos', 'dir': str(Path('jobs') / ('a' * 32) /
            ('b' * 32) / 'c057bac00a8c'), 'shard_downloads': True, 'proxy_mode': 'direct'}

    def options(self, config=None):
        # Exercise Windows' budget on every CI platform. Only option building
        # uses the override; file operations run on the actual host platform.
        with patch('webapp.downloader.sys.platform', 'win32'):
            return build_ydl_options(config or self.config, self.root)

    def test_real_metadata_write_in_deep_job_directory_preserves_full_title(self):
        for title in (TITLE, '野外生存与露营' * 30):
            with self.subTest(title=title):
                options = self.options()
                options.update(skip_download=True, writethumbnail=False, download_archive=None,
                               quiet=True, no_warnings=True, overwrites=True)
                info = dict(id=VIDEO_ID, title=title, ext='mp4', url='https://example.test/video.mp4',
                            extractor='youtube', extractor_key='Youtube')
                with yt_dlp.YoutubeDL(options) as downloader:
                    result = downloader.process_ie_result(info)
                    target = Path(downloader.prepare_filename(result)).with_suffix('.info.json')
                self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['title'], title)
                self.assertIn(VIDEO_ID, target.name)
                # yt-dlp creates a random eight-character temporary name plus
                # a dot and .tmp. Leave room for format and fragment suffixes too.
                self.assertLess(len(str(target).encode('utf-16-le')) // 2 + 13, 240)
                fragment = target.with_suffix('').with_suffix('.f401.mp4.part-Frag123456789')
                self.assertLess(len(str(fragment).encode('utf-16-le')) // 2, 260)

    def test_short_directories_keep_the_existing_filename_template(self):
        options = self.options({'path': 'videos', 'proxy_mode': 'direct'})
        self.assertIn('%(title).120B [%(id)s].%(ext)s', options['outtmpl'])

    def test_unsharded_and_unicode_directories_also_allow_atomic_sidecars(self):
        config = {**self.config, 'shard_downloads': False,
                  'dir': str(Path(self.config['dir']) / '中文目录')}
        with yt_dlp.YoutubeDL(self.options(config)) as downloader:
            filename = downloader.prepare_filename({'id': VIDEO_ID, 'title': TITLE * 3, 'ext':'mp4'})
        self.assertLess(len(str(Path(filename).with_suffix('.info.json')).encode('utf-16-le')) // 2 + 13, 240)

    def test_directory_that_cannot_fit_required_suffixes_has_an_actionable_error(self):
        with self.assertRaisesRegex(ValueError, '缩短素材目录'):
            self.options({**self.config, 'dir': self.config['dir'] + '/' + 'x' * 100})

    def test_real_video_audio_merge_thumbnail_and_metadata_in_deep_directory(self):
        configure_runtime()
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('FFmpeg required for the local merge fixture')
        source = self.root / 'source'
        source.mkdir()
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'color=c=black:s=64x64:d=0.3', '-c:v', 'libx264', str(source / 'video.mp4')], check=True)
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'anullsrc=r=44100:cl=mono', '-t', '0.3', '-c:a', 'aac', str(source / 'audio.m4a')], check=True)
        from PIL import Image
        Image.new('RGB', (16, 16)).save(source / 'cover.jpg')

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(source)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host = f'http://127.0.0.1:{server.server_port}'
        info = dict(id=VIDEO_ID, title=TITLE, extractor='youtube', extractor_key='Youtube',
                    thumbnail=host + '/cover.jpg', formats=[
                        dict(format_id='137', url=host + '/video.mp4', ext='mp4',
                             vcodec='avc1', acodec='none', width=64, height=64),
                        dict(format_id='140', url=host + '/audio.m4a', ext='m4a',
                             vcodec='none', acodec='mp4a', abr=128)])
        options = self.options()
        completed = []
        options.update(quiet=True, no_warnings=True, download_archive=None, post_hooks=[completed.append])
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.process_ie_result(info)
        self.assertEqual(len(completed), 1)
        video = Path(completed[0])
        self.assertGreater(video.stat().st_size, 0)
        self.assertTrue(video.with_suffix('.jpg').is_file())
        self.assertEqual(json.loads(video.with_suffix('.info.json').read_text(encoding='utf-8'))['title'], TITLE)
        self.assertFalse(list(video.parent.glob('*.f137.mp4')))
        self.assertFalse(list(video.parent.glob('*.f140.m4a')))


if __name__ == '__main__':
    unittest.main()
