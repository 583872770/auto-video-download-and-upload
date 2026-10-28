"""Resolve YouTube videos through the Bergung Cobalt website."""
import asyncio
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeoutError, async_playwright

from webapp.yt5s import BrowserEnvironmentRequired, WebsiteTimeoutError, require_browser_environment
from webapp.yt5s_browser import session


SITE_URL = 'https://bergung.hoffnungfuerdiezukunft.net/'
API_URL = 'https://bergung-api.hoffnungfuerdiezukunft.net/'
MAX_RESPONSE = 1024 * 1024
RETRYABLE_NAVIGATION_ERRORS = ('net::ERR_CONNECTION_CLOSED', 'net::ERR_CONNECTION_RESET',
                               'net::ERR_TIMED_OUT', 'net::ERR_PROXY_CONNECTION_FAILED',
                               'net::ERR_TUNNEL_CONNECTION_FAILED')


class BergungError(RuntimeError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def require_environment(config, base):
    from webapp.environment import DownloadEnvironmentRequired

    try:
        executable = require_browser_environment(config, base)
    except BrowserEnvironmentRequired as error:
        raise BrowserEnvironmentRequired(str(error).replace('yt5s（方式二 / 三）', 'Bergung')) from error
    if not shutil.which('ffprobe') and not (Path(__file__).resolve().parents[1] / '.tools/ffmpeg/bin/ffprobe.exe').is_file():
        raise DownloadEnvironmentRequired('Bergung 需要 ffprobe 校验视频和音频，请安装 FFmpeg。')
    return executable


def result_info(data, video_id, quality, http_status=200):
    from webapp.yt1d import media_url

    if not isinstance(data, dict):
        raise BergungError('Bergung 未返回有效的解析结果。')
    status = data.get('status')
    if status == 'error':
        error = data.get('error')
        code = error.get('code', '') if isinstance(error, dict) else ''
        if not isinstance(code, str) or len(code) > 120:
            code = ''
        if 'auth' in code or 'captcha' in code:
            raise BergungError('Bergung 验证或授权未完成，请在远程桌面完成网站验证。')
        raise BergungError('Bergung 解析失败，视频可能不可用或当前出口受限。'
                           + (f'（{code}）' if code and code.isascii() and code.replace('.', '').replace('_', '').isalnum() else ''),
                           retryable=http_status in (408, 429, 500, 502, 503, 504)
                           or 'rate' in code or 'timeout' in code or 'unreachable' in code)
    if status not in ('redirect', 'tunnel'):
        raise BergungError('Bergung 未返回单个可下载视频，请检查网站的处理方式或画质设置。')
    try:
        link = media_url(data.get('url'))
    except RuntimeError:
        raise BergungError('Bergung 未返回有效的公网视频地址。') from None
    filename = data.get('filename')
    if not isinstance(filename, str) or len(filename) > 512:
        raise BergungError('Bergung 未返回有效的视频文件名。')
    filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
    suffix = Path(filename).suffix.lower()
    if suffix not in ('.mp4', '.webm', '.mkv'):
        raise BergungError('Bergung 返回的文件不是支持的视频格式。')
    title = Path(filename).stem.strip() or video_id
    return {'id': video_id, 'title': title, 'url': link, 'ext': suffix[1:],
            'format_id': 'bergung-' + quality,
            'thumbnail': f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
            'webpage_url': f'https://www.youtube.com/watch?v={video_id}',
            'extractor': 'youtube', 'extractor_key': 'Youtube',
            'http_headers': {'Referer': SITE_URL}}


async def open_site(page, url):
    for attempt in range(2):
        try:
            return await page.goto(url, wait_until='load', timeout=45000)
        except BrowserError as error:
            if attempt or not any(code in str(error) for code in RETRYABLE_NAVIGATION_ERRORS):
                raise
            await asyncio.sleep(1)


async def probe(args, owner, on_stage=None):
    def stage(value):
        if on_stage:
            on_stage(value)

    async def select_setting(page, name, value):
        selector = f'#setting-button-save-{name}-{value}'
        button = page.locator(selector)
        for _ in range(3):
            if await button.get_attribute('aria-pressed', timeout=20000) == 'true':
                return
            await button.click(timeout=20000)
            try:
                await page.wait_for_function(
                    "selector => document.querySelector(selector)?.getAttribute('aria-pressed') === 'true'",
                    arg=selector, timeout=2000)
                return
            except BrowserTimeoutError:
                pass
        raise BergungError('Bergung 网页设置未生效，请检查网站是否正常加载。')

    stage('starting_browser')
    address = await owner.start(args)
    async with async_playwright() as driver:
        stage('connecting_browser')
        browser = await driver.chromium.connect_over_cdp(address, timeout=20000)
        context = browser.contexts[0]
        existing_pages = set(context.pages)
        page = await context.new_page()
        pending = set()
        completed = asyncio.Event()
        result = {}
        try:
            async def capture(response):
                if response.url.rstrip('/') != args.api_url.rstrip('/') or response.request.method != 'POST':
                    return
                try:
                    body = await asyncio.wait_for(response.body(), timeout=20)
                    if len(body) > MAX_RESPONSE:
                        raise ValueError('large response')
                    result['data'] = json.loads(body)
                except Exception:
                    result['error'] = BergungError('Bergung 接口未返回有效 JSON，网站可能已更新。')
                result['status'] = response.status
                completed.set()

            def on_response(response):
                if response.url.rstrip('/') == args.api_url.rstrip('/') and response.request.method == 'POST':
                    task = asyncio.create_task(capture(response))
                    pending.add(task)
                    task.add_done_callback(pending.discard)

            def on_download(download):
                task = asyncio.create_task(download.cancel())
                pending.add(task)
                task.add_done_callback(pending.discard)

            page.on('response', on_response)
            page.on('download', on_download)
            stage('opening_site')
            response = await open_site(page, args.site_url + 'settings/video')
            if response and response.status >= 400:
                raise BergungError(f'Bergung 网页返回 HTTP {response.status}。', retryable=response.status >= 500)
            await select_setting(page, 'videoQuality', args.quality)
            await select_setting(page, 'youtubeVideoContainer', 'auto')
            if args.quality in ('max', '4320', '2160', '1440'):
                await select_setting(page, 'youtubeVideoCodec', 'av1')
            else:
                await select_setting(page, 'youtubeVideoCodec', 'h264')
            await open_site(page, args.site_url + 'settings/local')
            await select_setting(page, 'localProcessing', 'disabled')
            response = await open_site(page, args.site_url)
            if response and response.status >= 400:
                raise BergungError(f'Bergung 网页返回 HTTP {response.status}。', retryable=response.status >= 500)
            link_input = page.locator('#link-area')
            await link_input.wait_for(state='visible', timeout=30000)
            await select_setting(page, 'downloadMode', 'auto')
            await link_input.fill(f'https://www.youtube.com/watch?v={args.video_id}')
            stage('waiting_verification')
            print(f'Bergung 正在等待网站验证，最多 {args.timeout} 秒；需要时可在远程桌面完成。', flush=True)
            try:
                await page.wait_for_function(
                    "() => Boolean(document.querySelector('input[name=\"cf-turnstile-response\"]')?.value)",
                    timeout=args.timeout * 1000)
            except BrowserTimeoutError:
                raise WebsiteTimeoutError('Bergung 网站验证超时，请在远程桌面完成验证或更换节点。') from None
            stage('submitting_site')
            await page.locator('#download-button').click(timeout=30000)
            stage('extracting')
            await asyncio.wait_for(completed.wait(), timeout=45)
            if result.get('error'):
                raise result['error']
            if result['status'] >= 400 and (not isinstance(result.get('data'), dict)
                                            or result['data'].get('status') != 'error'):
                raise BergungError(f'Bergung 接口返回 HTTP {result["status"]}。',
                                   retryable=result['status'] in (408, 429, 500, 502, 503, 504))
            return result_info(result.get('data'), args.video_id, args.quality, result['status'])
        except (BrowserTimeoutError, TimeoutError):
            raise WebsiteTimeoutError('Bergung 网页加载或解析超时，请检查当前节点或在远程桌面完成验证。') from None
        finally:
            for task in list(pending):
                task.cancel()
            await asyncio.gather(*list(pending), return_exceptions=True)
            try:
                opened = set(context.pages) - existing_pages
                opened.add(page)
                await asyncio.wait_for(asyncio.gather(*(item.close() for item in opened)), timeout=5)
            except Exception:
                await asyncio.to_thread(owner.close)


def resolve(url, config, base, directory, on_stage=None, *, worker_slot=0, browser_capacity=None):
    from webapp.settings import validate_method_urls
    from webapp.store import source_identity

    validate_method_urls({'download_method': 'bergung'}, [url])
    video_id, _ = source_identity(url)
    proxy = '' if config.get('proxy_mode') == 'direct' else (
        config.get('_resolved_clash_proxy', '') if config.get('proxy_mode') == 'clash' else config.get('proxy', ''))
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise BergungError('Bergung 暂仅支持不含账号密码的 HTTP(S) 代理，请使用 Clash HTTP 端口或直连。')
    executable = require_environment(config, base)
    pool = Path(directory) / 'yt5s'
    capacity = browser_capacity if browser_capacity is not None else config.get('yt5s_browser_count', 2)
    signature = (executable, proxy, config.get('_resolved_clash_node', ''))
    quality = str(config.get('bergung_quality', 'best'))
    args = SimpleNamespace(video_id=video_id, quality='max' if quality == 'best' else quality,
                           timeout=int(config.get('bergung_timeout', 180)), proxy=proxy,
                           browser_executable=executable, site_url=SITE_URL, api_url=API_URL)

    def waiting():
        if on_stage:
            on_stage('waiting_browser')

    with session(pool, worker_slot).use(pool, capacity, waiting, worker_slot, signature,
            **({'disk_directory': config['_disk_state'], 'disk_config': config} if config.get('_disk_state') else {})) as owner:
        try:
            return asyncio.run(probe(args, owner, on_stage))
        except BrowserError as error:
            raise BergungError('Bergung 浏览器连接网站失败，请检查当前网络或代理节点。',
                               retryable=True) from error
