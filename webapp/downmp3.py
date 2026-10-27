"""Resolve downmp3's hosted Loader.to component without a browser or cookies."""
import ipaddress
import json
import math
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

API_BASE = 'https://p.savenow.to'
API_HOSTS = {'p.savenow.to', 'p.lbserver.xyz'}
MAX_RESPONSE = 1024 * 1024


class Downmp3Error(RuntimeError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def api_url(value):
    """Only poll the service's own API; never follow arbitrary progress URLs."""
    url = urljoin(API_BASE + '/', value)
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in API_HOSTS or parsed.username
            or parsed.password or parsed.port not in (None, 443) or not parsed.path.startswith('/api/')):
        raise Downmp3Error('downmp3 返回了无效的进度接口地址。')
    return url


def media_url(value):
    if not isinstance(value, str):
        raise Downmp3Error('downmp3 未返回有效的视频下载地址。')
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    if parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password:
        raise Downmp3Error('downmp3 未返回有效的视频下载地址。')
    try:
        private = not ipaddress.ip_address(host).is_global
    except ValueError:
        private = host.lower() == 'localhost' or host.lower().endswith(('.localhost', '.local'))
    if private:
        raise Downmp3Error('downmp3 返回的下载地址不是公网视频地址。')
    return value


def request_json(opener, url, timeout):
    request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'Frameflow/1.0',
                                    'Referer': 'https://downmp3.yt/'})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        hint = '；当前出口可能需要网站验证，请更换节点或使用其他下载方式' if status == 403 else ''
        raise Downmp3Error(f'downmp3 接口返回 HTTP {status}{hint}。',
                           retryable=status in (408, 429, 500, 502, 503, 504)) from None
    except (URLError, TimeoutError, socket.timeout, OSError) as error:
        from webapp.diagnostics import safe_text
        # Preserve the DNS/connect/TLS reason, but never the signed request URL.
        reason = getattr(error, 'reason', error)
        raise Downmp3Error('downmp3 接口连接失败或超时，请检查当前连接和节点。'
                           f'（{type(reason).__name__}: {safe_text(reason)[:800]}）', retryable=True) from None
    if len(raw) > MAX_RESPONSE:
        raise Downmp3Error('downmp3 接口响应过大，未获取有效解析结果。')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Downmp3Error('downmp3 接口未返回 JSON，可能需要网站验证或接口已变更。') from None
    if not isinstance(data, dict):
        raise Downmp3Error('downmp3 接口返回的数据格式无效。')
    return data


def resolve(url, config, base, on_stage=None):
    from webapp.settings import validate_method_urls
    from webapp.store import source_identity

    validate_method_urls({**config, 'download_method': 'downmp3'}, [url])
    video_id, _ = source_identity(url)
    # The API accepts a numeric target, not "best"; its highest supported
    # video target is currently 1080p. The source may have a lower resolution.
    quality = config.get('downmp3_quality', 'best')
    if quality == 'best':
        quality = '1080'
    timeout = int(config.get('downmp3_timeout', 300))
    proxy = '' if config.get('proxy_mode') == 'direct' else (
        config.get('_resolved_clash_proxy', '') if config.get('proxy_mode') == 'clash' else config.get('proxy', ''))
    opener = build_opener(ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}))
    deadline = time.monotonic() + timeout

    def request(endpoint):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Downmp3Error(f'downmp3 转换等待超时（{timeout} 秒），可稍后重试。', retryable=True)
        return request_json(opener, api_url(endpoint), min(30, remaining))

    def stage(name, percent=None):
        if on_stage:
            on_stage(name, percent)

    stage('requesting_api')
    print(f'downmp3：提交 {quality}p 视频转换请求，无需浏览器或 YouTube Cookie。', flush=True)
    initial = request('/api/v2/download?' + urlencode({
        'button': 1, 'format': quality, 'iframe_source': 'https://downmp3.yt/',
        'url': 'https://www.youtube.com/watch?v=' + video_id}))
    if initial.get('success') in (False, 0, '0') or initial.get('error'):
        raise Downmp3Error('downmp3 未能创建转换任务，视频可能不可用或当前出口受限。')
    data = initial
    if not (data.get('success') in (True, 1, '1') and data.get('download_url')):
        identity = initial.get('id')
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            raise Downmp3Error('downmp3 未返回转换任务编号，接口可能已变更。')
        progress_url = api_url(initial.get('progress_url') or '/api/progress?' + urlencode({'id': identity}))
        print('downmp3：转换任务已创建，等待远端准备视频；此阶段尚未下载到服务器。', flush=True)
        last_log = 0
        while True:
            data = request(progress_url)
            percent = data.get('progress')
            percent = (round(max(0, min(100, float(percent) / 10)), 1)
                       if isinstance(percent, (float, int)) and not isinstance(percent, bool)
                       and math.isfinite(percent) else None)
            stage('waiting_api_conversion', percent)
            if data.get('success') in (True, 1, '1'):
                if not data.get('download_url'):
                    raise Downmp3Error('downmp3 转换已结束，但没有返回视频下载地址。')
                break
            # success=0 is also the service's normal "still processing" state.
            if data.get('error') or str(data.get('status', '')).lower() in ('failed', 'error'):
                raise Downmp3Error('downmp3 视频转换失败，视频可能不可用或网站上游异常。')
            now = time.monotonic()
            if now - last_log >= 15:
                print('downmp3：远端准备中' + (f'，进度 {percent}%' if percent is not None else '') + '。', flush=True)
                last_log = now
            time.sleep(min(3, max(0, deadline - time.monotonic())))
    link = media_url(data['download_url'])
    info = {**(initial['info'] if isinstance(initial.get('info'), dict) else {}),
            **(data['info'] if isinstance(data.get('info'), dict) else {})}
    title = str(data.get('title') or initial.get('title') or info.get('title') or video_id)
    thumbnail = data.get('thumbnail_url') or initial.get('thumbnail_url') or info.get('image')
    print('downmp3：下载地址已就绪，开始传输视频文件。', flush=True)
    stage('extracting')
    return {'id': video_id, 'title': title, 'url': link, 'ext': 'mp4',
            'height': int(quality), 'format_id': 'downmp3-' + quality,
            'thumbnail': thumbnail, 'webpage_url': 'https://www.youtube.com/watch?v=' + video_id,
            'extractor': 'youtube', 'extractor_key': 'Youtube',
            'http_headers': {'Referer': 'https://p.savenow.to/'}}
