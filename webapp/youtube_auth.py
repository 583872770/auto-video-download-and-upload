"""Read-only YouTube session preflight, shared across download processes."""
import hashlib
from http.client import HTTPException
from http.cookiejar import MozillaCookieJar
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPCookieProcessor, build_opener
import uuid

from sqlalchemy import select

from Utils.config import resolve_path
from Utils.youtube_login import parse_youtube_cookie_file, save_youtube_cookies
from webapp.database import settings
from web_worker import worker_lock

AUTH_COOKIES = {'SID', 'HSID', 'SSID', 'APISID', 'SAPISID', 'LOGIN_INFO',
                '__Secure-1PSID', '__Secure-3PSID', '__Secure-1PAPISID', '__Secure-3PAPISID'}


class YouTubeAuthRequired(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__(result['message'])


def is_youtube(urls):
    return any((urlsplit(url).hostname or '').lower() in
               ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be') for url in urls)


def logged_in(document):
    """Require an explicit server flag, never infer login from a cookie name."""
    decoder = json.JSONDecoder()
    authenticated = None
    for match in re.finditer(r'ytcfg\.set\s*\(\s*', document):
        try:
            value, _ = decoder.raw_decode(document, match.end())
        except ValueError:
            continue
        if isinstance(value, dict) and type(value.get('LOGGED_IN')) is bool:
            authenticated = value['LOGGED_IN']
    return authenticated


def probe(cookie_data, proxy):
    # Load from the checked bytes, not a path that may change mid-request.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='youtube-preflight-') as directory:
        file = Path(directory) / 'cookies.txt'
        save_youtube_cookies(parse_youtube_cookie_file(cookie_data), file)
        jar = MozillaCookieJar(str(file))
        jar.load(ignore_discard=True)
        opener = build_opener(ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}), HTTPCookieProcessor(jar))
        request = Request('https://www.youtube.com/feed/history?hl=en', headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9', 'Accept-Encoding': 'identity'})
        try:
            with opener.open(request, timeout=15) as response:
                host = (urlsplit(response.geturl()).hostname or '').lower()
                if host not in ('www.youtube.com', 'youtube.com'):
                    return 'verification_required', 'YouTube 要求登录或额外验证，请使用当前节点完成浏览器登录后重试。'
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    return 'unconfirmed', 'YouTube 返回内容异常，尚未确认登录，请稍后重新检测。'
        except HTTPError as error:
            error.close()
            if error.code == 401:
                return 'not_logged_in', 'YouTube 未认可当前 Cookie，请重新登录或上传新的 Cookie。'
            if error.code in (403, 429):
                return 'verification_required', '当前节点被 YouTube 限制或要求额外验证，请检查节点后重新检测。'
            return 'network_error', '无法连接 YouTube 验证登录，请检查当前代理节点后重试。'
        except (URLError, OSError, ValueError, HTTPException):
            return 'network_error', '无法连接 YouTube 验证登录，请检查当前代理节点后重试。'
    authenticated = logged_in(data.decode('utf-8', errors='replace'))
    if authenticated is True:
        return 'valid', 'YouTube 登录验证通过。'
    if authenticated is False:
        return 'not_logged_in', 'YouTube 未认可当前 Cookie，请重新登录或上传新的 Cookie。'
    return 'unconfirmed', '未能确认 YouTube 登录状态，请在当前节点完成浏览器验证后重新检测。'


class YouTubeAuth:
    def __init__(self, store):
        self.store = store
        self.directory = store.directory / 'youtube-auth'
        self.directory.mkdir(parents=True, exist_ok=True)

    def _read(self, key, default=None):
        with self.store.connect() as db:
            value = db.execute(select(settings.c.value).where(settings.c.key == key)).scalar_one_or_none()
        return json.loads(value) if value else default

    def _write(self, key, value):
        with self.store.connect() as db:
            db.execute(self.store.db.upsert(settings, dict(key=key, value=json.dumps(value, ensure_ascii=False)), ['key']))

    def invalidate(self):
        self._write('youtube_auth_generation', uuid.uuid4().hex)
        self._write('youtube_auth_latest', {'state': 'unchecked', 'message': 'Cookie 或连接已更新，下载前将验证 YouTube 登录。'})
        self.clear_block()

    def clear_block(self):
        self._write('youtube_auth_block', None)

    def block_reason(self):
        return self._read('youtube_auth_block')

    def block(self, result):
        with self.store.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='true'))
            value = json.dumps(result, ensure_ascii=False)
            db.execute(self.store.db.upsert(settings, dict(key='youtube_auth_block', value=value), ['key']))

    def status(self):
        result = self._read('youtube_auth_latest', {'state': 'unchecked', 'message': '尚未检测 YouTube 登录状态。'})
        if result['state'] == 'valid' and result.get('expires_at', 0) <= time.time():
            return {**result, 'state': 'unchecked', 'message': '登录验证已到期，下次下载前将重新检测。'}
        return result

    def check(self, config, base, *, proxy=None, node='', force=False):
        if proxy is None:
            proxy = '' if config.get('proxy_mode') == 'direct' else config.get('proxy', '')
        connection = ('Clash · ' + node) if node else '手动代理' if proxy else '直连'
        result = {'state': 'missing', 'message': '请先登录 YouTube 或上传 Cookie，再开始下载。',
                  'connection': connection, 'checked_at': time.time()}
        try:
            if not config.get('youtube_cookie'):
                raise FileNotFoundError
            path = resolve_path(base, config['youtube_cookie'])
            with path.open('rb') as stream:
                data = stream.read(1024 * 1024 + 1)
            cookies = parse_youtube_cookie_file(data)
        except OSError:
            self._write('youtube_auth_latest', result)
            return result
        except ValueError:
            result.update(state='invalid', message='Cookie 文件无效或已过期，请重新登录 YouTube 或上传新的 Cookie。')
            self._write('youtube_auth_latest', result)
            return result
        auth = [cookie for cookie in cookies if cookie['name'] in AUTH_COOKIES and cookie['value']]
        if not auth:
            result.update(state='not_logged_in', message='Cookie 中没有 YouTube 登录凭据，请先登录或上传登录后的 Cookie。')
            self._write('youtube_auth_latest', result)
            return result
        generation = self._read('youtube_auth_generation', '')
        signature = hashlib.sha256(data + b'\0' + json.dumps([proxy, node, generation]).encode()).hexdigest()
        key = 'youtube_auth:' + signature
        deadline = time.monotonic() + 20
        while True:
            with worker_lock(self.directory / (signature + '.lock')) as acquired:
                if acquired:
                    cached = self._read(key)
                    if not force and cached and cached.get('expires_at', 0) > time.time():
                        self._write('youtube_auth_latest', cached)
                        return cached
                    try:
                        state, message = probe(data, proxy)
                    except Exception:
                        # A failed preflight must never become thousands of failed videos.
                        state, message = 'unconfirmed', '登录检测未完成，请检查网络和 Cookie 文件后重新检测。'
                    checked = time.time()
                    expiry = min([checked + 300, *[cookie['expires'] for cookie in auth if cookie['expires'] > 0]])
                    result.update(state=state, message=message, checked_at=checked,
                                  expires_at=expiry if state == 'valid' else checked + 15)
                    self._write(key, result)
                    self._write('youtube_auth_latest', result)
                    return result
            if time.monotonic() >= deadline:
                result.update(state='checking', message='另一个进程正在检测 YouTube 登录，请稍后重试。')
                return result
            time.sleep(0.1)

    def require(self, config, base, **options):
        result = self.check(config, base, **options)
        if result['state'] != 'valid':
            raise YouTubeAuthRequired(result)
        return result

    def reject(self, message):
        self.invalidate()
        result = {'state': 'verification_required', 'message': message, 'checked_at': time.time()}
        self._write('youtube_auth_latest', result)
        return result
