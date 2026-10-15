"""Shared browser lifecycle, positive login checks and publication receipts."""
from contextlib import asynccontextmanager
import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

UPLOAD_INPUT = 'input[type="file"][accept*="video"]'
UPLOAD_URLS = {
    'douyin': 'https://creator.douyin.com/creator-micro/content/upload',
    'kuaishou': 'https://cp.kuaishou.com/article/publish/video',
    'xigua': 'https://studio.ixigua.com/upload?from=post_article',
}


def ensure_upload_origin(page, platform):
    host = urlsplit(page.url).hostname
    expected = urlsplit(UPLOAD_URLS[platform]).hostname
    if platform == 'xigua' and host == 'creator.douyin.com':
        raise RuntimeError('西瓜账号已进入抖音创作者中心，旧西瓜网页流程不可用；需使用合并后的发布流程，已停止操作')
    if host != expected:
        raise RuntimeError(f'{platform} 当前尚未进入对应创作者页面，未上传文件或保存登录态')


def browser_options(chromium, executable_path=None):
    if executable_path:
        if not Path(executable_path).is_file():
            raise FileNotFoundError(f'指定的浏览器不存在：{executable_path}')
        return {'executable_path': str(executable_path)}
    if Path(str(chromium.executable_path)).is_file():
        return {}
    # Standard installation paths only; every launch uses a fresh profile.
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        root = os.environ.get(variable)
        if not root:
            continue
        for relative in ('Microsoft/Edge/Application/msedge.exe',
                         'Google/Chrome/Application/chrome.exe'):
            candidate = Path(root) / relative
            if candidate.is_file():
                return {'executable_path': str(candidate)}
    return {}  # Playwright provides the actionable missing-browser error.


@asynccontextmanager
async def browser_session(playwright, *, account_file=None, headless=True,
                          executable_path=None, user_agent=None, proxy_server=None):
    options = browser_options(playwright.chromium, executable_path)
    selected_proxy = proxy_server if proxy_server is not None else os.environ.get('VIDEO_BROWSER_PROXY')
    if selected_proxy:
        options['proxy'] = {'server': selected_proxy}
    browser = await playwright.chromium.launch(
        headless=headless, **options)
    context = None
    try:
        options = {}
        if account_file is not None:
            options['storage_state'] = str(account_file)
        if user_agent:
            options['user_agent'] = user_agent
        context = await browser.new_context(**options)
        yield context
    finally:
        try:
            if context is not None:
                await context.close()
        finally:
            await browser.close()


def validate_storage_state(account_file):
    try:
        state = json.loads(Path(account_file).read_text(encoding='utf-8'))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(f'登录文件不是有效的 UTF-8 JSON：{account_file}') from error
    if not isinstance(state, dict) or not isinstance(state.get('cookies'), list) or not isinstance(state.get('origins'), list):
        raise ValueError(f'登录文件必须是 Playwright storage-state JSON：{account_file}')
    return state


async def save_storage_state(context, account_file):
    state = await context.storage_state()
    target = Path(account_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.login-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            json.dump(state, output, ensure_ascii=False)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def login_ready(page, timeout=10000):
    """A usable upload input is positive evidence; a missing login banner is not."""
    try:
        await page.locator(UPLOAD_INPUT).first.wait_for(state='attached', timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        return False


async def check_login(playwright, platform, account_file, executable_path=None):
    try:
        validate_storage_state(account_file)
    except (FileNotFoundError, ValueError):
        return False
    async with browser_session(playwright, account_file=account_file,
                               executable_path=executable_path) as context:
        page = await context.new_page()
        await page.goto(UPLOAD_URLS[platform], wait_until='domcontentloaded', timeout=60000)
        ready = await login_ready(page)
        if ready:
            ensure_upload_origin(page, platform)
        return ready


async def generate_login(playwright, platform, account_file, executable_path=None):
    async with browser_session(playwright, headless=False,
                               executable_path=executable_path) as context:
        page = await context.new_page()
        await page.goto(UPLOAD_URLS[platform], wait_until='commit', timeout=60000)
        await page.bring_to_front()
        print(f'请在浏览器中完成 {platform} 扫码登录，最多等待 10 分钟。', flush=True)
        for _ in range(60):
            if platform == 'xigua' and urlsplit(page.url).hostname == 'creator.douyin.com':
                ensure_upload_origin(page, platform)
            if await login_ready(page, timeout=10000):
                ensure_upload_origin(page, platform)
                await save_storage_state(context, account_file)
                print(f'{platform} 登录已确认，登录态已保存。', flush=True)
                return
        raise RuntimeError('10 分钟内未确认登录完成或上传页面已改版，未覆盖已有登录文件')


async def confirm_publication(page, platform, timeout=60000):
    """Require a management page or a visible, explicit success notification."""
    try:
        if platform == 'douyin':
            await page.wait_for_url('**/creator-micro/content/manage**', timeout=timeout)
        elif platform == 'kuaishou':
            dialog = page.locator('div.ant-modal-confirm-centered:visible').first
            try:
                await dialog.wait_for(state='visible', timeout=3000)
            except PlaywrightTimeoutError:
                pass
            else:
                await dialog.locator('button.ant-btn-primary:visible').first.click(timeout=8000)
            # Route is also used by social-auto-upload's current Kuaishou uploader.
            await page.wait_for_url('**/article/manage/video**', timeout=timeout)
        else:
            await page.locator(
                '[role="alert"], [role="status"], .arco-message, .byte-message, .m-message'
            ).filter(has_text=re.compile('发布成功')).first.wait_for(state='visible', timeout=timeout)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(f'{platform} 提交后未获得成功确认，请先核对平台结果再重试') from error
    return {'platform': platform, 'status': 'confirmed', 'url': page.url}


async def refresh_login_after_publish(context, account_file):
    try:
        await save_storage_state(context, account_file)
    except OSError:
        # The publication already succeeded; a cookie refresh failure must not
        # turn it into a failed job that users might mistakenly publish twice.
        print('发布已确认，但登录文件更新失败；下次运行前请检查文件权限。')


async def wait_for_upload(page, success_selector, failure_selector, retry, *, timeout=1200, max_retries=2):
    deadline = time.monotonic() + timeout
    retries = 0
    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError('浏览器页面已关闭，上传已停止')
        if await page.locator(failure_selector).first.is_visible():
            if retries >= max_retries:
                raise RuntimeError(f'视频上传连续失败，已重试 {max_retries} 次')
            retries += 1
            await retry()
        elif await page.locator(success_selector).first.is_visible():
            return
        await asyncio.sleep(1)
    raise TimeoutError('等待视频上传完成超时')
