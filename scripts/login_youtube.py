"""Let the user sign in normally, then save this independent YouTube session."""
import argparse
import asyncio
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright, Error as BrowserError, TimeoutError as BrowserTimeoutError
from Uploaders.browser import browser_session
from Utils.config import DEFAULT_CONFIG, load_config
from Utils.runtime import ROOT, configure_runtime
from Utils.youtube_login import save_youtube_cookies


async def login(config, output):
    async with async_playwright() as driver:
        async with browser_session(driver, headless=False, proxy_server=config.get('proxy', '')) as context:
            page = await context.new_page()
            await page.goto('https://www.youtube.com/', wait_until='commit', timeout=60000)
            await page.bring_to_front()
            print('YouTube 登录窗口已打开，请在网页中完成正常登录，最多等待 10 分钟。', flush=True)
            rejected = page.get_by_text(re.compile(
                r'此浏览器或应用可能不安全|This browser or app may not be secure', re.IGNORECASE)).first
            for _ in range(60):
                if await rejected.is_visible():
                    raise RuntimeError('Google 拒绝自动化浏览器登录；请在正常浏览器手动登录并导出 YouTube 专用 Netscape Cookie。')
                try:
                    await page.locator('#avatar-btn img').first.wait_for(state='visible', timeout=10000)
                except BrowserTimeoutError:
                    continue
                cookies = await context.cookies(['https://www.youtube.com/'])
                save_youtube_cookies(cookies, output)
                print(f'YouTube 登录已确认，Netscape Cookie 已保存到：{output}', flush=True)
                return
            raise RuntimeError('10 分钟内未确认 YouTube 登录，未覆盖已有下载凭据。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='在独立浏览器登录 YouTube 并保存下载凭据')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--output', type=Path, default=ROOT / 'Cookies/youtube.txt')
    args = parser.parse_args(argv)
    configure_runtime()
    try:
        config, _ = load_config(args.config)
        asyncio.run(login(config, args.output.resolve()))
    except (OSError, ValueError, RuntimeError, BrowserError) as error:
        parser.exit(1, f'YouTube 登录未完成：{error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
