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


class LoginCancelled(RuntimeError):
    pass


async def login(config, output, *, on_status=None, cancelled=None, timeout_seconds=600):
    """Use a fresh visible session; cancellation also closes its browser."""
    def report(state, message):
        if on_status:
            on_status(state, message)

    async def watch_cancel():
        while cancelled is None or not cancelled.is_set():
            await asyncio.sleep(0.2)

    operation = asyncio.create_task(_login(config, output, report))
    cancellation = asyncio.create_task(watch_cancel())
    try:
        done, _ = await asyncio.wait((operation, cancellation), timeout=timeout_seconds,
                                     return_when=asyncio.FIRST_COMPLETED)
        if operation in done:
            return await operation
        if cancellation in done:
            raise LoginCancelled('登录已取消，已有 Cookie 保持不变。')
        raise RuntimeError('10 分钟内未确认 YouTube 登录，未覆盖已有下载凭据。')
    finally:
        operation.cancel()
        cancellation.cancel()
        await asyncio.gather(operation, cancellation, return_exceptions=True)


async def _login(config, output, report):
    async with async_playwright() as driver:
        async with browser_session(driver, headless=False, proxy_server=config.get('proxy', ''),
                                   executable_path=config.get('browser_executable') or None) as context:
            page = await context.new_page()
            report('starting', '正在打开 YouTube，请稍候…')
            await page.goto('https://www.youtube.com/', wait_until='commit', timeout=60000)
            await page.bring_to_front()
            report('waiting', '请在独立浏览器中登录 YouTube，最多等待 10 分钟；登录成功后自动保存并关闭窗口。')
            print('YouTube 登录窗口已打开，请在网页中完成正常登录，最多等待 10 分钟。', flush=True)
            rejected = page.get_by_text(re.compile(
                r'此浏览器或应用可能不安全|This browser or app may not be secure', re.IGNORECASE)).first
            while True:
                if await rejected.is_visible():
                    raise RuntimeError('Google 拒绝自动化浏览器登录；请在正常浏览器手动登录并导出 YouTube 专用 Netscape Cookie。')
                try:
                    await page.locator('#avatar-btn img').first.wait_for(state='visible', timeout=2000)
                except BrowserTimeoutError:
                    continue
                # Freeze the session before exporting, as recommended by yt-dlp.
                report('saving', '已确认登录，正在保存 YouTube Cookie…')
                await page.goto('https://www.youtube.com/robots.txt', wait_until='domcontentloaded', timeout=30000)
                cookies = await context.cookies(['https://www.youtube.com/'])
                save_youtube_cookies(cookies, output)
                print(f'YouTube 登录已确认，Netscape Cookie 已保存到：{output}', flush=True)
                return


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
