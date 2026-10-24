"""Native-browser resolver for yt5s and its independent download experiment.

Run with a real display (or Xvfb on Linux). This does not fake a challenge
response, modify browser fingerprints, or reuse a YouTube account session.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from playwright.async_api import TimeoutError as BrowserTimeoutError, async_playwright


class BrowserEnvironmentRequired(RuntimeError):
    """A deployment prerequisite is missing; keep queued downloads for later."""


class WebsiteTimeoutError(RuntimeError):
    """A website step timed out; retain normal queue retry/backoff behavior."""


class WebsiteHTTPError(RuntimeError):
    """Keep the website status separate from translated user-facing messages."""
    def __init__(self, step, status):
        self.status = status
        self.retryable = status in (408, 429, 500, 502, 503, 504)
        label = '解析' if step == 'analyze' else '转换'
        super().__init__(f'yt5s {label}接口返回 HTTP {status}，可能是网站上游故障或当前出口受限。')


def response_error(step, http_status, data):
    label = '解析' if step == 'analyze' else '转换'
    if isinstance(data, dict) and 'verification failed' in str(data.get('result', '')).lower():
        return RuntimeError('yt5s 网站验证未通过，请在远程桌面查看提示或更换节点。')
    if http_status >= 400:
        return WebsiteHTTPError(step, http_status)
    if not isinstance(data, dict):
        return RuntimeError(f'yt5s {label}接口未返回有效数据（HTTP {http_status}）；请查看远程桌面或检查节点。')
    # Some upstream failures are JSON business statuses inside an HTTP 200.
    status = str(data.get('status', ''))
    if status.isascii() and status.isdigit() and 400 <= int(status) <= 599:
        return WebsiteHTTPError(step, int(status))
    return None


def display_available():
    return os.name == 'nt' or bool(os.environ.get('DISPLAY'))


def require_browser_environment(config, base):
    from Utils.config import resolve_path

    explicit = str(resolve_path(base, config['browser_executable'])) if config.get('browser_executable') else ''
    try:
        executable = browser_executable(explicit)
    except RuntimeError as error:
        raise BrowserEnvironmentRequired(
            '方式二缺少 Chrome/Chromium。Docker 部署请同时加载主编排和 compose.yt5s.yaml，'
            '重新构建并创建容器（up -d --build）；非 Docker 环境请安装浏览器或配置 YT5S_BROWSER。') from error
    if not display_available():
        raise BrowserEnvironmentRequired(
            '方式二缺少 Linux 显示环境。Docker 部署请同时加载主编排和 compose.yt5s.yaml，'
            '重新构建并创建容器（up -d --build）；非 Docker 环境请启动 Xvfb 并配置 DISPLAY。')
    return executable


def browser_executable(explicit=''):
    configured = explicit or os.environ.get('YT5S_BROWSER', '')
    candidates = [configured, shutil.which('google-chrome'), shutil.which('chromium'),
                  shutil.which('chromium-browser')]
    for root in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        candidates.append(str(Path(os.environ.get(root, '')) / 'Google/Chrome/Application/chrome.exe'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError('没有找到 Chrome/Chromium，请安装浏览器或配置 YT5S_BROWSER。')


def verify_media(path):
    executable = shutil.which('ffprobe')
    local = Path(__file__).resolve().parents[1] / '.tools/ffmpeg/bin/ffprobe.exe'
    if not executable and local.is_file():
        executable = str(local)
    if not executable:
        raise RuntimeError('需要 ffprobe 验证下载文件，请安装 FFmpeg。')
    checked = subprocess.run([executable, '-v', 'error', '-show_entries',
                              'format=duration:stream=codec_type,codec_name', '-of', 'json', str(path)],
                             capture_output=True, text=True, timeout=30)
    if checked.returncode:
        raise RuntimeError('下载结果未通过 FFprobe 校验。')
    media = json.loads(checked.stdout)
    kinds = {stream.get('codec_type') for stream in media.get('streams', [])}
    if not {'video', 'audio'} <= kinds:
        raise RuntimeError('下载结果没有同时包含视频和音频。')
    return media


def transfer(url, directory, args):
    """Stream into this run's output, with byte and elapsed-time limits."""
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise RuntimeError('网站没有返回有效的 HTTP(S) 下载地址。')
    temporary = directory / (args.video_id + '.part')
    destination = directory / (args.video_id + '.mp4')
    maximum = args.max_mib * 1024 * 1024
    opener = build_opener(ProxyHandler({'http': args.proxy, 'https': args.proxy} if args.proxy else {}))
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://yt5s.biz/'})
    started, size = time.monotonic(), 0
    try:
        with opener.open(request, timeout=30) as response, temporary.open('wb') as output:
            announced = response.headers.get('Content-Length')
            if announced and int(announced) > maximum:
                raise RuntimeError('视频超过本次验证的文件大小上限。')
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' in content_type or 'application/json' in content_type:
                raise RuntimeError('下载地址返回了网页或错误信息，未得到视频。')
            while block := response.read(256 * 1024):
                size += len(block)
                if size > maximum or time.monotonic() - started > args.timeout:
                    raise RuntimeError('视频下载超过本次验证的大小或时间上限。')
                output.write(block)
            if announced and size != int(announced):
                raise RuntimeError('视频文件没有下载完整。')
        media = verify_media(temporary)
        temporary.replace(destination)
        return {'bytes': size, 'content_type': content_type, 'media': media,
                'download_seconds': round(time.monotonic() - started, 2), 'file': destination.name}
    finally:
        temporary.unlink(missing_ok=True)


