import importlib
import asyncio
from contextlib import redirect_stdout
import io
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from Utils.get_schedule_time import generate_schedule_within_7_days
from Utils.runtime import configure_runtime

configure_runtime()


class ScheduleTests(unittest.TestCase):
    def test_times_stay_within_seven_days_and_never_become_immediate(self):
        now = datetime(2026, 10, 14, 7, 30)
        with patch('Utils.get_schedule_time.datetime') as clock:
            clock.now.return_value = now
            clock.combine.side_effect = datetime.combine
            clock.min = datetime.min
            slots = generate_schedule_within_7_days(8, 11, 14)
        self.assertTrue(slots)
        self.assertTrue(all(isinstance(slot, datetime) for slot in slots))
        self.assertTrue(all(now + timedelta(hours=2) <= slot < now + timedelta(days=7)
                            for slot in slots))

    def test_boundary_sorted_deduplicated(self):
        now = datetime(2026, 10, 14, 6)
        slots = generate_schedule_within_7_days(11, 8, 8, now=now)
        self.assertEqual(slots[0], now + timedelta(hours=2))
        self.assertEqual(slots, sorted(set(slots)))
        self.assertTrue(all(slot < now + timedelta(days=7) for slot in slots))

    def test_invalid_hours(self):
        for hours in ((), (24,), (-1,), (True,), ('8',)):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                generate_schedule_within_7_days(*hours)


class FileTests(unittest.TestCase):
    def test_only_video_files_including_uppercase_extensions(self):
        from main import getFileNames
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'episode.MP4').touch()
            (root / 'notes.txt').touch()
            (root / 'folder.mp4').mkdir()
            self.assertEqual(getFileNames(root), ['episode.MP4'])

    def test_downloader_import_does_not_start_network_work(self):
        with patch('yt_dlp.YoutubeDL') as downloader:
            importlib.reload(importlib.import_module('Download'))
            downloader.assert_not_called()

    def test_cover_preserves_periods_and_accepts_jpeg(self):
        from main import find_cover
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / 'episode.v2.MP4'
            cover = video.with_suffix('.JPG')
            cover.touch()
            self.assertEqual(find_cover(video), cover)

    def test_config_paths_do_not_depend_on_working_directory(self):
        from Utils.config import load_config, video_directory
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / 'config.yaml'
            config_file.write_text('path: videos\ndir: series\n', encoding='utf-8')
            config, base = load_config(config_file)
            self.assertEqual(video_directory(config, base), root / 'videos' / 'series')

    def test_exhausted_schedule_fails_before_preparing_or_uploading(self):
        from main import build_plan
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'video.mp4').touch()
            (root / 'video.png').touch()
            with patch('main.get_video_duration', return_value=180 * 1000):
                with self.assertRaisesRegex(ValueError, '1000'):
                    build_plan({'path': '.', 'time_list': [8]}, root)
            self.assertEqual(len(list(root.iterdir())), 2)


class DownloadTests(unittest.TestCase):
    def test_empty_proxy_explicitly_disables_inherited_proxy(self):
        from Download import build_ydl_options
        self.assertEqual(build_ydl_options({'proxy': ''}, Path('.'))['proxy'], '')
    def test_python_api_options_and_shared_output_path(self):
        from Download import build_ydl_options
        from yt_dlp import YoutubeDL
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = build_ydl_options({'path': 'media', 'dir': 'batch'}, root)
            self.assertEqual(Path(options['outtmpl']).parent, root / 'media' / 'batch')
            # Instantiate the real library to check API compatibility without networking.
            with YoutubeDL({**options, 'quiet': True}) as downloader:
                self.assertEqual(downloader.params['concurrent_fragment_downloads'], 16)
                self.assertEqual(downloader.params['http_chunk_size'], 10485760)
                self.assertIs(downloader.params['writethumbnail'], True)

    def test_cli_uses_configured_url_and_propagates_failure_code(self):
        from Download import main
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yaml'
            config.write_text('url: https://example.com/video.mp4\n', encoding='utf-8')
            with patch('yt_dlp.YoutubeDL') as factory:
                downloader = factory.return_value.__enter__.return_value
                downloader.download.return_value = 1
                self.assertEqual(main(['--config', str(config), '--simulate']), 1)
                downloader.download.assert_called_once_with(['https://example.com/video.mp4'])
                self.assertTrue(factory.call_args.args[0]['simulate'])


class SplitTests(unittest.TestCase):
    def test_invalid_durations(self):
        from Utils.split_video import segment_count
        for duration, maximum in ((0, 180), (float('nan'), 180), (10, 0), (10, -1)):
            with self.subTest(duration=duration, maximum=maximum), self.assertRaises(ValueError):
                segment_count(duration, maximum)

    def test_failed_split_does_not_expose_partial_parts(self):
        from Utils.split_video import split_video_ffmpeg
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def fail_on_second_segment(command, **kwargs):
                output = Path(command[-1])
                output.write_bytes(b'partial media')
                calls.append(output)
                if len(calls) == 2:
                    raise subprocess.CalledProcessError(1, command)
            with patch('Utils.split_video.get_video_duration', return_value=400):
                with patch('Utils.split_video.subprocess.run', side_effect=fail_on_second_segment):
                    with self.assertRaises(subprocess.CalledProcessError):
                        split_video_ffmpeg(root / 'video.mp4', root / 'parts')
            self.assertEqual(list((root / 'parts').iterdir()), [])


class UploaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_uploaders_cancel_at_timeout(self):
        from main import PLATFORMS
        async def stall(*args):
            await asyncio.sleep(10)
        for module_name, class_name in PLATFORMS.values():
            with self.subTest(platform=class_name):
                module = importlib.import_module(module_name)
                app = getattr(module, class_name)('title', 'video', 'cover', [], 0, 'cookie')
                app.upload_timeout_seconds = 0.01
                app.upload = AsyncMock(side_effect=stall)
                with patch.object(module, 'async_playwright') as driver:
                    driver.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
                    driver.return_value.__aexit__ = AsyncMock(return_value=False)
                    with self.assertRaises(TimeoutError):
                        await app.main()
                    driver.return_value.__aexit__.assert_awaited_once()

    async def test_douyin_auth_has_no_missing_init_script(self):
        from Uploaders import DouyinUpload
        await self.check_auth_success_and_timeout(DouyinUpload)

    async def check_auth_success_and_timeout(self, module):
        page = AsyncMock()
        from Uploaders.browser import UPLOAD_URLS
        platform = {'DouyinUpload': 'douyin', 'KuaishouUpload': 'kuaishou', 'XiguaUpload': 'xigua'}[module.__name__.split('.')[-1]]
        page.url = UPLOAD_URLS[platform]
        upload_input = MagicMock()
        upload_input.first = upload_input
        upload_input.wait_for = AsyncMock()
        page.locator = MagicMock(return_value=upload_input)
        context = AsyncMock()
        context.new_page.return_value = page
        browser = AsyncMock()
        browser.new_context.return_value = context
        driver = MagicMock()
        driver.chromium.launch = AsyncMock(return_value=browser)
        with tempfile.TemporaryDirectory() as directory:
            cookie = Path(directory) / 'cookie.json'
            cookie.write_text('{"cookies": [], "origins": []}', encoding='utf-8')
            with patch.object(module, 'async_playwright') as factory:
                factory.return_value.__aenter__ = AsyncMock(return_value=driver)
                factory.return_value.__aexit__ = AsyncMock(return_value=False)
                self.assertTrue(await module.cookie_auth(cookie))
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError
                upload_input.wait_for.side_effect = PlaywrightTimeoutError('expired')
                self.assertFalse(await module.cookie_auth(cookie))
            self.assertEqual(context.close.await_count, 2)
            self.assertEqual(browser.close.await_count, 2)

    async def test_xigua_auth_success_and_timeout(self):
        from Uploaders import XiguaUpload
        await self.check_auth_success_and_timeout(XiguaUpload)

    async def test_kuaishou_auth_success_and_timeout(self):
        from Uploaders import KuaishouUpload
        await self.check_auth_success_and_timeout(KuaishouUpload)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
class MediaIntegrationTests(unittest.TestCase):
    def test_real_split_decode_cover_and_dry_run(self):
        from PIL import Image
        from main import main
        from Utils.process_cover import enhance_cover
        from Utils.split_video import get_video_duration, split_video_ffmpeg
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / '测试 episode.v2.mp4'
            subprocess.run([
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                '-t', '7', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', str(source),
            ], check=True, capture_output=True)
            cover = source.with_suffix('.png')
            Image.new('RGBA', (64, 32), (255, 0, 0, 128)).save(cover)
            with enhance_cover(cover, (864, 516)) as image:
                self.assertEqual(image.size, (864, 516))
                self.assertEqual(image.mode, 'RGB')
            # Repeat to prove existing output files never cause an interactive hang.
            for _ in range(2):
                segments = split_video_ffmpeg(source, root / 'parts', max_duration=3)
                self.assertEqual(len(segments), 3)
                durations = [get_video_duration(part) for part in segments]
                self.assertTrue(all(0 < duration <= 3.1 for duration in durations))
                self.assertAlmostEqual(sum(durations), 7, delta=0.5)
                for part in segments:
                    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(part), '-f', 'null', '-'],
                                   check=True, capture_output=True)
            config = root / 'config.yaml'
            config.write_text('path: .\nsegment_seconds: 3\ntime_list: [8, 11, 14]\n', encoding='utf-8')
            before = set(root.iterdir())
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['--config', str(config), '--dry-run']), 0)
            self.assertIn('3 个分段', output.getvalue())
            self.assertEqual(set(root.iterdir()), before)
            # Exercise real batch preparation and platform dispatch, replacing
            # only the external uploaders so no account or posting is involved.
            from main import build_plan, publish_plan, PLATFORMS
            cookie = root / 'cookie.json'
            cookie.write_text('{"cookies": [], "origins": []}', encoding='utf-8')
            settings = {'path': '.', 'segment_seconds': 3, 'time_list': [8, 11, 14],
                        'douyin_cookie': 'cookie.json', 'kuaishou_cookie': 'cookie.json'}
            plans, schedule = build_plan(settings, root)
            sent = []
            modules = {}
            for platform in ('douyin', 'kuaishou'):
                module_name, class_name = PLATFORMS[platform]
                module = MagicMock()
                def construct(title, file, cover, tags, slot, account, platform=platform):
                    self.assertGreater(Path(file).stat().st_size, 0)
                    with Image.open(cover) as image:
                        self.assertGreater(image.width, 0)
                    sent.append((platform, Path(file).name, slot))
                    return MagicMock(main=AsyncMock(return_value={'platform': platform, 'status': 'confirmed'}))
                getattr(module, class_name).side_effect = construct
                modules[module_name] = module
            with patch('main.importlib.import_module', side_effect=modules.__getitem__):
                asyncio.run(publish_plan(plans, schedule, settings, root, ['douyin', 'kuaishou']))
            self.assertEqual(len(sent), 6)
            self.assertEqual([platform for platform, _, _ in sent], ['douyin', 'kuaishou'] * 3)
            self.assertEqual([slot for _, _, slot in sent], [slot for slot in schedule for _ in range(2)])


if __name__ == '__main__':
    unittest.main()
