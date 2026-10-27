"""Resolve yt1d through its normal website, using the shared native browser pool.

The page obtains its own Turnstile response. No YouTube Cookie or challenge
token is exported; only the resulting public media URL leaves the browser.
"""
import asyncio
import ipaddress
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from webapp.diagnostics import error_details, event, safe_text
from webapp.yt5s import BrowserEnvironmentRequired, probe, require_browser_environment
from webapp.yt5s_browser import session

SITE_URL = 'https://yt1d.lol/'


def require_environment(config, base):
    from webapp.environment import DownloadEnvironmentRequired
    import shutil

    try:
        executable = require_browser_environment(config, base)
    except BrowserEnvironmentRequired as error:
        raise BrowserEnvironmentRequired(str(error).replace('yt5s（方式二 / 三）', 'yt1d')) from error
    # verify_media also supports the project's bundled Windows FFprobe.
    if not shutil.which('ffprobe') and not (Path(__file__).resolve().parents[1] / '.tools/ffmpeg/bin/ffprobe.exe').is_file():
        raise DownloadEnvironmentRequired('yt1d 需要 ffprobe 校验视频和音频，请安装 FFmpeg。')
    return executable


def media_url(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443)):
            raise ValueError
        try:
            public = ipaddress.ip_address(host).is_global
        except ValueError:
            public = '.' in host and host != 'localhost' and not host.endswith(('.localhost', '.local'))
        if not public:
            raise ValueError
    except ValueError:
        raise RuntimeError('yt1d 未返回有效的公网视频下载地址。') from None
    return value


def resolve(url, config, base, directory, on_stage=None, *, worker_slot=0, browser_capacity=None):
    from webapp.settings import validate_method_urls
    from webapp.store import source_identity

    validate_method_urls({'download_method': 'yt1d'}, [url])
    video_id, _ = source_identity(url)
    proxy = '' if config.get('proxy_mode') == 'direct' else (
        config.get('_resolved_clash_proxy', '') if config.get('proxy_mode') == 'clash' else config.get('proxy', ''))
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError('yt1d 暂仅支持不含账号密码的 HTTP(S) 代理，请使用 Clash HTTP 端口或直连。')
    executable = require_environment(config, base)
    # Share the exact same slots/session registry with yt5s so their combined
    # resident browser count never exceeds the instance's browser limit.
    pool = Path(directory) / 'yt5s'

    def waiting():
        print('yt1d 正在等待共享浏览器名额。', flush=True)
        if on_stage:
            on_stage('waiting_browser')

    capacity = browser_capacity if browser_capacity is not None else config.get('yt5s_browser_count', 2)
    signature = (executable, proxy, config.get('_resolved_clash_node', ''))
    args = SimpleNamespace(video_id=video_id, quality=config.get('yt1d_quality', 'best'),
                           timeout=int(config.get('yt1d_timeout', 180)), proxy=proxy,
                           browser_executable=executable, provider='yt1d', site_url=SITE_URL)
    report = {'responses': []}
    try:
        with session(pool, worker_slot).use(pool, capacity, waiting, worker_slot, signature,
                **({'disk_directory': config['_disk_state'], 'disk_config': config} if config.get('_disk_state') else {})) as owner:
            info = asyncio.run(probe(args, pool, report, download=False, on_stage=on_stage, browser_owner=owner))
            info['url'] = media_url(info.get('url'))
            info['format_id'] = 'yt1d-' + str(info.get('height') or 'best')
            return info
    except Exception as error:
        event('browser_resolve_failed', provider='yt1d', video_id=video_id, worker_slot=worker_slot,
              stage=report.get('stage'), stage_seconds=report.get('stage_seconds', {}),
              elapsed_seconds=report.get('elapsed_seconds'), **error_details(error))
        if isinstance(error, RuntimeError):
            raise
        from webapp.disk_budget import DiskPressure
        if isinstance(error, (DiskPressure, OSError)):
            raise
        raise RuntimeError('yt1d 未能完成网站验证或解析，请检查当前节点或通过远程桌面完成验证。'
                           f'（{type(error).__name__}: {safe_text(error)[:1000]}）') from error
