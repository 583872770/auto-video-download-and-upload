import asyncio
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from Uploaders.browser import (browser_session, confirm_publication, generate_login,
                               refresh_login_after_publish, validate_storage_state, wait_for_upload,
                               ensure_upload_origin, UPLOAD_URLS)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


def fake_browser():
    locator = MagicMock()
    locator.first = locator
    locator.nth.return_value = locator
    locator.locator.return_value = locator
    locator.get_by_role.return_value = locator
    locator.filter.return_value = locator
    for method in ('wait_for', 'click', 'hover', 'type', 'fill', 'press', 'set_input_files'):
        setattr(locator, method, AsyncMock())
    locator.count = AsyncMock(return_value=1)
    locator.is_visible = AsyncMock(return_value=False)
    locator.is_checked = AsyncMock(return_value=True)
    page = AsyncMock()
    page.url = 'https://example.com/manage'
    page.is_closed = MagicMock(return_value=False)
    page.locator = MagicMock(return_value=locator)
    page.get_by_role = MagicMock(return_value=locator)
    page.get_by_text = MagicMock(return_value=locator)
    page.eval_on_selector.return_value = 'semi-switch'
    page.keyboard = AsyncMock()
    context = AsyncMock()
    context.new_page.return_value = page
    context.storage_state.return_value = {'cookies': [], 'origins': []}
    browser = AsyncMock()
    browser.new_context.return_value = context
    driver = MagicMock()
    driver.chromium.launch = AsyncMock(return_value=browser)
    return driver, browser, context, page, locator


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_download_proxy_does_not_inherit_upload_proxy(self):
        driver, browser, context, page, locator = fake_browser()
        with patch.dict('os.environ', {'VIDEO_BROWSER_PROXY': 'http://127.0.0.1:21931'}):
            async with browser_session(driver, proxy_server='http://127.0.0.1:10808'):
                pass
            self.assertEqual(driver.chromium.launch.await_args.kwargs['proxy']['server'], 'http://127.0.0.1:10808')
            async with browser_session(driver, proxy_server=''):
                pass
            self.assertNotIn('proxy', driver.chromium.launch.await_args.kwargs)

    async def test_closes_browser_when_context_creation_fails(self):
        driver, browser, context, page, locator = fake_browser()
        browser.new_context.side_effect = RuntimeError('broken storage state')
        with self.assertRaisesRegex(RuntimeError, 'broken storage state'):
            async with browser_session(driver):
                self.fail('context should not open')
        browser.close.assert_awaited_once()

    async def test_closes_browser_even_if_context_close_fails(self):
        driver, browser, context, page, locator = fake_browser()
        context.close.side_effect = RuntimeError('context close failed')
        with self.assertRaisesRegex(RuntimeError, 'context close failed'):
            async with browser_session(driver):
                pass
        browser.close.assert_awaited_once()

    async def test_closes_both_on_cancellation(self):
        driver, browser, context, page, locator = fake_browser()
        with self.assertRaises(asyncio.CancelledError):
            async with browser_session(driver):
                raise asyncio.CancelledError()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_resuming_login_without_auth_does_not_overwrite_state(self):
        driver, browser, context, page, locator = fake_browser()
        page.url = UPLOAD_URLS['douyin']
        locator.wait_for.side_effect = PlaywrightTimeoutError('still on login screen')
        with tempfile.TemporaryDirectory() as directory:
            cookie = Path(directory) / 'cookie.json'
            cookie.write_text('previous login state', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, '未覆盖'):
                await generate_login(driver, 'douyin', cookie)
            self.assertEqual(cookie.read_text(encoding='utf-8'), 'previous login state')
        context.storage_state.assert_not_awaited()
        browser.close.assert_awaited_once()

    async def test_successful_login_saves_valid_json_and_closes(self):
        driver, browser, context, page, locator = fake_browser()
        page.url = UPLOAD_URLS['kuaishou']
        with tempfile.TemporaryDirectory() as directory:
            cookie = Path(directory) / 'accounts' / 'cookie.json'
            await generate_login(driver, 'kuaishou', cookie)
            self.assertEqual(validate_storage_state(cookie), {'cookies': [], 'origins': []})
            self.assertEqual(list(cookie.parent.glob('.login-*')), [])
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_publication_timeout_never_returns_success(self):
        driver, browser, context, page, locator = fake_browser()
        page.wait_for_url.side_effect = PlaywrightTimeoutError('no management page')
        locator.wait_for.side_effect = PlaywrightTimeoutError('no success message')
        for platform in ('douyin', 'kuaishou', 'xigua'):
            with self.subTest(platform=platform), self.assertRaisesRegex(RuntimeError, '先核对'):
                await confirm_publication(page, platform, timeout=10)

    async def test_cookie_refresh_failure_keeps_confirmed_publication(self):
        with patch('Uploaders.browser.save_storage_state', side_effect=PermissionError()):
            await refresh_login_after_publish(AsyncMock(), 'cookie.json')


class PlatformWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_uploaders_complete_flow_and_return_receipt(self):
        from main import PLATFORMS
        for platform, (module_name, class_name) in PLATFORMS.items():
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                driver, browser, context, page, locator = fake_browser()
                page.url = UPLOAD_URLS[platform]
                module = importlib.import_module(module_name)
                cookie = Path(directory) / 'cookie.json'
                app = getattr(module, class_name)('test', 'video', 'cover', [], 0, str(cookie))
                with patch.object(module.asyncio, 'sleep', new=AsyncMock()):
                    with patch.object(module, 'wait_for_upload', new=AsyncMock()):
                        receipt = await app.upload(driver)
                self.assertEqual(receipt['status'], 'confirmed')
                self.assertEqual(receipt['platform'], platform)
                self.assertTrue(cookie.is_file())
                context.close.assert_awaited_once()
                browser.close.assert_awaited_once()

    async def test_navigation_error_closes_all_platform_browsers(self):
        from main import PLATFORMS
        for platform, (module_name, class_name) in PLATFORMS.items():
            with self.subTest(platform=platform):
                driver, browser, context, page, locator = fake_browser()
                page.goto.side_effect = RuntimeError('network error')
                module = importlib.import_module(module_name)
                app = getattr(module, class_name)('test', 'video', 'cover', [], 0, 'cookie.json')
                with patch.object(module.asyncio, 'sleep', new=AsyncMock()):
                    with self.assertRaises(RuntimeError):
                        await app.upload(driver)
                context.close.assert_awaited_once()
                browser.close.assert_awaited_once()

    async def test_batch_rejects_invalid_settings_before_splitting(self):
        from main import publish_plan
        with patch('main.split_video_ffmpeg') as split:
            with self.assertRaises(ValueError):
                await publish_plan([], [], {'upload_timeout_seconds': -1}, Path('.'), ['douyin'])
            split.assert_not_called()

    async def test_batch_stops_on_unconfirmed_receipt(self):
        from main import VideoPlan, publish_plan
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cookie = root / 'cookie.json'
            cookie.write_text('{"cookies": [], "origins": []}', encoding='utf-8')
            config = {'douyin_cookie': 'cookie.json', 'kuaishou_cookie': 'cookie.json'}
            from PIL import Image
            cover = root / 'cover.png'
            Image.new('RGB', (20, 20)).save(cover)
            plan = VideoPlan(root / 'video.mp4', cover, 1)
            (root / 'video.mp4.parts').mkdir()
            module = MagicMock()
            uploader = module.DouYinVideo.return_value
            uploader.main = AsyncMock(return_value=None)
            with patch('main.split_video_ffmpeg', return_value=[root / 'part.mp4']):
                with patch('main.importlib.import_module', return_value=module) as imported:
                    with self.assertRaisesRegex(RuntimeError, '未返回发布成功确认'):
                        await publish_plan([plan], [datetime.now() + timedelta(days=1)],
                                           config, root, ['douyin', 'kuaishou'])
                    imported.assert_called_once_with('Uploaders.DouyinUpload')


class StorageStateTests(unittest.TestCase):
    def test_xigua_redirect_cannot_reuse_douyin_upload_form(self):
        page = MagicMock(url=UPLOAD_URLS['douyin'])
        with self.assertRaisesRegex(RuntimeError, '旧西瓜网页流程不可用'):
            ensure_upload_origin(page, 'xigua')

    def test_unrelated_origin_is_rejected(self):
        page = MagicMock(url='https://example.com/upload')
        with self.assertRaises(RuntimeError):
            ensure_upload_origin(page, 'kuaishou')

    def test_rejects_cookie_array_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie = Path(directory) / 'cookie.json'
            for content in ('[]', '{', '{"cookies": {}}'):
                cookie.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    validate_storage_state(cookie)


class KuaishouScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_schedule_stops_before_publish(self):
        from Uploaders.KuaishouUpload import KuaiShouVideo
        driver, browser, context, page, locator = fake_browser()
        locator.input_value = AsyncMock(return_value='')
        app = KuaiShouVideo('test', 'video', 'cover', [], 0, 'cookie.json')
        with self.assertRaisesRegex(RuntimeError, '未接受指定排期'):
            await app.set_schedule_time_kuaishou(page, datetime(2026, 10, 16, 12))

    async def test_accepted_schedule_is_verified(self):
        from Uploaders.KuaishouUpload import KuaiShouVideo
        driver, browser, context, page, locator = fake_browser()
        locator.input_value = AsyncMock(return_value='2026-10-16 12:00:00')
        app = KuaiShouVideo('test', 'video', 'cover', [], 0, 'cookie.json')
        await app.set_schedule_time_kuaishou(page, datetime(2026, 10, 16, 12))
        locator.input_value.assert_awaited_once()


class DouyinScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_schedule_stops_before_publish(self):
        from Uploaders.DouyinUpload import DouYinVideo
        driver, browser, context, page, locator = fake_browser()
        locator.input_value = AsyncMock(return_value='2026-10-16 08:00')
        app = DouYinVideo('test', 'video', 'cover', [], 0, 'cookie.json')
        with self.assertRaisesRegex(RuntimeError, '未接受指定排期'):
            await app.set_schedule_time_douyin(page, datetime(2026, 10, 16, 12))

    async def test_accepted_schedule_is_verified(self):
        from Uploaders.DouyinUpload import DouYinVideo
        driver, browser, context, page, locator = fake_browser()
        locator.input_value = AsyncMock(return_value='2026-10-16 12:00')
        app = DouYinVideo('test', 'video', 'cover', [], 0, 'cookie.json')
        await app.set_schedule_time_douyin(page, datetime(2026, 10, 16, 12))
        locator.input_value.assert_awaited_once()


class UploadProgressTests(unittest.IsolatedAsyncioTestCase):
    def make_page(self, *, failed=False, ready=False):
        page = MagicMock()
        page.is_closed.return_value = False
        failure, success = MagicMock(), MagicMock()
        failure.first = failure
        success.first = success
        failure.is_visible = AsyncMock(return_value=failed)
        success.is_visible = AsyncMock(return_value=ready)
        page.locator.side_effect = lambda selector: failure if selector == 'failed' else success
        return page, failure, success

    async def test_visible_success_completes_without_retry(self):
        page, failure, success = self.make_page(ready=True)
        retry = AsyncMock()
        await wait_for_upload(page, 'ready', 'failed', retry)
        retry.assert_not_awaited()

    async def test_persistent_failure_stops_after_two_retries(self):
        page, failure, success = self.make_page(failed=True)
        retry = AsyncMock()
        with patch('Uploaders.browser.asyncio.sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, '2 次'):
                await wait_for_upload(page, 'ready', 'failed', retry)
        self.assertEqual(retry.await_count, 2)

    async def test_retry_error_is_not_swallowed(self):
        page, failure, success = self.make_page(failed=True)
        with self.assertRaisesRegex(RuntimeError, 'upload input changed'):
            await wait_for_upload(page, 'ready', 'failed', AsyncMock(side_effect=RuntimeError('upload input changed')))

    async def test_closed_page_fails_immediately(self):
        page, failure, success = self.make_page()
        page.is_closed.return_value = True
        with self.assertRaisesRegex(RuntimeError, '页面已关闭'):
            await wait_for_upload(page, 'ready', 'failed', AsyncMock())

    async def test_missing_visible_progress_times_out(self):
        page, failure, success = self.make_page()
        with self.assertRaises(TimeoutError):
            await wait_for_upload(page, 'ready', 'failed', AsyncMock(), timeout=0)