async def wait_for_site(event, timeout, label, *, interval=10):
    """Bound each website wait and explain that no video bytes are transferring."""
    started = asyncio.get_running_loop().time()
    deadline = started + timeout
    while not event.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(interval, remaining))
        except TimeoutError:
            elapsed = asyncio.get_running_loop().time() - started
            if elapsed >= timeout:
                raise
            print(f'{label}：已等待 {int(elapsed)} 秒，最多 {timeout} 秒，视频文件尚未开始下载。', flush=True)


def site_timeout_message(stage, timeout):
    messages = {
        'starting_browser': '方式二浏览器启动超时，请检查 Chromium 和显示环境。',
        'connecting_browser': '方式二浏览器调试连接超时，请检查 Chromium 是否正常运行。',
        'opening_site': 'yt5s 网页加载超时（45 秒），请检查任务使用的连接方式和节点能否访问 yt5s.biz。',
        'waiting_site_form': 'yt5s 下载输入框未出现（30 秒），网页可能仍在验证或页面结构已变化；请查看远程桌面。',
        'submitting_site': 'yt5s 提交按钮未能完成操作（30 秒），请查看远程桌面中的验证或弹窗。',
        'waiting_verification': f'yt5s 验证与解析超时（{timeout} 秒），网站未返回解析结果；请在远程桌面查看验证提示或更换节点后重试。',
        'extracting': 'yt5s 已返回解析结果，但视频信息或所选画质未出现；网站页面结构可能已变化。',
        'converting': f'yt5s 视频转换超时（{timeout} 秒），网站未返回下载地址。',
    }
    return messages.get(stage, '方式二网站操作超时，请检查当前节点及远程桌面中的网页。')


