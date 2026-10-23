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
        while not self.stop.is_set():
            try:
                if time.time() - last_recovery > 15:
                    self.store.recover(expired_only=True)
                    last_recovery = time.time()
                config = self.settings()
                count = max(1, min(16, int(config.get('worker_count', 1))))
                if self.store.pending():
                    for slot in [-1, *range(count)]:
                        previous = self.processes.get(slot)
                        if previous is not None and previous.poll() is None:
                            continue
                        if time.monotonic() - self.last_started.get(slot, 0) < 5:
                            continue
                        role = 'maintenance' if slot < 0 else 'regular' if slot == 0 else 'download'
                        env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1')
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
