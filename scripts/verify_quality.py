"""Exercise yt5s quality selection in a real browser without external requests."""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from webapp.browser import browser_options
from webapp.yt5s import select_video_link


async def main():
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True, **browser_options(driver.chromium))
        try:
            page = await browser.new_page()
            await page.set_content('''<div id="result"><table>
                <tr><td>MP3 320kbps</td><td><a id="audio">Download</a></td></tr>
                <tr><td>MP4 360p</td><td><a id="360">Download</a></td></tr>
                <tr><td>MP4 1080p</td><td><button id="1080">Download</button></td></tr>
                <tr><td>MP4 2160p</td><td><button id="2160">Download</button></td></tr>
                <tr><td>MP4 2160p60</td><td><button id="2160-60">Download</button></td></tr>
                <tr><td>MP4 4320p</td><td><button disabled>Download</button></td></tr>
                <tr><td>MP4 4320p60</td><td>Unavailable</td></tr>
                </table></div>''')
            height, link = await select_video_link(page, 'best')
            assert (height, await link.get_attribute('id')) == (2160, '2160-60')
            height, link = await select_video_link(page, '360')
            assert (height, await link.get_attribute('id')) == (360, '360')
            # Highest available can also be a low-resolution source.
            await page.set_content('''<div id="result"><table>
                <tr><td>MP4 240p</td><td><a id="240">Download</a></td></tr>
                <tr><td>MP3 320kbps</td><td><a>Download</a></td></tr>
                </table></div>''')
            height, link = await select_video_link(page, 'best')
            assert (height, await link.get_attribute('id')) == (240, '240')
            await page.set_content('''<div id="result"><table>
                <tr><td>MP4 1080p</td><td><button disabled>Download</button></td></tr>
                </table></div>''')
            try:
                await select_video_link(page, 'best')
            except RuntimeError as error:
                assert '没有返回可下载' in str(error)
            else:
                raise AssertionError('Disabled download action was selected')
            print('Quality browser checks passed: highest resolution/fps, fixed quality, lower source, unavailable rows.')
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