async def probe(args, directory, report, *, download=True, on_stage=None):
    def stage(value):
        report['stage'] = value
        if on_stage:
            on_stage(value)

    stage('starting_browser')
    print('方式二浏览器连接：' + ('HTTP(S) 代理。' if args.proxy else '直连；如需代理，请确认任务创建时已启用。'), flush=True)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    pending = set()
    resolved = asyncio.Event()
    converted = asyncio.Event()
    state = {}
    # A fresh test profile; never open the user's normal browser profile.
    with tempfile.TemporaryDirectory(prefix='yt5s-probe-', ignore_cleanup_errors=True) as profile:
        command = [browser_executable(getattr(args, 'browser_executable', '')), f'--user-data-dir={profile}', f'--remote-debugging-port={port}',
                   '--remote-debugging-address=127.0.0.1', '--no-first-run',
                   '--no-default-browser-check', '--start-maximized']
        if os.environ.get('YT5S_PROBE_CONTAINER') == '1' or os.environ.get('YT5S_CONTAINER_BROWSER') == '1':
            command.append('--no-sandbox')
        if args.proxy:
            command.append('--proxy-server=' + args.proxy)
        command.append('about:blank')
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            address = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 20
            # CDP remains accessible only on the loopback interface.
            opener = build_opener(ProxyHandler({}))
            while True:
                try:
                    with opener.open(address + '/json/version', timeout=1) as response:
                        response.read()
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('原生浏览器启动失败，请检查显示环境和浏览器安装。')
                    await asyncio.sleep(0.2)
            async with async_playwright() as driver:
                stage('connecting_browser')
                browser = await driver.chromium.connect_over_cdp(address, timeout=20000)
                page = None
                try:
                    page = await browser.contexts[0].new_page()

                    async def response_seen(response):
                        if not any(path in response.url for path in ('/mates/en/analyze/ajax', '/mates/en/convert')):
                            return
                        step = 'analyze' if '/analyze/' in response.url else 'convert'
                        entry = {'step': step, 'http_status': response.status}
                        data = None
                        try:
                            data = await asyncio.wait_for(response.json(), timeout=15)
                        except Exception:
                            entry['json_response'] = False
                        error = response_error(step, response.status, data)
                        if error:
                            state[step + '_error'] = error
                        if isinstance(data, dict):
                            entry.update(status=data.get('status'),
                                         verification_failed='verification failed' in str(data.get('result', '')).lower())
                            if step == 'analyze':
                                state['analyze_status'] = data.get('status')
                            if not error and data.get('downloadUrlX'):
                                state['download_url'] = data['downloadUrlX']
                                converted.set()
                        if step == 'convert' and error:
                            converted.set()
                        report['responses'].append(entry)
                        print(json.dumps(entry, ensure_ascii=False), flush=True)
                        if step == 'analyze':
                            resolved.set()

                    def received(response):
                        if '/mates/en/' in response.url:
                            operation = asyncio.create_task(response_seen(response))
                            pending.add(operation)
                            operation.add_done_callback(pending.discard)

                    page.on('response', received)
                    stage('opening_site')
                    print('正在打开 yt5s 网页，最多等待 45 秒。', flush=True)
                    response = await page.goto('https://yt5s.biz/enwr200/', wait_until='domcontentloaded', timeout=45000)
                    print(f'yt5s 网页已响应，HTTP {response.status if response else "未知"}。', flush=True)
                    stage('waiting_site_form')
                    await page.locator('#txt-url').fill('https://www.youtube.com/watch?v=' + args.video_id, timeout=30000)
                    stage('submitting_site')
                    await page.locator('#btn-submit').click(timeout=30000)
                    stage('waiting_verification')
                    print(f'等待网站验证与解析，最多 {args.timeout} 秒；需要时可在远程桌面中操作。', flush=True)
                    try:
                        await wait_for_site(resolved, args.timeout, '网站验证与解析')
                        if state.get('analyze_error'):
                            raise state['analyze_error']
                        if state.get('analyze_status') != 'success':
                            raise RuntimeError('网站解析失败，本次验证未得到下载选项。')
                        report['analysis_succeeded'] = True
                        stage('extracting')
                        title = await page.locator('#video_title').inner_text()
                        thumbnail = await page.locator('#result .img-thumbnail').get_attribute('src')
                        duration_text = await page.locator('#result .caption p').inner_text()
                        duration_match = re.search(r'(\d+:\d+(?::\d+)?)', duration_text)
                        duration = None
                        if duration_match:
                            duration = 0
                            for part in duration_match[1].split(':'):
                                duration = duration * 60 + int(part)
                        link = page.locator('#result tr').filter(
                            has_text=re.compile(r'\b' + args.quality + r'p\b')).first.locator('a, button').filter(
                                has_text='Download').first
                        await link.wait_for(state='visible', timeout=15000)
                        href = await link.get_attribute('href')
                        if href and href.startswith(('https://', 'http://')):
                            state['download_url'] = href
                        else:
                            stage('converting')
                            await link.click()
                            await wait_for_site(converted, args.timeout, '网站视频转换')
                            if state.get('convert_error'):
                                raise state['convert_error']
                        # No YouTube credentials or challenge tokens are exported.
                        if not download:
                            return {'id': args.video_id, 'title': title.strip() or args.video_id,
                                    'url': state['download_url'], 'ext': 'mp4', 'duration': duration,
                                    'thumbnail': thumbnail, 'height': int(args.quality),
                                    'webpage_url': 'https://www.youtube.com/watch?v=' + args.video_id,
                                    'extractor': 'youtube', 'extractor_key': 'Youtube',
                                    'http_headers': {'Referer': 'https://yt5s.biz/'}}
                        report['download'] = await asyncio.to_thread(transfer, state['download_url'], directory, args)
                        report['success'] = True
                    finally:
                        try:
                            await page.screenshot(path=str(directory / 'page.png'), timeout=5000)
                        except Exception:
                            pass
                finally:
                    # Also capture failures before submission, such as a blocked landing page.
                    if page is not None and not (directory / 'page.png').is_file():
                        try:
                            await page.screenshot(path=str(directory / 'page.png'), timeout=5000)
                        except Exception:
                            pass
                    for operation in list(pending):
                        operation.cancel()
                    await asyncio.gather(*list(pending), return_exceptions=True)
                    try:
                        await asyncio.wait_for(browser.close(), timeout=10)
                    except Exception:
                        pass  # The native process is terminated below as well.
        except (TimeoutError, BrowserTimeoutError):
            raise WebsiteTimeoutError(site_timeout_message(report.get('stage'), args.timeout)) from None
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立验证 yt5s 原生浏览器解析和视频下载；不连接项目数据库')
    parser.add_argument('--video-id', default=os.environ.get('YT5S_VIDEO_ID', 'npNnbj46_ik'))
    parser.add_argument('--quality', choices=('360', '720'), default=os.environ.get('YT5S_QUALITY', '360'))
    parser.add_argument('--timeout', type=int, default=int(os.environ.get('YT5S_TIMEOUT', '180')))
    parser.add_argument('--max-mib', type=int, default=int(os.environ.get('YT5S_MAX_MIB', '128')))
    parser.add_argument('--proxy', default=os.environ.get('YT5S_PROXY', ''))
    parser.add_argument('--output', type=Path, default=Path('.cache/yt5s-probe'))
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', args.video_id):
        parser.error('video-id 必须是 11 位 YouTube 视频 ID')
    if not 15 <= args.timeout <= 1800 or not 1 <= args.max_mib <= 4096:
        parser.error('timeout 范围为 15–1800 秒，max-mib 范围为 1–4096 MiB')
    if args.proxy:
        parsed = urlsplit(args.proxy)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            parser.error('本验证工具仅支持不含账号密码的 HTTP(S) 代理，可连接 Clash 的 HTTP 端口')
    directory = args.output.resolve() / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    report = {'success': False, 'platform': os.name, 'video_id': args.video_id,
              'quality': args.quality, 'responses': []}
    try:
        asyncio.run(probe(args, directory, report))
    except HTTPError as error:
        report.update(error_type='HTTPError', error=f'视频下载返回 HTTP {error.code}。')
    except RuntimeError as error:
        report.update(error_type=type(error).__name__, error=str(error))
    except KeyboardInterrupt:
        report.update(error_type='Interrupted', error='本次验证已取消。')
    except Exception as error:
        # Browser/HTTP exception strings may contain signed URLs or proxy secrets.
        report.update(error_type=type(error).__name__, error='验证未完成，请查看 page.png 和失败阶段。')
    (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print('验证结果目录：' + str(directory), flush=True)
    return 0 if report['success'] else 1


def resolve(url, config, base, directory, on_stage=None):
    """Resolve one video without loading or exporting any YouTube login cookie."""
    from types import SimpleNamespace
    from webapp.settings import validate_method_urls
    from webapp.store import source_identity
    from web_worker import worker_lock

    validate_method_urls({**config, 'download_method': 'yt5s'}, [url])
    video_id, _ = source_identity(url)
    proxy = '' if config.get('proxy_mode') == 'direct' else (
        config.get('_resolved_clash_proxy', '') if config.get('proxy_mode') == 'clash' else config.get('proxy', ''))
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError('方式二暂仅支持不含账号密码的 HTTP(S) 代理，请使用 Clash HTTP 端口或直连。')
    executable = require_browser_environment(config, base)
    # Serialize browser resolution; completed direct transfers may run in parallel.
    directory = Path(directory) / 'yt5s'
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 600
    while True:
        with worker_lock(directory / 'resolver.lock') as acquired:
            if acquired:
                args = SimpleNamespace(video_id=video_id, quality=config.get('yt5s_quality', '360'),
                                       timeout=180, proxy=proxy, browser_executable=executable)
                report = {'responses': []}
                try:
                    with tempfile.TemporaryDirectory(prefix='resolve-', dir=directory) as temporary:
                        try:
                            return asyncio.run(probe(args, Path(temporary), report,
                                                     download=False, on_stage=on_stage))
                        except Exception:
                            try:
                                screenshot = Path(temporary) / 'page.png'
                                target = directory / 'last-failure.png'
                                saved = screenshot.is_file()
                                if saved:
                                    screenshot.replace(target)
                                    print('方式二最近失败截图已保留：' + str(target), flush=True)
                                else:
                                    target.unlink(missing_ok=True)
                                (directory / 'last-failure.json').write_text(json.dumps({
                                    'video_id': video_id, 'stage': report.get('stage'),
                                    'responses': report.get('responses', []),
                                    'screenshot_saved': saved, 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                                }, ensure_ascii=False), encoding='utf-8')
                            except OSError:
                                pass  # Diagnostics must not replace the original failure.
                            raise
                except RuntimeError:
                    raise
                except Exception as error:
                    raise RuntimeError('方式二未能完成网站验证或解析（' + type(error).__name__
                                       + '）。请检查浏览器、Linux 显示环境及当前节点；可通过远程桌面完成验证。') from None
        if time.monotonic() >= deadline:
            raise RuntimeError('等待方式二浏览器超时，请稍后重试。')
        if on_stage:
            on_stage('waiting_browser')
        time.sleep(0.5)
