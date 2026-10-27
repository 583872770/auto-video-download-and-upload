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
        self.output_threads = {}
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._watch, name='video-task-dispatcher', daemon=True).start()

    def _watch(self):
        last_recovery = 0
        last_error = 0
        last_cleanup = 0
        last_beat = 0
        last_disk = 0
        last_log_cleanup = 0
        config = {}
        try:
            with (self.store.directory / 'disk-budget-config.json').open('rb') as stream:
                config = json.loads(stream.read(16384))
        except (OSError, ValueError):
            pass
        if not isinstance(config, dict):
            config = {}
        from webapp.diagnostics import runtime_log, capture_output, safe_text, error_details
        diagnostic_log = runtime_log(self.store.directory, config=config)
        while not self.stop.is_set():
            # Run before database/heartbeat work: a SQL outage must not turn off
            # retention of local process/task logs.
            if time.monotonic() - last_log_cleanup >= 30:
                last_log_cleanup = time.monotonic()
                try:
                    from webapp.maintenance import prune_disk_logs
                    from webapp.disk_budget import GIB
                    import shutil
                    prune_disk_logs(self.store, config, low_space=shutil.disk_usage(self.store.directory).free <
                                    int(config.get('disk_min_free_gb', 10)) * GIB)
                except Exception as error:
                    diagnostic_log.write((safe_text('日志清理检查失败：' + str(error))[:2000] + '\n').encode('utf-8'))
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
                diagnostic_log.maximum = max(4096, min(256 * 1024, int(config.get('disk_log_budget_mb', 64)) * 1024 ** 2 // 128))
                for old_slot, previous in list(self.processes.items()):
                    code = previous.poll()
                    if code is not None:
                        diagnostic_log.write((json.dumps({'time': now(), 'event': 'worker_exit',
                            'slot': old_slot, 'pid': previous.pid, 'exit_code': code}, ensure_ascii=False) + '\n').encode('utf-8'))
                        self.processes.pop(old_slot, None)
                        old_thread = self.output_threads.pop(old_slot, None)
                        if old_thread:
                            old_thread.join(timeout=0.1)
                from webapp.disk_budget import DiskBudget
                disk = DiskBudget(self.store.directory, config)
                if time.monotonic() - last_disk >= 30:
                    disk.cleanup(self.store)
                    last_disk = time.monotonic()
                disk_state = disk.snapshot()
                disk.publish(self.store, disk_state)
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
                if role_counts.get('download') and (not self.store.fleet or self.store.fleet.can_download):
                    roles.add('regular')
                if disk_state['low_space']:
                    roles.discard('download')
                    roles.discard('regular')
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
                        from webapp.disk_budget import safe_path
                        temporary = safe_path(self.store.directory / 'runtime/cache' / f'worker-{"maintenance" if slot < 0 else slot}', self.store.directory)
                        temporary.mkdir(parents=True, exist_ok=True)
                        env.update(TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary))
                        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
                        process = subprocess.Popen(
                            [sys.executable, '-u', str(ROOT / 'web_worker.py'), '--state', str(self.store.directory),
                             '--slot', str(slot), '--role', role], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
                        self.output_threads[slot] = capture_output(process.stdout, diagnostic_log)
                        diagnostic_log.write((json.dumps({'time': now(), 'event': 'worker_start',
                            'slot': slot, 'pid': process.pid, 'role': role}) + '\n').encode('utf-8'))
                        self.processes[slot] = process
                        self.last_started[slot] = time.monotonic()
                        if slot == 0:
                            self.process = process
            except Exception as error:
                if time.monotonic() - last_error > 30:
                    # Avoid logging connection strings or database parameters.
                    details = error_details(error)
                    diagnostic_log.write((json.dumps({'time': now(), 'event': 'scheduler_error', **details}, ensure_ascii=False) + '\n').encode('utf-8'))
                    logging.getLogger(__name__).error('下载调度器暂时不可用（%s），详情见实例日志；30 秒内不重复记录。', details['error_type'])
                    last_error = time.monotonic()
            self.stop.wait(1)
