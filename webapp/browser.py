"""Small Playwright helpers shared by YouTube login and diagnostics."""
from contextlib import asynccontextmanager
import os
from pathlib import Path


def browser_options(chromium, executable_path=None):
    if executable_path:
        if not Path(executable_path).is_file():
            raise FileNotFoundError(f'指定的浏览器不存在：{executable_path}')
        return {'executable_path': str(executable_path)}
    if Path(str(chromium.executable_path)).is_file():
        return {}
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        root = os.environ.get(variable)
        if not root:
            continue
        for relative in ('Microsoft/Edge/Application/msedge.exe',
                         'Google/Chrome/Application/chrome.exe'):
            candidate = Path(root) / relative
            if candidate.is_file():
                return {'executable_path': str(candidate)}
    return {}


@asynccontextmanager
async def browser_session(playwright, *, headless=True, executable_path=None,
                          user_agent=None, proxy_server=None):
    options = browser_options(playwright.chromium, executable_path)
    selected_proxy = proxy_server if proxy_server is not None else os.environ.get('VIDEO_BROWSER_PROXY')
    if selected_proxy:
        options['proxy'] = {'server': selected_proxy}
    browser = await playwright.chromium.launch(headless=headless, **options)
    context = None
    try:
        context_options = {}
        if user_agent:
            context_options['user_agent'] = user_agent
        context = await browser.new_context(**context_options)
        yield context
    finally:
        try:
            if context is not None:
                await context.close()
        finally:
            await browser.close()
