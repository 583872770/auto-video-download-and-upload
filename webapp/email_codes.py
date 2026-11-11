"""Read fresh Google email codes from the supported, account-specific inbox."""
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.async_api import Error as BrowserError


ORIGIN = 'https://fbvjdjcf.asia'
MAX_PAGE_BYTES = 512 * 1024
POLL_SECONDS = 30
WAIT_SECONDS = 180


class InboxUnavailable(ValueError):
    def __init__(self):
        super().__init__('邮件接码暂不可用，请在浏览器中手动填写验证码。')


def supported_source(value):
    try:
        address = urlsplit(value or '')
        return (address.scheme in ('http', 'https') and address.hostname == 'fbvjdjcf.asia'
                and not address.username and not address.password
                and address.port in (None, 80 if address.scheme == 'http' else 443)
                and address.path in ('', '/', '/login', '/inbox'))
    except (ValueError, TypeError):
        return False


def safe_url(value):
    """Session URLs stay in memory; redirects may never leave the provider."""
    address = urlsplit(value)
    if (address.scheme != 'https' or address.hostname != 'fbvjdjcf.asia'
            or address.port not in (None, 443) or address.username or address.password
            or address.path not in ('/', '/login', '/inbox')):
        raise InboxUnavailable()
    return value


@dataclass
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.walk()

    def text(self):
        if self.tag in ('script', 'style'):
            return ''
        return ' '.join(child.text() if isinstance(child, Element) else child for child in self.children)

    def has_class(self, *names):
        return bool(set(self.attrs.get('class', '').split()).intersection(names))


class InboxHTML(HTMLParser):
    VOID = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                      'link', 'meta', 'param', 'source', 'track', 'wbr'))

    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.root = Element('document')
        self.stack = [self.root]
        self.feed(body)
        self.close()

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 80:
            raise InboxUnavailable()
        node = Element(tag, dict(attrs))
        self.stack[-1].children.append(node)
        encoded = node.attrs.get('data-cfemail')
        if encoded:
            try:
                raw = bytes.fromhex(encoded)
                node.children.append(bytes(value ^ raw[0] for value in raw[1:]).decode('utf-8'))
            except (ValueError, UnicodeError, IndexError):
                pass
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def read_inbox(body, email, since):
    """Return known codes, fresh unambiguous Google codes, and a refresh URL."""
    if len(body.encode('utf-8')) > MAX_PAGE_BYTES:
        raise InboxUnavailable()
    nodes = list(InboxHTML(body).root.walk())
    headings = ' '.join(node.text() for node in nodes if node.tag in ('h1', 'h2')).lower()
    if email.lower() not in headings:
        raise InboxUnavailable()
    refresh = next((node.attrs['href'] for node in nodes if node.tag == 'a'
                    and 'href' in node.attrs and parse_qs(urlsplit(node.attrs['href']).query).get('refresh')), None)
    if not refresh:
        raise InboxUnavailable()
    known, candidates = set(), []
    for node in nodes:
        if not node.has_class('mail-card', 'verify-row'):
            continue
        text = ' '.join(node.text().split())
        code_nodes = [child for child in node.walk() if child.has_class('verify-code')
                      or 'data-copy-value' in child.attrs]
        values = set()
        for child in code_nodes:
            value = child.attrs.get('data-copy-value', child.text()).strip()
            if re.fullmatch(r'(?:G-)?[0-9]{6}', value):
                values.add(value.removeprefix('G-'))
        if not values:
            # Match a labelled code inside a Google mail, never arbitrary numbers.
            values.update(re.findall(r'(?i)(?:G-|verification code\s*[:：]?\s*|验证码(?:为|是)?\s*[:：]?\s*)([0-9]{6})(?![0-9])', text))
            values.update(re.findall(r'(?i)(?<![0-9])([0-9]{6})\s+is your (?:Google )?(?:verification|security) code\b', text))
        known.update(values)
        if len(values) != 1 or not re.search(r'(?i)\bgoogle\b|谷歌', text):
            continue
        timestamps = []
        for child in node.walk():
            if child.tag == 'time' and child.attrs.get('datetime'):
                try:
                    stamp = datetime.fromisoformat(child.attrs['datetime'].replace('Z', '+00:00'))
                    if stamp.tzinfo is not None:
                        timestamps.append(stamp.timestamp())
                except ValueError:
                    pass
        if timestamps and (max(timestamps) < since - 15 or max(timestamps) > time.time() + 120):
            continue
        candidates.append(next(iter(values)))
    # An unknown layout must not turn every six-digit page number into a code.
    if not any(node.has_class('mail-card', 'verify-row', 'verify-empty') for node in nodes):
        raise InboxUnavailable()
    return known, set(candidates), safe_url(urljoin(ORIGIN, refresh))


