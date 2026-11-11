"""Read-only YouTube session preflight, shared across download processes."""
import hashlib
from http.client import HTTPException, HTTPSConnection
from http.cookiejar import MozillaCookieJar
import json
from pathlib import Path
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPCookieProcessor, HTTPSHandler, build_opener
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
        # Missing login evidence and an unavailable check are different failures.
        self.http_status = result.get('http_status')
        self.retry_after = result.get('retry_after') or 0
        self.retryable = self.http_status == 429 or result['state'] in ('network_error', 'unconfirmed', 'checking')
        self.connection_failure = result['state'] == 'network_error'
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


class SourceHTTPSHandler(HTTPSHandler):
    """Use the same direct source address as the media downloader."""
    def __init__(self, source):
        super().__init__()
        self.source = source

    def https_open(self, request):
        def connection(host, **kwargs):
            return HTTPSConnection(host, source_address=(self.source, 0), **kwargs)
        return self.do_open(connection, request, context=self._context)


class ProbeResult(tuple):
    """Keep the two-value probe API while retaining actual HTTP evidence."""
    def __new__(cls, state, message, *, http_status=None, retry_after=None, verification_reason=None):
        value = super().__new__(cls, (state, message))
        value.http_status, value.retry_after = http_status, retry_after
        value.verification_reason = verification_reason
        return value


