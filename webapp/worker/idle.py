"""Reuse local processing workers without retaining a task or transaction."""
import json
from pathlib import Path
import time

from sqlalchemy import select
from webapp.database import settings


PROCESSING_IDLE_SECONDS = 60
POLL_SECONDS = 2
HEARTBEAT_SECONDS = 10
WAKE_SECONDS = .25


def notify_processing(directory):
    """A local hint after commit; polling also covers retries and missed hints."""
    try:
        (Path(directory) / 'processing-wakeup').touch()
    except OSError:
        pass


class ProcessingIdle:
    def __init__(self, store, slot, name, *, idle_seconds=None, clock=None, sleep=None):
        self.store, self.slot, self.name = store, slot, name
        self.idle_seconds = PROCESSING_IDLE_SECONDS if idle_seconds is None else idle_seconds
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.signal = store.directory / 'processing-wakeup'
        self.token = self._token()
        self.idle_since = None
        self.checked_at = self.beat_at = float('-inf')

    def _token(self):
        try:
            return self.signal.stat().st_mtime_ns
        except OSError:
            return None

    def busy(self):
        self.idle_since = None

    def allowed(self):
        config = self.configuration()
        return config is not None and max(0, -self.slot - 2) < max(1, min(8, int(config.get('postprocess_limit', 2))))

    def configuration(self):
        # Only read control/configuration, never take the admission/queue locks.
        with self.store.connect() as db:
            if self.store.fleet:
                fleet = self.store.fleet
                row = fleet.row(db)
                if row['paused'] or row['stop_requested'] or row['boot_id'] != fleet.boot_id:
                    return None
                config = {**json.loads(row['config']).get('config', {}), **fleet.resource_limits(row)}
            else:
                values = dict(db.execute(select(settings.c.key, settings.c.value).where(
                    settings.c.key.in_(('downloads_paused', 'runtime')))).all())
                if values.get('downloads_paused') == 'true':
                    return None
                config = json.loads(values.get('runtime') or '{}')
        return config

    def wait(self):
        now = self.clock()
        if self.idle_since is None:
            self.idle_since = now
        deadline = self.idle_since + self.idle_seconds
        next_poll = now + POLL_SECONDS
        while now < deadline:
            if now - self.checked_at >= POLL_SECONDS:
                if not self.allowed():
                    return False
                self.checked_at = now
            if now - self.beat_at >= HEARTBEAT_SECONDS:
                self.store.worker_status(self.store.owner, self.name, 'idle')
                self.beat_at = now
            token = self._token()
            if token != self.token:
                self.token = token
                return True
            if now >= next_poll:
                return True
            self.sleep(min(WAKE_SECONDS, deadline - now, next_poll - now))
            now = self.clock()
        return False


class DownloadIdle(ProcessingIdle):
    """Reuse an idle network worker; every task still has isolated context."""
    def allowed(self):
        config = self.configuration()
        if config is None:
            return False
        from webapp.throughput import runtime_limits
        return self.slot < runtime_limits(config, self.store.directory)['workers']
