"""Run a real browser against intercepted local HTML, with no platform requests."""
import asyncio
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright
from Uploaders.browser import (browser_session, confirm_publication, login_ready,
                               save_storage_state, validate_storage_state)
from Utils.runtime import configure_runtime


async def main():
    configure_runtime()
    html = '<input type="file" accept="video/mp4" hidden><div role="status">发布成功</div>'
    async with async_playwright() as playwright:
        async with browser_session(playwright) as context:
            browser = context.browser
            async def fulfill(route):
                await route.fulfill(status=200, content_type='text/html; charset=utf-8', body=html)
            await context.route('**/*', fulfill)
            page = await context.new_page()
            await page.goto('https://offline.test/upload')
            assert await login_ready(page, timeout=1000), 'hidden upload input not detected'
            receipt = await confirm_publication(page, 'xigua', timeout=1000)
            assert receipt['status'] == 'confirmed'
            with tempfile.TemporaryDirectory() as directory:
                cookie = Path(directory) / 'account.json'
                await save_storage_state(context, cookie)
                validate_storage_state(cookie)
            await page.goto('https://cp.kuaishou.com/article/manage/video?status=2&from=publish')
            assert (await confirm_publication(page, 'kuaishou', timeout=1000))['status'] == 'confirmed'
            await page.goto('https://creator.douyin.com/creator-micro/content/manage')
            assert (await confirm_publication(page, 'douyin', timeout=1000))['status'] == 'confirmed'
            await page.set_content('<p>No success notification</p>')
            try:
                await confirm_publication(page, 'xigua', timeout=50)
            except RuntimeError:
                pass
            else:
                raise AssertionError('missing receipt was reported as successful')
        assert not browser.is_connected(), 'browser process still connected after session closed'
    print('Browser smoke OK: launch, hidden input, three receipts, missing receipt, state save, cleanup.')


if __name__ == '__main__':
    asyncio.run(main())
