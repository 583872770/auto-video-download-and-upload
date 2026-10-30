"""Small, bounded diagnostics. Never persist credentials, responses or page dumps."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
import re
import threading


_context = ContextVar('diagnostic_context', default={})
_sink = None
MAX_LINE = 16384


def safe_text(value):
    text = re.sub(r'\x1b\[[0-9;]*m', '', str(value))
    # Strip userinfo and the ENTIRE query/fragment, including signed CDN URLs.
    def url(match):
        from urllib.parse import urlsplit, urlunsplit
        try:
            parsed = urlsplit(match[0])
            return urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1],
                               parsed.path, '[redacted]' if parsed.query else '', ''))
        except ValueError:
            return '[redacted URL]'
    text = re.sub(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"\']+', url, text)
    text = re.sub(r'(?im)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=][^\r\n]*',
                  r'\1: [redacted]', text)
    text = re.sub(r'''(?ix)(["']?\b(?:(?:[a-z][a-z0-9_]*[_-])?(?:password|passwd|token|secret)|api[_-]?key|access[_-]?key|signature|credential|youtube_cookie|cookie|set-cookie|authorization|proxy-authorization)\b["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;\}\]\r\n]+)''',
                  r'\1[redacted]', text)
    text = re.sub(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_./+=-]+', r'\1 [redacted]', text)
    return text


def safe_value(value, depth=0):
    if depth > 5:
        return '[omitted]'
    if isinstance(value, str):
        return safe_text(value)[:4096]
    if isinstance(value, dict):
        return {str(key)[:80]: ('[redacted]' if re.search(r'(?i)password|passwd|cookie|secret|token|authorization|api[_-]?key|access[_-]?key|signature|credential', str(key))
                               else safe_value(item, depth + 1)) for key, item in list(value.items())[:32]}
    if isinstance(value, (tuple, list)):
        return [safe_value(item, depth + 1) for item in value[:32]]
    return value


def error_details(error):
    causes, seen = [], set()
    current = error
    while current is not None and id(current) not in seen and len(causes) < 3:
        seen.add(id(current))
        detail = {'type': type(current).__name__, 'message': safe_text(str(current))[:1600]}
        if isinstance(getattr(current, 'errno', None), int):
            detail['errno'] = current.errno
        if current.args and type(current.args[0]) is int:
            detail['code'] = current.args[0]
        causes.append(detail)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return {'error_type': type(error).__name__, 'causes': causes}


class SafeOutput:
    """Redact complete lines across pipe reads; discard overlong lines as a unit."""
    def __init__(self, write, *, timestamp=False):
        self.write, self.timestamp = write, timestamp
        self.pending = b''
        self.dropping = False

    def feed(self, data):
        for part in re.split(b'([\r\n])', data):
            if part in (b'\r', b'\n'):
                if self.pending and not self.dropping:
                    self._line(self.pending)
                self.pending, self.dropping = b'', False
            elif not self.dropping:
                self.pending += part
                if len(self.pending) > MAX_LINE:
                    self.pending, self.dropping = b'', True
                    self._line(b'[oversized log line omitted]')

    def _line(self, data):
        prefix = datetime.now().astimezone().isoformat(timespec='milliseconds') + ' ' if self.timestamp else ''
        text = data.decode('utf-8', errors='replace')
        if text.startswith('[event] '):
            try:
                text = '[event] ' + json.dumps(safe_value(json.loads(text[8:])), ensure_ascii=False)
            except (ValueError, RecursionError):
                text = safe_text(text)
        else:
            text = safe_text(text)
        self.write((prefix + text + '\n').encode('utf-8', errors='replace'))

    def finish(self):
        if self.pending and not self.dropping:
            self._line(self.pending)
        self.pending = b''


def capture_output(stream, writer):
    def drain():
        output = SafeOutput(writer.write, timestamp=True)
        try:
            with stream:
                while data := stream.read1(65536):
                    if not isinstance(data, bytes):
                        break
                    output.feed(data)
        except OSError:
            pass
        finally:
            output.finish()
    thread = threading.Thread(target=drain, name='diagnostic-output', daemon=True)
    thread.start()
    return thread


def runtime_log(directory, slot='supervisor', config=None):
    from pathlib import Path
    from webapp.disk_budget import safe_path, MIB
    from webapp.maintenance import BoundedLog
    if str(slot) not in {'supervisor', 'maintenance', 'subtitles', *(str(number) for number in range(96))}:
        raise ValueError('无效的日志进程编号')
    root = Path(directory).resolve()
    path = safe_path(root / 'logs' / 'instance' / f'{slot}.log', root)
    budget = int((config or {}).get('disk_log_budget_mb', 64)) * MIB
    return BoundedLog(path, max(4096, min(256 * 1024, budget // 128)),
                      warning_path=root / 'log-write-warning.json')


def read_runtime_logs(directory):
    from pathlib import Path
    from webapp.disk_budget import safe_path
    root = Path(directory).resolve()
    sections = []
    # Fixed allowlist and 3 KiB per file: the response is bounded too.
    for slot in ['supervisor', 'maintenance', *range(96)]:
        try:
            path = safe_path(root / 'logs' / 'instance' / f'{slot}.log', root)
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 3072))
                data = stream.read(3072)
            sections.append(f'--- {slot} ---\n' + safe_text(data.decode('utf-8', errors='replace')))
        except (OSError, ValueError):
            continue
    return '\n\n'.join(sections)


@contextmanager
def active_task_log(directory, slot, task_id):
    """Fixed slot markers protect live logs even when MySQL is unavailable."""
    from pathlib import Path
    from webapp.disk_budget import safe_path, process_identity, file_lock
    root = Path(directory).resolve()
    name = 'subtitles' if slot == -2 else 'maintenance' if slot < 0 else str(slot)
    path = safe_path(root / 'logs/instance' / f'active-{name}.json', root)
    published = False
    try:
        with file_lock(root / 'log-maintenance.lock', blocking=True) as owned:
            if owned:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'task_id': task_id, 'owner': process_identity()}), encoding='utf-8')
                temporary.replace(path)
                published = True
    except OSError:
        pass
    try:
        yield published
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def active_log_ids(directory):
    from pathlib import Path
    from webapp.disk_budget import safe_path, alive
    root = Path(directory).resolve()
    identities = set()
    for name in ['maintenance', 'subtitles', *range(96)]:
        try:
            path = safe_path(root / 'logs/instance' / f'active-{name}.json', root)
            with path.open('rb') as stream:
                value = json.loads(stream.read(1024))
            if alive(value['owner']):
                if re.fullmatch('[0-9a-f]{32}', value['task_id']):
                    identities.add(value['task_id'])
            else:
                path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return identities


@contextmanager
def process_diagnostics(directory, slot):
    global _sink
    previous = _sink
    _sink = runtime_log(directory, 'subtitles' if slot == -2 else 'maintenance' if slot < 0 else slot)
    try:
        yield
    finally:
        _sink = previous


def configure_diagnostics(config):
    if _sink:
        from webapp.disk_budget import MIB
        _sink.maximum = max(4096, min(256 * 1024, int(config.get('disk_log_budget_mb', 64)) * MIB // 128))


@contextmanager
def diagnostic_context(**values):
    token = _context.set({**_context.get(), **values})
    try:
        yield
    finally:
        _context.reset(token)


def event(name, *, context=None, console=True, **fields):
    record = {'time': datetime.now().astimezone().isoformat(timespec='milliseconds'),
              'event': name, **_context.get(), **(context or {}), **fields}
    # Sanitize individual values so structured JSON remains parseable.
    line = '[event] ' + json.dumps(safe_value(record), ensure_ascii=False, default=str)
    if _sink:
        _sink.write((line + '\n').encode('utf-8', errors='replace'))
    if console:
        print(line, flush=True)
    return record
