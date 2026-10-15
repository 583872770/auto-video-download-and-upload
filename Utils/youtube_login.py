"""Export only this tool's YouTube session to yt-dlp's Netscape cookie format."""
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
import tempfile


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
