"""Optional, disposable live progress; MySQL remains the execution authority."""
import hashlib
import json
import os
import time


class ProgressCache:
    def __init__(self, database_url, *, client=None):
        self.url = os.environ.get('REDIS_URL', '').strip()
        self.enabled = bool(self.url or client is not None)
        self.client = client
        self.prefix = 'frameflow:progress:v1:' + hashlib.sha256(database_url.encode()).hexdigest()[:24] + ':'
        self.retry_at = 0

    @staticmethod
    def attempt(row):
        return [row.get('worker_id'), row.get('started_at'), row.get('attempts')]

    def _call(self, method, *args, **kwargs):
        if not self.enabled or time.monotonic() < self.retry_at:
            return None
        try:
            if self.client is None:
                import redis
                self.client = redis.Redis.from_url(self.url, decode_responses=True,
                    socket_connect_timeout=.2, socket_timeout=.2, max_connections=4,
                    retry_on_timeout=False)
            return getattr(self.client, method)(*args, **kwargs)
        except Exception:
            # Cache outages must not stop workers or hide durable SQL progress.
            # Back off so every task/card does not repeat a connection timeout.
            self.retry_at = time.monotonic() + 5
            return None

    def publish(self, row, progress):
        if not self.enabled or not row.get('worker_id'):
            return False
        value = json.dumps(dict(attempt=self.attempt(row), updated_at=time.time(), progress=progress),
                           ensure_ascii=False)
        if len(value.encode('utf-8')) > 65536:
            return False
        return bool(self._call('set', self.prefix + row['id'], value, ex=120))

    def read(self, row):
        if not self.enabled or row.get('status') != 'running' or not row.get('worker_id'):
            return None
        if row.get('lease_until') and row['lease_until'] <= time.time():
            return None
        raw = self._call('get', self.prefix + row['id'])
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            durable = row.get('progress') or {}
            if isinstance(durable, str):
                durable = json.loads(durable)
            if (value.get('attempt') != self.attempt(row) or not isinstance(value.get('progress'), dict)
                    or not isinstance(value.get('updated_at'), (int, float))
                    or not 0 <= time.time() - value['updated_at'] <= 120
                    or durable.get('_reported_at', 0) > value['progress'].get('_reported_at', 0)):
                return None
            return value
        except (ValueError, TypeError, AttributeError):
            return None
