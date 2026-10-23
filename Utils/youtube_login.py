"""Export only this tool's YouTube session to yt-dlp's Netscape cookie format."""
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
import tempfile
import time


def parse_youtube_cookie_file(data):
    """Validate a Netscape upload without putting cookie values in errors/logs."""
    if not data or len(data) > 1024 * 1024:
        raise ValueError('Cookie 文件不能为空，大小不能超过 1 MB。')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError as error:
        raise ValueError('请上传 UTF-8 编码的 Netscape Cookie 文本文件。') from error
    lines = text.splitlines()
    if not lines or lines[0].strip() not in ('# Netscape HTTP Cookie File', '# HTTP Cookie File'):
        raise ValueError('文件不是 Netscape 格式，请上传浏览器导出的 cookies.txt，而非 JSON。')
    cookies = []
    current_time = time.time()
    for line in lines[1:]:
        http_only = line.startswith('#HttpOnly_')
        if http_only:
            line = line[len('#HttpOnly_'):]
        elif not line.strip() or line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) != 7:
            raise ValueError('Cookie 文件格式错误：每条记录需要 7 个以制表符分隔的字段。')
        domain, subdomains, path, secure, expiry, name, value = fields
        if (not domain or domain != domain.strip() or subdomains not in ('TRUE', 'FALSE')
                or secure not in ('TRUE', 'FALSE') or not path.startswith('/') or not name
                or (subdomains == 'TRUE') != domain.startswith('.')
                or (expiry and (not expiry.isascii() or not expiry.isdigit()))
                or any('\x00' in field for field in fields)):
            raise ValueError('Cookie 文件包含无效记录，请重新导出 Netscape 格式。')
        domain = domain.lower()
        if domain.lstrip('.') != 'youtube.com' and not domain.endswith('.youtube.com'):
            continue
        expires = int(expiry) if expiry else 0
        if expires > 0 and expires <= current_time:
            continue
        cookies.append({'domain': domain, 'name': name, 'value': value, 'path': path,
                        'secure': secure == 'TRUE', 'httpOnly': http_only, 'expires': expires or -1})
    if not cookies:
        raise ValueError('文件中没有未过期的 YouTube Cookie，请登录 YouTube 后重新导出。')
    return cookies


def save_youtube_cookies(cookies, target):
    jar = MozillaCookieJar()
    for cookie in cookies:
        domain = cookie['domain'].lower()
        if domain.lstrip('.') != 'youtube.com' and not domain.endswith('.youtube.com'):
            continue
        expires = cookie.get('expires', -1)
        jar.set_cookie(Cookie(
            version=0, name=cookie['name'], value=cookie['value'], port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith('.'), domain_initial_dot=domain.startswith('.'),
            path=cookie.get('path', '/'), path_specified=True, secure=cookie.get('secure', False),
            expires=int(expires) if expires > 0 else None, discard=expires <= 0,
            comment=None, comment_url=None, rest={'HTTPOnly': ''} if cookie.get('httpOnly') else {},
            rfc2109=False,
        ))
    if not len(jar):
        raise ValueError('未取得 YouTube Cookie，未覆盖已有文件')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.youtube-', suffix='.tmp', delete=False) as file:
            temporary = Path(file.name)
        jar.save(str(temporary), ignore_discard=True, ignore_expires=True)
        temporary.replace(target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
