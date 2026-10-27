"""Bounded log retention; media files are never removed by maintenance."""
from datetime import datetime, timedelta
from contextlib import contextmanager
import os
import json
from pathlib import Path
import re
import sys
import threading
import time

from sqlalchemy import select

from webapp.database import tasks


class BoundedLog:
    """Keep draining child output even when logging runs out of disk space."""
    def __init__(self, path, maximum, *, warning_path=None):
        self.path, self.maximum = Path(path), max(4096, int(maximum))
        self.lock = threading.Lock()
        self.warning_path = Path(warning_path) if warning_path else None
        self.last_warning = float('-inf')
        self.offset_path = self.path.with_suffix('.offset')
        try:
            self.base = int(self.offset_path.read_text())
        except (OSError, ValueError):
            self.base = 0

    def write(self, data):
        with self.lock:
            self._write(data)

    def _write(self, data):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a+b') as output:
                length = output.seek(0, 2)
                if length + len(data) > self.maximum:
                    keep = max(0, self.maximum // 2 - min(len(data), self.maximum // 2))
                    output.seek(max(0, length - keep))
                    tail = output.read(keep)
                    output.seek(0)
                    output.truncate()
                    output.write(tail)
                    self.base += length - len(tail) + max(0, len(data) - self.maximum // 2)
                    temporary = self.offset_path.with_suffix('.offset.tmp')
                    temporary.write_text(str(self.base), encoding='ascii')
                    temporary.replace(self.offset_path)
                    data = data[-self.maximum // 2:]
                output.write(data)
        except OSError as error:
            # One fixed-size status file, never a growing secondary error log.
            if self.warning_path and time.monotonic() - self.last_warning >= 60:
                self.last_warning = time.monotonic()
                try:
                    self.warning_path.write_text(json.dumps({'time': time.time(), 'errno': error.errno,
                        'message': '日志写入失败，部分诊断可能缺失；请检查磁盘空间和目录权限。'}), encoding='utf-8')
                except OSError:
                    pass  # An entirely full/read-only disk may prevent even the status write.


@contextmanager
def bounded_task_log(path, maximum, *, warning_path=None):
    """Capture Python AND native subprocess output, with bounded on-disk storage."""
    sys.stdout.flush()
    sys.stderr.flush()
    read_fd, write_fd = os.pipe()
    saved = [os.dup(1), os.dup(2)]
    writer = BoundedLog(path, maximum, warning_path=warning_path)
    from webapp.diagnostics import SafeOutput
    def drain():
        output = SafeOutput(writer.write, timestamp=True)
        with os.fdopen(read_fd, 'rb', buffering=0) as stream:
            while data := stream.read(65536):
                output.feed(data)
        output.finish()
    thread = threading.Thread(target=drain, name='bounded-task-log', daemon=True)
    thread.start()
    try:
        os.dup2(write_fd, 1)
        os.dup2(write_fd, 2)
        os.close(write_fd)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        for target, source in zip((1, 2), saved):
            os.dup2(source, target)
            os.close(source)
        thread.join(timeout=10)


def prune_disk_logs(store, config, *, low_space=False):
    from webapp.disk_budget import file_lock
    # Serialize cleanup with publication of active slot markers. A just-claimed
    # task must not start writing into a file selected for deletion moments ago.
    with file_lock(store.directory / 'log-maintenance.lock') as owned:
        if owned:
            _prune_disk_logs(store, config, low_space=low_space)


def _prune_disk_logs(store, config, *, low_space=False):
    from webapp.disk_budget import MIB, size_of, safe_path
    paths = list((store.directory / 'logs').rglob('*.log')) + list(store.directory.glob('*.log'))
    paths = [p for p in paths if re.fullmatch('[0-9a-f]{32}', p.stem) and not p.is_symlink()]
    from webapp.diagnostics import active_log_ids
    active = active_log_ids(store.directory)
    states = {}
    database_available = True
    for offset in range(0, len(paths), 500):
        ids = [p.stem for p in paths[offset:offset + 500]]
        try:
            with store.connect() as db:
                states.update((r.id, dict(r._mapping)) for r in db.execute(select(tasks.c.id, tasks.c.status, tasks.c.finished_at).where(tasks.c.id.in_(ids))))
        except Exception:
            # Database outages must not disable local capacity/age cleanup.
            database_available = False
            break
    entries = []
    now = time.time()
    for path in paths:
        try:
            safe_path(path, store.directory)
            size = size_of(path) + size_of(path.with_suffix('.offset')) + size_of(path.with_suffix('.offset.tmp'))
            row = states.get(path.stem, {})
            modified = path.stat().st_mtime
            if path.stem in active or row.get('status') == 'running' or (not database_available and now - modified < 120):
                continue
            hours = int(config.get('disk_success_log_hours', 24)) if row.get('status') == 'succeeded' else int(config.get('disk_failure_log_days', 7)) * 24
            if config.get('log_retention_days'):
                hours = min(hours, int(config['log_retention_days']) * 24)
            entries.append((modified, path, size, now - modified > hours * 3600))
        except (OSError, ValueError):
            continue
    # Leave room for every running slot's capped log plus round-off/metadata.
    maximum = int(config.get('disk_log_budget_mb', 64)) * MIB
    # Instance logs have fixed names (18 at most), bounded writers and age expiry.
    # They share this budget with task logs; no per-attempt diagnostic files.
    for path in (store.directory / 'logs' / 'instance').glob('*.log'):
        if not re.fullmatch(r'(supervisor|maintenance|\d{1,2})', path.stem):
            continue
        try:
            safe_path(path, store.directory)
            expired = now - path.stat().st_mtime > min(7, int(config.get('disk_failure_log_days', 7))) * 86400
            if expired:
                path.unlink(missing_ok=True)
                path.with_suffix('.offset').unlink(missing_ok=True)
            else:
                # Enforce reduced configuration on idle logs too.
                cap = max(4096, min(256 * 1024, maximum // 128))
                if now - path.stat().st_mtime > 120 and path.stat().st_size > cap:
                    with path.open('r+b') as stream:
                        stream.seek(-cap, 2)
                        tail = stream.read(cap)
                        stream.seek(0)
                        stream.write(tail)
                        stream.truncate()
        except (OSError, ValueError):
            continue
    # Include allocated filesystem blocks and shard directories, not just text
    # lengths: tiny per-task files can otherwise exhaust a small disk via metadata.
    total = size_of(store.directory / 'logs') + sum(
        size_of(path) + size_of(path.with_suffix('.offset')) + size_of(path.with_suffix('.offset.tmp'))
        for path in paths if path.parent == store.directory)
    headroom = maximum // 2
    retained = len(paths)
    removed_ids = []
    for modified, path, size, expired in sorted(entries):
        if not low_space and not expired and total <= maximum - headroom and retained <= 1024:
            continue
        try:
            for suffix in ('.log', '.offset', '.offset.tmp'):
                path.with_suffix(suffix).unlink(missing_ok=True)
            directory_bytes = 0
            if path.parent != store.directory:
                for parent in (path.parent, path.parent.parent):
                    if parent == store.directory / 'logs' or not parent.is_relative_to(store.directory / 'logs'):
                        break
                    try:
                        safe_path(parent, store.directory)
                        metadata = parent.stat()
                        amount = max(4096, metadata.st_size, getattr(metadata, 'st_blocks', 0) * 512)
                        parent.rmdir()
                        directory_bytes += amount
                    except OSError:
                        break
            total -= size + directory_bytes
            retained -= 1
            removed_ids.append(path.stem)
        except OSError:
            continue
    if database_available:
        for offset in range(0, len(removed_ids), 500):
            try:
                with store.connect() as db:
                    db.execute(tasks.update().where(tasks.c.id.in_(removed_ids[offset:offset + 500])).values(logs_pruned=1))
            except Exception:
                break
    # A crash can leave offsets without a log. Their count must also stay bounded.
    for root in (store.directory, store.directory / 'logs'):
        entries = root.glob('*.offset*') if root == store.directory else root.rglob('*.offset*')
        for path in entries:
            try:
                safe_path(path, store.directory)
                if not path.with_suffix('.log').exists() and now - path.stat().st_mtime > 120:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue
    # Reclaim empty shards left by crashes or log reads after retention.
    for first in (store.directory / 'logs').glob('*'):
        if not re.fullmatch('[0-9a-f]{2}', first.name):
            continue
        for path in [*first.glob('[0-9a-f][0-9a-f]'), first]:
            try:
                safe_path(path, store.directory)
                if now - path.stat().st_mtime > 120:
                    path.rmdir()
            except (OSError, ValueError):
                continue


def trim_log(store, task_id, max_mb=10):
    path = store.log_path(task_id)
    maximum = int(max_mb) * 1024 ** 2
    if not path.is_file() or path.stat().st_size <= maximum:
        return
    with path.open('rb') as source:
        source.seek(-maximum, 2)
        tail = source.read(maximum)
    with path.open('wb') as target:
        target.write(b'[Earlier log output removed by retention policy]\n')
        target.write(tail)


def cleanup_logs(store, days, limit=500):
    if not days:
        return 0
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec='seconds')
    with store.connect() as db:
        statement = select(tasks.c.id).where(tasks.c.logs_pruned == 0, tasks.c.finished_at < cutoff,
                         tasks.c.status.in_(('succeeded', 'failed', 'interrupted')))
        if store.fleet:
            statement = statement.where(tasks.c.instance_id == store.fleet.id)
        ids = db.execute(statement.order_by(tasks.c.finished_at).limit(limit)).scalars().all()
    removed = 0
    for task_id in ids:
        path = store.log_path(task_id, create=False)
        if path.is_file():
            path.unlink(missing_ok=True)
            path.with_suffix('.offset').unlink(missing_ok=True)
            removed += 1
    if ids:
        with store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id.in_(ids)).values(logs_pruned=1))
    return removed
