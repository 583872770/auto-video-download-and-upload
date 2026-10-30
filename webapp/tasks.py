"""Durable task queue. A separate, exclusively locked process runs the pipeline."""
import codecs
import json
import logging
import os
from pathlib import Path
import shutil
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
    """Supervise download slots, one import/index lane and one subtitle lane."""
    def __init__(self, store, settings=None):
        self.store = store
        self.settings = settings or (lambda: {})
        self.process = None
        self.processes = {}
        self.last_started = {}
        self.output_threads = {}
        self.stop = threading.Event()
        self._wake = threading.Event()
        self._maintenance_config = {}
        self._settings_ready = threading.Event()
        from webapp.throughput import ThroughputController
        self.throughput = ThroughputController(self.store.directory)
        self.parser = None

    def wake(self):
        """Dispatch committed queue changes without waiting for the next poll."""
        self._wake.set()

    def start(self):
        threading.Thread(target=self._watch, name='video-task-dispatcher', daemon=True).start()

    def _watch(self):
        config = {}
        try:
            with (self.store.directory / 'disk-budget-config.json').open('rb') as stream:
                config = json.loads(stream.read(16384))
        except (OSError, ValueError):
            pass
        if not isinstance(config, dict):
            config = {}
        from webapp.diagnostics import runtime_log
        diagnostic_log = runtime_log(self.store.directory, config=config)
        self._maintenance_config = config
        maintenance = threading.Thread(target=self._maintain, args=(diagnostic_log,),
                                       name='video-task-maintenance', daemon=True)
        maintenance.start()
        try:
            self._dispatch(diagnostic_log)
        finally:
            self.stop.set()
            maintenance.join(timeout=2)
            if self.parser:
                self.parser.close()

    def _maintain(self, diagnostic_log):
        """Directory scans and retention must never hold up queue dispatch."""
        from webapp.diagnostics import safe_text
        from webapp.disk_budget import DiskBudget, GIB
        from webapp.maintenance import prune_disk_logs, cleanup_logs
        from web_worker import worker_lock
        last_disk = last_cleanup = last_log_cleanup = last_snapshot = last_error = 0
        while not self.stop.is_set():
            config = self._maintenance_config
            # Local log retention also works during a database outage.
            if time.monotonic() - last_log_cleanup >= 30:
                last_log_cleanup = time.monotonic()
                try:
                    prune_disk_logs(self.store, config, low_space=shutil.disk_usage(self.store.directory).free <
                                    int(config.get('disk_min_free_gb', 10)) * GIB)
                except Exception as error:
                    diagnostic_log.write((safe_text('日志清理检查失败：' + str(error))[:2000] + '\n').encode('utf-8'))
            if not self._settings_ready.is_set():
                self.stop.wait(1)
                continue
            config = self._maintenance_config
            try:
                disk = DiskBudget(self.store.directory, config)
                if time.monotonic() - last_disk >= 30:
                    last_disk = time.monotonic()
                    disk.cleanup(self.store)
                if self.stop.is_set():
                    break
                if time.monotonic() - last_snapshot >= 5:
                    last_snapshot = time.monotonic()
                    disk.publish(self.store, disk.snapshot())
                if time.monotonic() - last_cleanup >= 60:
                    last_cleanup = time.monotonic()
                    with worker_lock(self.store.directory / 'log-maintenance.lock') as acquired:
                        if acquired:
                            cleanup_logs(self.store, config.get('log_retention_days', 0))
            except Exception as error:
                if time.monotonic() - last_error >= 30:
                    diagnostic_log.write((safe_text('后台维护检查失败：' + str(error))[:2000] + '\n').encode('utf-8'))
                    last_error = time.monotonic()
            self.stop.wait(1)

    def _dispatch(self, diagnostic_log):
        from webapp.diagnostics import capture_output, error_details
        from webapp.disk_budget import GIB, resource_blocked
        last_recovery = last_error = last_beat = 0
        while not self.stop.is_set():
            # Clear before reading the queue so changes made during this tick
            # leave the event set and trigger another tick immediately.
            self._wake.clear()
            try:
                if time.time() - last_recovery > 15:
                    self.store.recover(expired_only=True)
                    last_recovery = time.time()
                config = self.settings()
                if self.store.fleet:
                    config = self.store.fleet.local_settings()['config']
                if self.throughput.state is None or time.monotonic() - last_beat >= 5:
                    from sqlalchemy import select
                    from webapp.database import tasks
                    with self.store.connect() as db:
                        statement = select(tasks.c.progress).where(tasks.c.status == 'running', tasks.c.action == 'download')
                        if self.store.fleet:
                            statement = statement.where(tasks.c.instance_id == self.store.fleet.id)
                        stages = [json.loads(value or '{}') for value in db.execute(statement).scalars()]
                    self.throughput.update(config, stages)
                    if self.store.fleet and not self.store.fleet.beat(throughput=self.throughput.state):
                        self.stop.set()
                        continue
                    last_beat = time.monotonic()
                self._maintenance_config = config
                self._settings_ready.set()
                from webapp.throughput import runtime_limits
                count = runtime_limits(config, self.store.directory)['workers']
                diagnostic_log.maximum = max(4096, min(256 * 1024, int(config.get('disk_log_budget_mb', 64)) * 1024 ** 2 // 128))
                for old_slot, previous in list(self.processes.items()):
                    code = previous.poll()
                    if code is not None:
                        diagnostic_log.write((json.dumps({'time': now(), 'event': 'worker_exit',
                            'slot': old_slot, 'pid': previous.pid, 'exit_code': code}, ensure_ascii=False) + '\n').encode('utf-8'))
                        self.processes.pop(old_slot, None)
                        if code == 0:
                            self.last_started.pop(old_slot, None)
                        old_thread = self.output_threads.pop(old_slot, None)
                        if old_thread:
                            old_thread.join(timeout=0.1)
                if self.store.downloads_stopped() or (self.store.fleet and self.store.fleet.should_stop()):
                    from webapp.control import stop_downloads
                    stop_downloads(self.store)
                role_counts = self.store.runnable_counts()
                roles = set(role_counts)
                if role_counts.get('download') and (not self.store.fleet or self.store.fleet.can_download):
                    roles.add('regular')
                # Reading free space and the last published budget is cheap.
                # Workers still reserve/check the actual budget before use.
                with self.store.connect() as db:
                    blocked = resource_blocked(self.store, db)
                if blocked or shutil.disk_usage(self.store.directory).free < int(config.get('disk_min_free_gb', 10)) * GIB:
                    roles.discard('download')
                    roles.discard('regular')
                if roles:
                    if (self.parser is None and config.get('shared_parser', True)
                            and 'bergung' in (config.get('download_methods') or [config.get('download_method')])):
                        from webapp.parser_broker import ParserBroker
                        self.parser = ParserBroker(self.store.directory, lambda: self._maintenance_config)
                        self.parser.start()
                    download_starts = role_counts.get('download', 0)
                    for slot in [-2, -1, *range(count)]:
                        role = 'subtitles' if slot == -2 else 'maintenance' if slot < 0 else 'regular' if slot == 0 else 'download'
                        if role not in roles:
                            continue
                        if role == 'download' and download_starts <= 0:
                            continue
                        previous = self.processes.get(slot)
                        if previous is not None and previous.poll() is None:
                            continue
                        if time.monotonic() - self.last_started.get(slot, 0) < 5:
                            continue
                        env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1',
                                   VIDEO_WORKER_DATABASE_URL=self.store.db.url)
                        # Every subprocess owns its own Engine. A download only
                        # needs its progress transaction and heartbeat in parallel.
                        env['VIDEO_DB_POOL_SIZE'] = os.environ.get('VIDEO_WORKER_DB_POOL_SIZE', '2')
                        env['VIDEO_DB_MAX_OVERFLOW'] = os.environ.get('VIDEO_WORKER_DB_MAX_OVERFLOW', '0')
                        from webapp.database import schema_token
                        env['VIDEO_WORKER_SCHEMA_READY'] = schema_token(self.store.db.url)
                        from webapp.disk_budget import safe_path
                        temporary = safe_path(self.store.directory / 'runtime/cache' / f'worker-{role if slot < 0 else slot}', self.store.directory)
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
                        if role in ('regular', 'download') and download_starts:
                            download_starts -= 1
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
            self._wake.wait(1)
