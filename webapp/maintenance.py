"""Bounded log retention; media files are never removed by maintenance."""
from datetime import datetime, timedelta
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys
import threading
import time

from sqlalchemy import select

from webapp.database import tasks


class BoundedLog:
    """Keep draining child output even when logging runs out of disk space."""
    def __init__(self, path, maximum):
        self.path, self.maximum = Path(path), max(4096, int(maximum))
        self.offset_path = self.path.with_suffix('.offset')
        try:
            self.base = int(self.offset_path.read_text())
        except (OSError, ValueError):
            self.base = 0

    def write(self, data):
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
        except OSError:
            pass  # The task's structured result remains in the database.


@contextmanager
def bounded_task_log(path, maximum):
    """Capture Python AND native subprocess output, with bounded on-disk storage."""
    sys.stdout.flush()
    sys.stderr.flush()
    read_fd, write_fd = os.pipe()
    saved = [os.dup(1), os.dup(2)]
    writer = BoundedLog(path, maximum)
    def drain():
        with os.fdopen(read_fd, 'rb', buffering=0) as stream:
            while data := stream.read(65536):
                writer.write(data)
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


def prune_disk_logs(store, config):
    from webapp.disk_budget import MIB, size_of, safe_path
    paths = list((store.directory / 'logs').rglob('*.log')) + list(store.directory.glob('*.log'))
    paths = [p for p in paths if re.fullmatch('[0-9a-f]{32}', p.stem) and not p.is_symlink()]
    states = {}
    for offset in range(0, len(paths), 500):
        ids = [p.stem for p in paths[offset:offset + 500]]
        with store.connect() as db:
            states.update((r.id, dict(r._mapping)) for r in db.execute(select(tasks.c.id, tasks.c.status, tasks.c.finished_at).where(tasks.c.id.in_(ids))))
    total, entries = 0, []
    now = time.time()
    for path in paths:
        try:
            safe_path(path, store.directory)
            size = size_of(path) + size_of(path.with_suffix('.offset'))
            total += size
            row = states.get(path.stem, {})
            if row.get('status') == 'running':
                continue
            hours = int(config.get('disk_success_log_hours', 24)) if row.get('status') == 'succeeded' else int(config.get('disk_failure_log_days', 7)) * 24
            if config.get('log_retention_days'):
                hours = min(hours, int(config['log_retention_days']) * 24)
            modified = path.stat().st_mtime
            entries.append((modified, path, size, now - modified > hours * 3600))
        except (OSError, ValueError):
            continue
    # Leave room for every running slot's capped log plus round-off/metadata.
    maximum = int(config.get('disk_log_budget_mb', 512)) * MIB
    headroom = min(maximum // 2, 16 * min(int(config.get('max_log_mb', 2)) * MIB, maximum // 32))
    for modified, path, size, expired in sorted(entries):
        if not expired and total <= maximum - headroom:
            continue
        try:
            for suffix in ('.log', '.offset', '.offset.tmp'):
                path.with_suffix(suffix).unlink(missing_ok=True)
            if path.parent != store.directory:
                for parent in (path.parent, path.parent.parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        break
            total -= size
            with store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == path.stem).values(logs_pruned=1))
        except OSError:
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
        path = store.log_path(task_id)
        if path.is_file():
            path.unlink(missing_ok=True)
            path.with_suffix('.offset').unlink(missing_ok=True)
            removed += 1
    if ids:
        with store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id.in_(ids)).values(logs_pruned=1))
    return removed