class EmailInbox:
    def __init__(self, request, email):
        self.request, self.email = request, email
        self.since = time.time()
        self.known, self.refresh = set(), None
        self.last_poll = None
        self.error = None

    async def fetch(self, url, *, form=None):
        for _ in range(4):
            safe_url(url)
            response = await (self.request.post(url, form=form, max_redirects=0) if form is not None
                              else self.request.get(url, max_redirects=0))
            try:
                if response.status in (301, 302, 303, 307, 308):
                    if form is not None and response.status in (307, 308):
                        raise InboxUnavailable()
                    location = response.headers.get('location')
                    if not location:
                        raise InboxUnavailable()
                    url, form = safe_url(urljoin(url, location)), None
                    continue
                if response.status != 200:
                    raise InboxUnavailable()
                length = response.headers.get('content-length', '')
                if length.isdigit() and int(length) > MAX_PAGE_BYTES:
                    raise InboxUnavailable()
                raw = await response.body()
                if len(raw) > MAX_PAGE_BYTES:
                    raise InboxUnavailable()
                return raw.decode('utf-8', errors='replace')
            finally:
                await response.dispose()
        raise InboxUnavailable()

    async def open(self):
        body = await self.fetch(ORIGIN + '/login?lang=zh', form={'email': self.email})
        known, _, self.refresh = read_inbox(body, self.email, self.since)
        # Populate the baseline from a real refresh before Google can send mail.
        body = await self.fetch(self.refresh)
        refreshed, _, self.refresh = read_inbox(body, self.email, self.since)
        self.known = known | refreshed
        self.last_poll = time.monotonic()

    async def poll(self):
        if self.error or not self.refresh:
            raise InboxUnavailable()
        if time.monotonic() - self.last_poll < POLL_SECONDS:
            return None
        self.last_poll = time.monotonic()
        body = await self.fetch(self.refresh)
        known, candidates, self.refresh = read_inbox(body, self.email, self.since)
        fresh = candidates - self.known
        self.known.update(known)
        if len(fresh) > 1:
            raise InboxUnavailable()
        return next(iter(fresh), None)


@asynccontextmanager
async def email_inbox(driver, credentials, config, report):
    if not credentials or not supported_source(credentials.get('code_url')):
        yield None
        return
    options = {'timeout': 10000, 'ignore_https_errors': False}
    if config.get('proxy'):
        options['proxy'] = {'server': config['proxy']}
    request = None
    inbox = None
    try:
        report('starting', '正在准备邮件接码，记录已有验证码…')
        try:
            request = await driver.request.new_context(**options)
            inbox = EmailInbox(request, credentials['email'])
            await inbox.open()
            report('starting', '邮件接码已准备好，已记录并排除已有验证码。')
        except (BrowserError, ValueError):
            # Browser/network errors can contain the private inbox session URL.
            if inbox:
                inbox.error = True
            report('starting', '邮件接码准备失败；邮箱、密码仍会自动填写，邮件验证需要手动接管。')
        yield inbox
    finally:
        if request is not None:
            await request.dispose()
