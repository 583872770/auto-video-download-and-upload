"""Cross-process route circuit breakers; never confuse missing videos with bad routes."""
from contextlib import contextmanager
import json
from pathlib import Path
import time

from webapp.throughput import PipelineBusy, _atomic


class ProxyCoolingDown(PipelineBusy):
    pass


def connection_failure(error):
    messages, seen = [], set()
    while error is not None and id(error) not in seen:
        if getattr(error, 'connection_failure', False):
            return True
        seen.add(id(error))
        messages.append(type(error).__name__ + ': ' + str(error))
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    text = ' '.join(messages).lower()
    return any(term in text for term in (
        'err_connection_closed', 'err_connection_reset', 'err_proxy_connection_failed',
        'err_tunnel_connection_failed', 'ssleoferror', 'sslerror', 'unexpected_eof_while_reading',
        'connection reset by peer', 'connection refused', 'connection aborted',
        'remote end closed connection', 'proxyerror', 'network is unreachable',
        'name or service not known', 'temporary failure in name resolution',
    ))


class ProxyHealth:
    def __init__(self, directory, clock=time.time):
        self.directory, self.clock = Path(directory), clock
        self.path = self.directory / 'route-health.json'

    def read(self):
        try:
            result = json.loads(self.path.read_text(encoding='utf-8'))
            return result if isinstance(result, dict) else {}
        except (OSError, ValueError):
            return {}

    @contextmanager
    def locked(self):
        from webapp.disk_budget import file_lock
        self.directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 2
        while True:
            with file_lock(self.directory / 'route-health.lock') as acquired:
                if acquired:
                    yield True
                    return
            if time.monotonic() >= deadline:
                yield False  # Diagnostics must not cause a download failure.
                return
            time.sleep(.02)

    def eligible(self, node):
        item = self.read().get(node, {})
        until = item.get('until', 0)
        return self.clock() >= until

    def record(self, node, *, error=None, transferred_bytes=0, elapsed=0):
        if error is not None and not connection_failure(error):
            return
        with self.locked() as acquired:
            if not acquired:
                return
            now, data = self.clock(), self.read()
            # Retention and cardinality stay bounded even after subscriptions change.
            data = {k:v for k,v in data.items() if now - v.get('updated', 0) < 7 * 86400}
            item = data.setdefault(node, {'failures': 0, 'until': 0})
            item['updated'] = now
            if error is None:
                item.update(failures=0, until=0, last_success=now)
                item['until'] = 0
                item['probe_until'] = 0
                if elapsed > 0 and transferred_bytes > 0:
                    speed = transferred_bytes / elapsed
                    item['bytes_per_second'] = round(item.get('bytes_per_second', speed) * .7 + speed * .3)
            else:
                if item.get('probe_until', 0):
                    item.update(until=now + 120, probe_until=0)
                if now - item.get('last_failure', 0) > 120:
                    item['failures'] = 0
                item['failures'] += 1
                item['last_failure'] = now
                if item['failures'] >= 3:
                    item['until'] = now + min(900, 120 * 2 ** min(3, item['failures'] - 3))
            if len(data) > 256:
                data = dict(sorted(data.items(), key=lambda kv:kv[1].get('updated', 0), reverse=True)[:256])
            _atomic(self.path, data)

    def select(self, nodes, start=0):
        candidates = [nodes[(start + offset) % len(nodes)] for offset in range(len(nodes))]
        with self.locked() as acquired:
            data, now = self.read(), self.clock()
            for node in candidates:
                item = data.get(node, {})
                if item.get('until', 0) > now or item.get('probe_until', 0) > now:
                    continue
                if item.get('until', 0) > 0:
                    if not acquired:
                        continue
                    # Only one task tries a recovering node at a time.
                    item['probe_until'] = now + 120
                    _atomic(self.path, data)
                return node
        raise ProxyCoolingDown('所有候选代理暂时冷却，等待线路恢复后继续。')
