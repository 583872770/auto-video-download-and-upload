"""Optional, disposable live progress; MySQL remains the execution authority."""
import hashlib
import json
import os
import time
import logging
import threading
from collections import Counter


class ProgressCache:
    def __init__(self, database_url, *, client=None):
        self.url = os.environ.get('REDIS_URL', '').strip()
        self.enabled = bool(self.url or client is not None)
        self.client = client
        self.prefix = 'frameflow:progress:v1:' + hashlib.sha256(database_url.encode()).hexdigest()[:24] + ':'
        self.retry_at = 0
        self.counts = Counter()
        self.lock = threading.Lock()
        self.last_warning = float('-inf')

    def _count(self, name, amount=1):
        with self.lock:
            self.counts[name] += amount

    def health(self):
        with self.lock:
            return {'enabled': self.enabled, 'scope': 'current_process',
                'backoff_seconds': round(max(0, self.retry_at-time.monotonic()), 1),
                'counters': dict(self.counts)}

    @staticmethod
    def attempt(row):
        return [row.get('worker_id'), row.get('started_at'), row.get('attempts')]

    def _call(self, method, *args, **kwargs):
        if not self.enabled or time.monotonic() < self.retry_at:
            if self.enabled:
                self._count('backoff_' + method)
            return None
        try:
            if self.client is None:
                import redis
                self.client = redis.Redis.from_url(self.url, decode_responses=True,
                    socket_connect_timeout=.2, socket_timeout=.2, max_connections=4,
                    retry_on_timeout=False)
            result = getattr(self.client, method)(*args, **kwargs)
            self._count('ok_' + method)
            return result
        except Exception as error:
            # Cache outages must not stop workers or hide durable SQL progress.
            # Back off so every task/card does not repeat a connection timeout.
            self.retry_at = time.monotonic() + 5
            self._count('error_' + method)
            if time.monotonic() - self.last_warning >= 60:
                self.last_warning = time.monotonic()
                logging.getLogger(__name__).warning('进度缓存暂不可用（%s），已回退数据库并退避 5 秒。', type(error).__name__)
            return None

    def publish(self, row, progress):
        if not self.enabled or not row.get('worker_id'):
            return False
        value = json.dumps(dict(attempt=self.attempt(row), updated_at=time.time(), progress=progress),
                           ensure_ascii=False)
        if len(value.encode('utf-8')) > 65536:
            return False
        return bool(self._call('set', self.prefix + row['id'], value, ex=120))

    def discard(self, identities):
        for start in range(0, len(identities), 100):
            self._call('delete', *(self.prefix + identity for identity in identities[start:start + 100]))

    def read(self, row):
        if not self._eligible(row):
            return None
        return self._validate(row, self._call('get', self.prefix + row['id']))

    def _eligible(self, row):
        if not self.enabled or row.get('status') != 'running' or not row.get('worker_id'):
            return False
        if row.get('lease_until') and row['lease_until'] <= time.time():
            self._count('reject_lease')
            return False
        return True

    def read_many(self, rows):
        eligible = [row for row in rows if self._eligible(row)]
        result = {}
        for start in range(0, len(eligible), 100):
            batch = eligible[start:start+100]
            values = self._call('mget', [self.prefix + row['id'] for row in batch])
            if values is None:
                continue
            for row, raw in zip(batch, values):
                value = self._validate(row, raw)
                if value:
                    result[row['id']] = value
        return result

    def _validate(self, row, raw):
        if raw is None:
            self._count('miss')
            return None
        try:
            value = json.loads(raw)
            durable = row.get('progress') or {}
            if isinstance(durable, str):
                durable = json.loads(durable)
            if value.get('attempt') != self.attempt(row):
                self._count('reject_attempt')
                return None
            if (not isinstance(value.get('progress'), dict) or not isinstance(value.get('updated_at'), (int, float))
                    or not 0 <= time.time() - value['updated_at'] <= 120):
                self._count('reject_shape_or_age')
                return None
            if durable.get('_reported_at', 0) > value['progress'].get('_reported_at', 0):
                self._count('reject_older_than_sql')
                return None
            self._count('accepted')
            return value
        except (ValueError, TypeError, AttributeError):
            self._count('reject_malformed')
            return None