def probe(cookie_data, proxy, *, source_address=''):
    # Load from the checked bytes, not a path that may change mid-request.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='youtube-preflight-') as directory:
        file = Path(directory) / 'cookies.txt'
        save_youtube_cookies(parse_youtube_cookie_file(cookie_data), file)
        jar = MozillaCookieJar(str(file))
        jar.load(ignore_discard=True)
        handlers = [ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}), HTTPCookieProcessor(jar)]
        if source_address and not proxy:
            handlers.append(SourceHTTPSHandler(source_address))
        opener = build_opener(*handlers)
        request = Request('https://www.youtube.com/feed/history?hl=en', headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9', 'Accept-Encoding': 'identity'})
        try:
            with opener.open(request, timeout=15) as response:
                host = (urlsplit(response.geturl()).hostname or '').lower()
                if host not in ('www.youtube.com', 'youtube.com'):
                    return ProbeResult('verification_required', '登录检查跳转到了 YouTube 以外的页面，请在当前出口的浏览器中查看提示后重新检测。',
                                       verification_reason='redirect')
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    return 'unconfirmed', 'YouTube 返回内容异常，尚未确认登录，请稍后重新检测。'
        except HTTPError as error:
            from webapp.download_policy import retry_after
            delay = retry_after((error.headers or {}).get('Retry-After'))
            error.close()
            if error.code == 401:
                return 'not_logged_in', 'YouTube 未认可当前 Cookie，请重新登录或上传新的 Cookie。'
            if error.code == 429:
                return ProbeResult('rate_limited', 'YouTube 登录检测收到 HTTP 429，请等待请求冷却；这不等于登录失效。',
                                   http_status=429, retry_after=delay)
            if error.code == 403:
                return ProbeResult('verification_required', 'YouTube 登录检查返回 HTTP 403（拒绝访问），具体原因尚未确认，请在当前出口检查后重新检测。',
                                   http_status=403, retry_after=delay)
            from webapp.diagnostics import event
            event('youtube_auth_probe_unavailable', http_status=error.code, proxy_configured=bool(proxy))
            return 'network_error', f'YouTube 登录检测暂时不可用（HTTP {error.code}），尚不能判断 Cookie 是否有效。'
        except (URLError, OSError, ValueError, HTTPException) as error:
            from webapp.diagnostics import event
            cause = error.reason if isinstance(error, URLError) else error
            event('youtube_auth_probe_unavailable', error_type=type(error).__name__,
                  cause_type=type(cause).__name__, proxy_configured=bool(proxy))
            reason = '连接超时' if isinstance(cause, TimeoutError) else 'TLS / 证书校验失败' if isinstance(cause, ssl.SSLError) else '网络请求失败'
            return 'network_error', f'YouTube 登录检测{reason}，尚不能判断 Cookie 是否有效，将稍后重试。'
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
        key = self._key(key)
        with self.store.connect() as db:
            value = db.execute(select(settings.c.value).where(settings.c.key == key)).scalar_one_or_none()
        return json.loads(value) if value else default

    def _write(self, key, value):
        key = self._key(key)
        with self.store.connect() as db:
            db.execute(self.store.db.upsert(settings, dict(key=key, value=json.dumps(value, ensure_ascii=False)), ['key']))

    def _key(self, key):
        fleet = self.store.fleet
        return f'i:{hashlib.sha256(fleet.id.encode()).hexdigest()[:16]}:{key}' if fleet else key

    def invalidate(self, *, preserve_block=False):
        self._write('youtube_auth_generation', uuid.uuid4().hex)
        self._write('youtube_auth_latest', {'state': 'unchecked', 'message': 'Cookie 或连接已更新，下载前将验证 YouTube 登录。'})
        if not preserve_block:
            self.clear_block()

    def clear_block(self):
        self._write('youtube_auth_block', None)

    def block_reason(self):
        return self._read('youtube_auth_block')

    def block(self, result):
        if self.store.fleet:
            self.store.fleet.control(self.store.fleet.id, {'action': 'drain'})
            self._write('youtube_auth_block', result)
            self.store.fleet.beat(result['message'])
            return
        with self.store.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='true'))
            value = json.dumps(result, ensure_ascii=False)
            db.execute(self.store.db.upsert(settings, dict(key='youtube_auth_block', value=value), ['key']))

    def status(self):
        result = self._read('youtube_auth_latest', {'state': 'unchecked', 'message': '尚未检测 YouTube 登录状态。'})
        if result['state'] == 'valid' and result.get('expires_at', 0) <= time.time():
            return {**result, 'state': 'unchecked', 'message': '登录验证已到期，下次下载前将重新检测。'}
        return result

    def check_profile(self, identity, config, base, *, recover=False, **options):
        from webapp.cookie_profiles import CookieProfiles
        profiles = CookieProfiles(base, self.store)
        target = profiles.require(identity)
        profile = next((item for item in profiles.items() if item['id'] == identity), None)
        fingerprint = hashlib.sha256(target.read_bytes()).hexdigest()
        pool, session = None, None
        if config.get('youtube_cookie_pool'):
            from webapp.cookie_pool import CookiePool
            pool = CookiePool(self.store, config, base, None, strict=False)
            canonical = pool.aliases.get(identity, identity)
            session = next((s for s in pool.status()['session_states'] if s['id'] == canonical), None)
            if recover and session and session['state'] == 'manual':
                options['force'] = True
        result = self.check({**config, 'youtube_cookie': str(target), '_cookie_pool_selected': identity}, base,
                            publish_latest=False, allow_manual=True, **options)
        from webapp.youtube_cookie_history import record as record_cookie_event
        record_cookie_event(self.store, profile_id=identity,
            profile_name=profile.get('name') if profile else identity, event_type='auth_check', phase='auth',
            kind=result.get('state'), message=result.get('message', ''),
            http_status=result.get('http_status'), retry_after=result.get('retry_after'),
            metadata={'connection': result.get('connection', '')})
        if result.get('http_status') == 429 or result['state'] in ('not_logged_in', 'invalid', 'verification_required'):
            self._limit_error({**config, '_cookie_pool_selected': identity}, base, result)
        if (pool and session and session['state'] == 'manual' and options.get('force')
                and result['state'] == 'valid'
                and hashlib.sha256(profiles.export(identity)[1]).hexdigest() == fingerprint):
            if pool.clear_account_after_check(identity, session.get('halt_incident_id'), result.get('checked_at', 0)):
                result = {**result, 'account_resumed': True,
                          'message': result['message'] + ' 该账号已恢复调度，原有限流冷却和启动设置仍生效。'}
        self._write('youtube_profile:' + identity, {'fingerprint': fingerprint, 'auth': result})
        return result

    def profile_status(self, identity, base):
        from webapp.cookie_profiles import CookieProfiles
        saved = self._read('youtube_profile:' + identity)
        if saved:
            try:
                data = CookieProfiles(base, self.store).require(identity).read_bytes()
            except (OSError, ValueError):
                return None
            if hashlib.sha256(data).hexdigest() == saved['fingerprint']:
                return saved['auth']
        return None

    def check_startup(self, config, base, **options):
        """Check an available pool session without bypassing account cooldowns."""
        if not config.get('youtube_cookie_pool'):
            return self.check(config, base, **options)
        from webapp.cookie_pool import CookiePool
        from webapp.throughput import PipelineBusy

        pool = CookiePool(self.store, config, base, None, strict=False)

        def waiting(status):
            if status['halted']:
                return {'state': 'blocked', 'message': status['reason'], 'source': 'cookie_pool_halted'}
            sessions = status['session_states']
            if sessions and all(s['state'] in ('manual', 'cooldown') for s in sessions) and any(
                    s['state'] == 'manual' for s in sessions):
                return {'state': 'waiting', 'message': status['capacity_summary']['reason']}
            if status['cooldown_until'] > time.time() or (
                    sessions and all(s['state'] == 'cooldown' for s in sessions)):
                until = max(status['cooldown_until'], min((s['cooldown_until'] for s in sessions), default=0))
                return {'state': 'waiting', 'message': '账号池正在冷却，启动后自动等待，到期按低频恢复。',
                        'cooldown_until': until}
            return None

        status = pool.status()
        blocked = waiting(status)
        if blocked:
            return blocked
        result = {'state': 'missing', 'message': '账号池没有可用会话，请检查 Cookie 配置。'}
        for session in status['session_states']:
            if session['state'] in ('cooldown', 'manual'):
                continue
            try:
                result = self.check_profile(session['id'], config, base, **options)
            except PipelineBusy:
                # Another task may start cooling this account after our snapshot.
                continue
            if result['state'] == 'valid':
                return {**result, 'profile_id': session['id']}
        # A failed login probe is authoritative for the account we just
        # checked.  ``check_profile`` records that failure in the pool, which
        # can make the aggregate status look like a generic wait (for example
        # when every member has been marked manual).  Preserve the actual
        # authentication result so callers can return the correct 409 error;
        # aggregate waiting remains appropriate for rate limits/cooldowns.
        if result.get('state') in ('not_logged_in', 'invalid', 'verification_required'):
            return result
        return waiting(pool.status()) or result

    def check(self, config, base, *, proxy=None, node='', force=False, publish_latest=True, allow_manual=False):
        if config.get('youtube_cookie_pool'):
            from webapp.cookie_pool import CookiePool
            from webapp.throughput import PipelineBusy
            pool = CookiePool(self.store, config, base, None, strict=False)
            selected = config.get('_cookie_pool_selected')
            target = Path(config.get('youtube_cookie') or '')
            if not target.is_absolute():
                target = Path(base) / target
            identity = selected or next((i for i, path, _ in pool.accounts
                if path.resolve() == target.resolve()), None)
            if identity is None and target.stem in pool.aliases:
                identity = target.stem
            identity = pool.aliases.get(identity, identity)
            session = next((s for s in pool.status()['session_states'] if s['id'] == identity), None)
            if session and session['state'] == 'manual' and not allow_manual:
                error = PipelineBusy('该账号需人工处理登录或验证，请在账号管理重新检测；其他健康账号继续。')
                error.retry_after = 60
                raise error
            if session and session.get('cooldown_until', 0) > time.time():
                error = PipelineBusy(f'账号「{session["name"]}」正在冷却，暂不发送登录检测请求；到期自动低频恢复。')
                error.retry_after = max(1, session['cooldown_until'] - time.time())
                raise error
        # Library checks must not overwrite the selected account's live status.
        generation = self._read('youtube_auth_generation', '')
        observed_data = None
        def publish(result):
            if publish_latest:
                self._write('youtube_auth_latest', result)
            from webapp.account_auth import publish_profile_auth
            publish_profile_auth(self.store, config, base, result, generation, observed_data)

        if proxy is None:
            proxy = '' if config.get('proxy_mode') == 'direct' else config.get('proxy', '')
        connection = ('Clash · ' + node) if node else '手动代理' if proxy else '直连'
        source = config.get('youtube_source_address', '') if config.get('proxy_mode') == 'direct' else ''
        if source:
            connection += ' · 源地址 ' + source
        result = {'state': 'missing', 'message': '请先登录 YouTube 或上传 Cookie，再开始下载。',
                  'connection': connection, 'checked_at': time.time()}
        try:
            # Older deployments could persist profiles before selecting one.
            # Use the first saved profile for this preflight as a safe fallback;
            # the settings endpoint also persists this repair for future jobs.
            if not config.get('youtube_cookie'):
                from webapp.cookie_profiles import CookieProfiles
                saved = CookieProfiles(base, self.store).items()
                if saved:
                    config = {**config, 'youtube_cookie': f"Cookies/profiles/{saved[0]['id']}.txt"}
            if not config.get('youtube_cookie'):
                raise FileNotFoundError
            from webapp.cookie_profiles import CookieProfiles
            CookieProfiles(base, self.store).restore_selected(config['youtube_cookie'])
            path = resolve_path(base, config['youtube_cookie'])
            with path.open('rb') as stream:
                data = stream.read(1024 * 1024 + 1)
            observed_data = data
            cookies = parse_youtube_cookie_file(data)
        except OSError:
            publish(result)
            return result
        except ValueError as error:
            from webapp.cookie_binding import CookieBindingConflict
            if isinstance(error, CookieBindingConflict):
                result.update(state='invalid', message=str(error))
                publish(result)
                return result
            result.update(state='invalid', message='Cookie 文件无效或已过期，请重新登录 YouTube 或上传新的 Cookie。')
            publish(result)
            return result
        auth = [cookie for cookie in cookies if cookie['name'] in AUTH_COOKIES and cookie['value']]
        if not auth:
            result.update(state='not_logged_in', message='Cookie 中没有 YouTube 登录凭据，请先登录或上传登录后的 Cookie。')
            publish(result)
            return result
        signature = hashlib.sha256(data + b'\0' + json.dumps([proxy, node, source, generation]).encode()).hexdigest()
        key = 'youtube_auth:' + signature
        deadline = time.monotonic() + 20
        while True:
            with worker_lock(self.directory / (signature + '.lock')) as acquired:
                if acquired:
                    cached = self._read(key)
                    if not force and cached and cached.get('expires_at', 0) > time.time():
                        publish(cached)
                        return cached
                    try:
                        checked = probe(data, proxy, **({'source_address': source} if source else {}))
                        state, message = checked
                        if getattr(checked, 'http_status', None):
                            result.update(http_status=checked.http_status, retry_after=checked.retry_after)
                        if getattr(checked, 'verification_reason', None):
                            result['verification_reason'] = checked.verification_reason
                    except Exception:
                        # A failed preflight must never become thousands of failed videos.
                        state, message = 'unconfirmed', '登录检测未完成，请检查网络和 Cookie 文件后重新检测。'
                    checked = time.time()
                    expiry = min([checked + 300, *[cookie['expires'] for cookie in auth if cookie['expires'] > 0]])
                    result.update(state=state, message=message, checked_at=checked,
                                  expires_at=expiry if state == 'valid' else checked + 15)
                    self._write(key, result)
                    publish(result)
                    return result
            if time.monotonic() >= deadline:
                result.update(state='checking', message='另一个进程正在检测 YouTube 登录，请稍后重试。')
                return result
            time.sleep(0.1)

    def _limit_error(self, config, base, result):
        error = YouTubeAuthRequired(result)
        if result.get('http_status'):
            from webapp.download_policy import failure_evidence
            failure_evidence(error, source='youtube', phase='auth')
        if config.get('youtube_cookie_pool'):
            from webapp.cookie_pool import CookiePool
            pool = CookiePool(self.store, config, base, None, strict=False)
            identity = config.get('_cookie_pool_selected')
            target = resolve_path(base, config.get('youtube_cookie') or '').resolve()
            if not identity:
                identity = next((i for i, path, _ in pool.accounts if path.resolve() == target), None)
                if identity is None and target.stem in pool.aliases:
                    identity = target.stem
            scope = next((scope for i, _, scope in pool.accounts
                          if i == pool.aliases.get(identity, identity)), None)
            if scope:
                pool.outcome(scope, error)
        return error

    def require(self, config, base, **options):
        result = self.check(config, base, **options)
        if result['state'] != 'valid':
            raise self._limit_error(config, base, result)
        return result

    def reject(self, message, *, request_mode='authenticated', node='', profile_id=None):
        if not profile_id:
            self.invalidate()
        if request_mode == 'anonymous_fallback':
            message = ('带 Cookie 的请求返回 Video unavailable；同一出口的匿名重试要求机器人验证。'
                       '这不能证明上传的 Cookie 已失效。请在同一出口重新检测登录和视频播放。')
        result = {'state': 'verification_required', 'message': message, 'checked_at': time.time(),
                  'source': 'download', 'request_mode': request_mode, 'node': node}
        if not profile_id:
            self._write('youtube_auth_latest', result)
        return result
