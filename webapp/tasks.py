"""Durable task queue. A separate, exclusively locked process runs the pipeline."""
import codecs
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = ('queued', 'running')


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


from webapp.store import TaskStore


class TaskRunner:
    """Supervise independent download slots and one import/index lane."""
    def __init__(self, store, settings=None):
        self.store = store
        self.settings = settings or (lambda: {})
        self.process = None
        self.processes = {}
        self.last_started = {}
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._watch, name='video-task-dispatcher', daemon=True).start()

    def _watch(self):
        last_recovery = 0
        last_error = 0
        last_cleanup = 0
        last_beat = 0
        while not self.stop.is_set():
            try:
                if time.time() - last_recovery > 15:
                    self.store.recover(expired_only=True)
                    last_recovery = time.time()
                config = self.settings()
                if self.store.fleet:
                    if time.monotonic() - last_beat >= 5:
                        if not self.store.fleet.beat():
                            self.stop.set()
                            continue
                        last_beat = time.monotonic()
                    config = self.store.fleet.local_settings()['config']
                count = max(1, min(16, int(config.get('worker_count', 1))))
                if time.monotonic() - last_cleanup >= 60:
                    from web_worker import worker_lock
                    from webapp.maintenance import cleanup_logs
                    with worker_lock(self.store.directory / 'log-maintenance.lock') as acquired:
                        if acquired:
                            cleanup_logs(self.store, config.get('log_retention_days', 0))
                    last_cleanup = time.monotonic()
                if self.store.downloads_paused() or (self.store.fleet and self.store.fleet.should_stop()):
                    from webapp.control import stop_downloads
                    stop_downloads(self.store)
                role_counts = self.store.runnable_counts()
                roles = set(role_counts)
                if self.store.fleet and self.store.fleet.role == 'manager':
                    roles.discard('download')
                    roles.discard('regular')
                if role_counts.get('download') and (not self.store.fleet or self.store.fleet.role == 'worker'):
                    roles.add('regular')
                if roles:
                    for slot in [-1, *range(count)]:
                        role = 'maintenance' if slot < 0 else 'regular' if slot == 0 else 'download'
                        if role not in roles:
                            continue
                        if role == 'download' and slot >= max(1, min(count, role_counts['download'])):
                            continue
                        previous = self.processes.get(slot)
                        if previous is not None and previous.poll() is None:
                            continue
                        if time.monotonic() - self.last_started.get(slot, 0) < 5:
                            continue
                        env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1',
                                   VIDEO_WORKER_DATABASE_URL=self.store.db.url)
                        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
                        process = subprocess.Popen(
                            [sys.executable, '-u', str(ROOT / 'web_worker.py'), '--state', str(self.store.directory),
                             '--slot', str(slot), '--role', role], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
                        self.processes[slot] = process
                        self.last_started[slot] = time.monotonic()
                        if slot == 0:
                            self.process = process
            except Exception:
                if time.monotonic() - last_error > 30:
                    # Avoid logging connection strings or database parameters.
                    logging.getLogger(__name__).error('下载调度器暂时不可用，30 秒内不重复记录；请检查数据库和进程状态。')
                    last_error = time.monotonic()
            self.stop.wait(1)
