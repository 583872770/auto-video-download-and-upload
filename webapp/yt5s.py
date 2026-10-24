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

from playwright.async_api import async_playwright


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


async def probe(args, directory, report, *, download=True, on_stage=None):
    stage = on_stage or (lambda value: None)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    pending = set()
    resolved = asyncio.Event()
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
                browser = await driver.chromium.connect_over_cdp(address)
                try:
                    page = await browser.contexts[0].new_page()

                    async def response_seen(response):
                        if not any(path in response.url for path in ('/mates/en/analyze/ajax', '/mates/en/convert')):
                            return
                        step = 'analyze' if '/analyze/' in response.url else 'convert'
                        entry = {'step': step, 'http_status': response.status}
                        try:
                            data = await response.json()
                            entry.update(status=data.get('status'),
                                         verification_failed='Verification failed' in str(data.get('result', '')))
                            if step == 'analyze':
                                state['analyze_status'] = data.get('status')
                                resolved.set()
                            if data.get('downloadUrlX'):
                                state['download_url'] = data['downloadUrlX']
                        except Exception:
                            entry['json_response'] = False
                        report['responses'].append(entry)
                        print(json.dumps(entry, ensure_ascii=False), flush=True)

                    def received(response):
                        if '/mates/en/' in response.url:
                            operation = asyncio.create_task(response_seen(response))
                            pending.add(operation)
                            operation.add_done_callback(pending.discard)

                    page.on('response', received)
                    await page.goto('https://yt5s.biz/enwr200/', wait_until='domcontentloaded', timeout=45000)
                    await page.locator('#txt-url').fill('https://www.youtube.com/watch?v=' + args.video_id)
                    stage('waiting_verification')
                    await page.locator('#btn-submit').click()
                    print('等待网站验证与解析；需要时可在远程桌面中操作。', flush=True)
                    try:
                        await asyncio.wait_for(resolved.wait(), timeout=args.timeout)
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
                            deadline = time.monotonic() + args.timeout
                            while not state.get('download_url'):
                                if time.monotonic() >= deadline:
                                    raise RuntimeError('网站转换超时，尚未得到下载链接。')
                                await asyncio.sleep(0.5)
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
                    for operation in list(pending):
                        operation.cancel()
                    await asyncio.gather(*list(pending), return_exceptions=True)
                    await browser.close()
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
    from Utils.config import resolve_path
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
    executable = str(resolve_path(base, config['browser_executable'])) if config.get('browser_executable') else ''
    # Serialize browser resolution; completed direct transfers may run in parallel.
    directory = Path(directory) / 'yt5s'
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 600
    while True:
        with worker_lock(directory / 'resolver.lock') as acquired:
            if acquired:
                args = SimpleNamespace(video_id=video_id, quality=config.get('yt5s_quality', '360'),
                                       timeout=180, proxy=proxy, browser_executable=executable)
                try:
                    with tempfile.TemporaryDirectory(prefix='resolve-', dir=directory) as temporary:
                        return asyncio.run(probe(args, Path(temporary), {'responses': []},
                                                 download=False, on_stage=on_stage))
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
