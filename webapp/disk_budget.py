"""Instance-local, crash-safe reservations for disposable files (software limits)."""
from contextlib import contextmanager
import errno
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid

import psutil

GIB = 1024 ** 3
MIB = 1024 ** 2
COORDINATION_TIMEOUT = 5
USAGE_INTERVAL = 5
DISK_DEFAULTS = {
    'disk_budget_gb': 20, 'disk_test_budget_gb': 16,
    'disk_browser_budget_mb': 1024, 'disk_log_budget_mb': 64,
    'disk_import_budget_mb': 1024, 'disk_cache_budget_mb': 256,
    'disk_min_free_gb': 10, 'disk_resume_free_gb': 15,
    'disk_upload_limit_mb': 256, 'disk_success_log_hours': 24,
    'disk_failure_log_days': 7,
}
DISK_FIELDS = set(DISK_DEFAULTS)


class DiskPressure(RuntimeError):
    """A recoverable resource condition, never a provider/video failure."""


class DiskCoordinationBusy(DiskPressure):
    """Local accounting could not be checked within the bounded lock wait."""


def is_disk_full(error):
    return isinstance(error, OSError) and error.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', 122))


def safe_path(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('临时文件路径超出受控目录。')
    if root.is_symlink() or root.is_junction():
        raise ValueError('临时根目录不能是链接。')
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError('临时目录不能包含链接。')
    if root.resolve() != root or path.resolve() != path:
        raise ValueError('临时文件路径包含链接。')
    return path


def size_of(path, *, measured=None):
    """Count entry bytes; only traverse directories, never links or sockets."""
    path = Path(path)
    try:
        if path.is_junction():
            return 0
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return 0
        total = max(metadata.st_size, getattr(metadata, 'st_blocks', 0) * 512)
        # Chromium creates filesystem sockets alongside its temporary files.
        # FIFOs and device nodes likewise have metadata but no children.
        if stat.S_ISDIR(metadata.st_mode):
            total = max(total, 4096)
            with os.scandir(path) as entries:
                for entry in entries:
                    total += size_of(entry.path, measured=measured)
        if measured is not None and path in measured:
            # Credit precisely the bytes included in this category traversal.
            # A separate later lease scan could subtract bytes not counted yet.
            measured[path] = total
        return total
    except (FileNotFoundError, NotADirectoryError):
        # Browser temp entries can disappear or change type during a scan.
        # Other I/O errors still surface instead of silently undercounting.
        if measured is not None:
            for target in measured:
                if target == path or target.is_relative_to(path):
                    measured[target] = 0
        return 0


@contextmanager
def file_lock(path, *, blocking=False):
    """Hold a cross-process lock for the entire operation, including deletion."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        deadline = time.monotonic() + (15 if blocking else 0)
        while True:
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        try:
            # Windows permits locking beyond EOF. Initialize only after taking
            # that lock: a competing initializer must never write a locked byte.
            if path.stat().st_size == 0:
                stream.write(b'0')
                stream.flush()
            yield True
        finally:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def process_identity(pid=None):
    process = psutil.Process(pid or os.getpid())
    return {'pid': process.pid, 'created': process.create_time()}


def alive(identity):
    try:
        return abs(psutil.Process(identity['pid']).create_time() - identity['created']) < 0.01
    except (psutil.NoSuchProcess, ValueError):
        return False
    except (psutil.AccessDenied, KeyError, TypeError):
        return True  # Unknown ownership must never authorize deletion.


def resource_blocked(store, db, identity=None):
    identity = identity or (store.fleet.id if store.fleet else 'local')
    return identity in resource_blocks(db, [identity])


def resource_blocks(db, identities):
    from sqlalchemy import select
    from webapp.database import settings
    rows = db.execute(select(settings.c.key, settings.c.value).where(settings.c.key.in_(['disk:' + identity for identity in identities])))
    blocked = set()
    for key, value in rows:
        state = json.loads(value)
        if state.get('blocked') and time.time() - state.get('time', 0) < 90:
            blocked.add(key.removeprefix('disk:'))
    return blocked


class DiskBudget:
    def __init__(self, directory, config=None):
        self.directory = Path(directory).resolve()
        self.config = {**DISK_DEFAULTS, **(config or {})}
        self.runtime = safe_path(self.directory / 'runtime', self.directory)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.database = safe_path(self.directory / 'disk-budget.sqlite3', self.directory)
        with self.connect(write=False) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'leases', 'state', 'requirements'} <= tables:
            return
        with self.connect() as db:
            for statement in '''
                CREATE TABLE IF NOT EXISTS leases (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, path TEXT NOT NULL,
                    task TEXT, owner TEXT NOT NULL, children TEXT NOT NULL DEFAULT '[]',
                    reserved INTEGER NOT NULL, measured INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requirements (
                    task TEXT PRIMARY KEY, version TEXT NOT NULL, needed INTEGER NOT NULL,
                    interruptions INTEGER NOT NULL, free INTEGER NOT NULL);
            '''.split(';'):
                if statement.strip():
                    db.execute(statement)

    @contextmanager
    def connect(self, *, write=True):
        # SQLite's busy handler retries transient locks within this deadline.
        db = sqlite3.connect(self.database, timeout=COORDINATION_TIMEOUT)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('PRAGMA max_page_count=32768')  # Bound metadata to 128 MiB.
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except sqlite3.OperationalError as error:
            db.rollback()
            if 'full' in str(error).lower():
                raise DiskPressure('本机磁盘协调记录或磁盘空间不足，等待清理。') from error
            if (getattr(error, 'sqlite_errorcode', 0) & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or any(
                    text in str(error).lower() for text in ('database is locked', 'database table is locked', 'database schema is locked')):
                raise DiskCoordinationBusy('本机磁盘协调繁忙，等待后自动重试。') from error
            raise
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def limits(self):
        return {'test': int(self.config['disk_test_budget_gb']) * GIB,
                'browsers': int(self.config['disk_browser_budget_mb']) * MIB,
                'logs': int(self.config['disk_log_budget_mb']) * MIB,
                'imports': int(self.config['disk_import_budget_mb']) * MIB,
                'cache': int(self.config['disk_cache_budget_mb']) * MIB}

    def roots(self):
        return {'test': [self.directory / 'test-downloads'],
                'browsers': [self.runtime / 'browser-profiles'],
                'logs': [self.directory / 'logs'],
                'imports': [self.directory / 'imports', self.runtime / 'uploads'],
                'cache': [self.runtime / 'cache', self.directory / 'yt5s']}

    def _get(self, db, key, default=None):
        row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, db, key, value):
        db.execute('''INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value
                      WHERE state.value != excluded.value''', (key, json.dumps(value)))

    def _refresh_usage(self, force=False):
        with self.connect(write=False) as db:
            cached = self._get(db, 'usage', {})
        if not force and cached and time.time() - cached['time'] < USAGE_INTERVAL:
            return
        # Serialize scanners, never downloads. No SQLite transaction spans I/O.
        with file_lock(self.directory / 'disk-usage.lock', blocking=force or not cached) as acquired:
            if not acquired:
                if not cached or time.time() - cached['time'] >= 3 * USAGE_INTERVAL:
                    raise DiskCoordinationBusy('本机磁盘用量核对繁忙，等待后自动重试。')
                return
            with self.connect(write=False) as db:
                latest = self._get(db, 'usage', {})
                rows = db.execute('SELECT id,category,path FROM leases').fetchall()
            if latest and (latest['time'] > cached.get('time', 0) or (
                    not force and time.time() - latest['time'] < USAGE_INTERVAL)):
                return
            usage, measurements = {}, {}
            for category, roots in self.roots().items():
                paths = [Path(row['path']) for row in rows if row['category'] == category]
                # Overlapping/duplicate custom paths cannot both receive credit.
                measured = {path: 0 for path in paths if not any(
                    other != path and path.is_relative_to(other) for other in paths) and paths.count(path) == 1}
                usage[category] = sum(size_of(path, measured=measured) for path in roots)
                measurements.update((row['id'], measured.get(Path(row['path']), 0))
                                    for row in rows if row['category'] == category)
            usage['logs'] += sum(size_of(p) for p in self.directory.glob('*.log'))
            with self.connect() as db:
                # New leases retain measured=0 and their full atomic reservation.
                # Deleted leases simply leave a conservative overcount until refresh.
                db.executemany('UPDATE leases SET measured=? WHERE id=? AND measured != ?',
                               [(amount, identity, amount) for identity, amount in measurements.items()])
                self._set(db, 'usage', {'time': time.time(), 'bytes': usage})

    def _usage(self, db):
        cached = self._get(db, 'usage')
        committed = dict(cached['bytes'])
        for row in db.execute('SELECT * FROM leases'):
            committed[row['category']] += max(0, row['reserved'] - row['measured'])
        return cached['bytes'], committed

    def _free(self):
        return shutil.disk_usage(self.directory).free

    def _watermark(self, db, *, persist=True):
        free = self._free()
        low = int(self.config['disk_min_free_gb']) * GIB
        high = int(self.config['disk_resume_free_gb']) * GIB
        waiting = self._get(db, 'low_space', False)
        waiting = free < (high if waiting else low)
        if persist:
            self._set(db, 'low_space', waiting)
        return waiting, free

    def snapshot(self, *, force=False):
        self._refresh_usage(force)
        with self.connect(write=False) as db:
            usage, committed = self._usage(db)
            waiting, free = self._watermark(db, persist=False)
            watermark_changed = waiting != self._get(db, 'low_space', False)
            blocked = self._get(db, 'reason', '')
            if not waiting and not any(committed[k] >= v for k, v in self.limits.items()) and sum(committed.values()) < int(self.config['disk_budget_gb']) * GIB:
                blocked = ''
            rows = db.execute('SELECT category,COUNT(*) n FROM leases GROUP BY category').fetchall()
            waiting_tasks = db.execute('SELECT COUNT(*) FROM requirements').fetchone()[0]
            result = dict(mode='software', directory=str(self.directory), free_bytes=free,
                        total_bytes=shutil.disk_usage(self.directory).total,
                        used_bytes=sum(usage.values()), committed_bytes=sum(committed.values()),
                        budget_bytes=int(self.config['disk_budget_gb']) * GIB,
                        categories={key: dict(used_bytes=usage[key], committed_bytes=committed[key],
                                              budget_bytes=limit) for key, limit in self.limits.items()},
                        low_space=waiting, message=('磁盘剩余空间不足，正在等待自动恢复。' if waiting else blocked),
                        active=dict((r['category'], r['n']) for r in rows),
                        waiting_tasks=waiting_tasks,
                        last_cleanup=self._get(db, 'cleanup', {}))
        if watermark_changed:
            with self.connect() as db:
                self._watermark(db)
        try:
            warning = json.loads((self.directory / 'log-write-warning.json').read_text(encoding='utf-8'))
            if time.time() - warning['time'] < 300:
                result['log_warning'] = warning['message']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return result

    def publish(self, store, snapshot):
        from webapp.database import settings
        identity = store.fleet.id if store.fleet else 'local'
        blocked = snapshot['low_space'] or snapshot['committed_bytes'] >= snapshot['budget_bytes']
        value = {'blocked': blocked, 'message': snapshot['message'], 'time': time.time()}
        with store.connect() as db:
            db.execute(store.db.upsert(settings, {'key': 'disk:' + identity, 'value': json.dumps(value)}, ['key']))
        # A local file keeps the independent disk watchdog off the remote DB.
        target = self.directory / 'disk-budget-config.json'
        content = json.dumps({key: self.config[key] for key in DISK_FIELDS})
        if not target.exists() or target.read_text(encoding='utf-8') != content:
            temporary = target.with_suffix('.tmp')
            temporary.write_text(content, encoding='utf-8')
            temporary.replace(target)
    def _version(self):
        policy_fields = ('download_methods', 'download_method', 'download_format', 'yt5s_quality',
                         'downmp3_quality', 'yt1d_quality', 'storage_mode')
        return json.dumps({'disk': {k: self.config[k] for k in sorted(DISK_FIELDS)},
                           'policy': {k: self.config.get(k) for k in policy_fields}}, sort_keys=True)

    def lease(self, category, *, task=None, reserve=None, path=None):
        identity = uuid.uuid4().hex
        if path is None:
            folder = {'test': 'test-downloads', 'browsers': 'runtime/browser-profiles',
                      'imports': 'runtime/uploads', 'cache': 'runtime/cache'}[category]
            path = self.directory / folder / identity
        return DiskLease(self, identity, category, safe_path(path, self.directory), task, reserve)

    def _reserve(self, lease, needed):
        self._refresh_usage()
        if lease.reserved >= needed:
            # The common progress/watchdog path only validates an existing
            # promise. It must not queue for a writer or generate journal I/O.
            with self.connect(write=False) as db:
                usage, committed = self._usage(db)
                waiting, free = self._watermark(db, persist=False)
                old = db.execute('SELECT reserved FROM leases WHERE id=?', (lease.id,)).fetchone()
                if (old and old[0] >= needed and free >= int(self.config['disk_min_free_gb']) * GIB
                        and waiting == self._get(db, 'low_space', False)
                        and committed[lease.category] <= self.limits[lease.category]
                        and sum(committed.values()) <= int(self.config['disk_budget_gb']) * GIB
                        and all(usage[key] <= limit for key, limit in self.limits.items() if key != lease.category)):
                    lease.reserved = old[0]
                    return
        for attempt in range(2):
            if attempt:
                self._refresh_usage(force=True)
            with self.connect() as db:
                usage, committed = self._usage(db)
                waiting, free = self._watermark(db)
                old = db.execute('SELECT reserved FROM leases WHERE id=?', (lease.id,)).fetchone()
                previous = old[0] if old else 0
                required = db.execute('SELECT * FROM requirements WHERE task=?', (lease.task or '',)).fetchone()
                if not old and lease.task and not required and db.execute('SELECT COUNT(*) FROM requirements').fetchone()[0] >= 10000:
                    raise DiskPressure('磁盘等待记录已达上限，请处理大视频预算后继续。')
                if not old and required:
                    previous_version = json.loads(required['version'])
                    current_version = json.loads(self._version())
                    if previous_version.get('policy') == current_version['policy']:
                        needed = max(needed, required['needed'])
                        if required['version'] == self._version() and required['interruptions'] >= 3 and free < required['free'] + GIB:
                            raise DiskPressure('同一磁盘预算下多次空间不足，等待提高预算或释放更多空间。')
                extra = max(0, needed - previous)
                unused = sum(max(0, row['reserved'] - row['measured']) for row in db.execute('SELECT reserved,measured FROM leases'))
                if not attempt and extra and free - unused - extra < int(self.config['disk_min_free_gb']) * GIB:
                    # Release the transaction before refreshing filesystem usage.
                    continue
                # Existing reservations can finish below the resume watermark, but
                # never below the low watermark or by extending an exhausted budget.
                low = int(self.config['disk_min_free_gb']) * GIB
                reason = ''
                if (waiting and not old) or free < low or (extra and free - unused - extra < low):
                    reason = '磁盘剩余空间不足，等待释放空间。'
                elif committed[lease.category] + extra > self.limits[lease.category]:
                    reason = '本机临时文件分类预算不足，等待空间；大视频可能需要提高预算。'
                elif sum(committed.values()) + extra > int(self.config['disk_budget_gb']) * GIB:
                    reason = '本机临时文件总预算不足，等待其他执行完成或自动清理。'
                elif any(usage[key] > maximum for key, maximum in self.limits.items() if key != lease.category):
                    reason = '其他临时文件分类超出预算，等待自动清理。'
                if reason:
                    # Admission waits must not exhaust the interruption count.
                    if old and lease.task:
                        count = required['interruptions'] + 1 if required and required['version'] == self._version() else 1
                        db.execute('INSERT OR REPLACE INTO requirements VALUES (?,?,?,?,?)',
                                   (lease.task, self._version(), needed, count, free))
                    self._set(db, 'reason', reason)
                else:
                    if old:
                        if needed > previous:
                            db.execute('UPDATE leases SET reserved=? WHERE id=?', (needed, lease.id))
                    else:
                        db.execute('INSERT INTO leases(id,category,path,task,owner,reserved,created) VALUES (?,?,?,?,?,?,?)',
                                   (lease.id, lease.category, str(lease.path), lease.task,
                                    json.dumps(process_identity()), needed, time.time()))
                    lease.reserved = max(previous, needed)
            break
        if reason:
            raise DiskPressure(reason)

    def cleanup(self, store=None, *, grace=300):
        """Only delete owned, unlocked directories after verifying all owners."""
        removed, freed, failed, pending = 0, 0, 0, 0
        with file_lock(self.directory / 'disk-cleanup.lock') as acquired:
            if not acquired:
                return self.snapshot()
            with self.connect(write=False) as db:
                rows = db.execute('SELECT * FROM leases').fetchall()
            for row in rows:
                if time.time() - row['created'] < grace or alive(json.loads(row['owner'])):
                    continue
                with file_lock(self.directory / 'disk-locks' / (row['id'] + '.lock')) as owned:
                    if not owned:
                        continue
                    children = json.loads(row['children'])
                    # Only verified descendants recorded by the original owner.
                    for child in children:
                        if alive(child):
                            try:
                                psutil.Process(child['pid']).kill()
                            except psutil.NoSuchProcess:
                                pass
                            except psutil.AccessDenied:
                                failed += 1
                    if any(alive(child) for child in children):
                        continue
                    try:
                        path = safe_path(row['path'], self.directory)
                        if row['task'] and store:
                            task = store.get(row['task'])
                            if task and task['status'] == 'running':
                                continue
                        receipts = path / '.completed-files.json'
                        if receipts.exists():
                            if not store or not self.receipts_recorded(store, row['task'], receipts):
                                pending += 1
                                continue
                        amount = size_of(path)
                        if path.exists():
                            shutil.rmtree(path)
                        with self.connect() as db:
                            db.execute('DELETE FROM leases WHERE id=?', (row['id'],))
                        try:
                            path.parent.rmdir()
                        except OSError:
                            pass
                        removed += 1
                        freed += amount
                    except (OSError, ValueError):
                        failed += 1
            if store:
                self._clean_imports(store)
                self._clean_legacy_tests(store, grace)
                from webapp.maintenance import prune_disk_logs
                prune_disk_logs(store, self.config, low_space=self._free() < int(self.config['disk_min_free_gb']) * GIB)
                from sqlalchemy import select
                from webapp.database import tasks
                with self.connect(write=False) as db:
                    identities = [row[0] for row in db.execute('SELECT task FROM requirements')]
                retained_tasks = set()
                for offset in range(0, len(identities), 500):
                    with store.connect() as db:
                        retained_tasks.update(db.execute(select(tasks.c.id).where(tasks.c.id.in_(identities[offset:offset + 500]),
                            tasks.c.status.in_(('queued', 'running')))).scalars())
                with self.connect() as db:
                    db.executemany('DELETE FROM requirements WHERE task=?', [(identity,) for identity in identities if identity not in retained_tasks])
            self._clean_diagnostics(grace)
            with self.connect(write=False) as db:
                retained = {row[0] for row in db.execute('SELECT id FROM leases')}
            for lock in (self.directory / 'disk-locks').glob('*.lock'):
                if lock.stem in retained:
                    continue
                with file_lock(lock) as available:
                    pass
                if available:
                    try:
                        lock.unlink(missing_ok=True)
                    except OSError:
                        pass
            with self.connect() as db:
                self._set(db, 'cleanup', dict(time=time.time(), removed=removed, freed_bytes=freed,
                                              failed=failed, pending_results=pending))
        return self.snapshot(force=True)

    @staticmethod
    def receipts_recorded(store, task, path):
        from sqlalchemy import select
        from webapp.database import test_files
        receipts = json.loads(path.read_text(encoding='utf-8'))
        with store.connect() as db:
            recorded = {r.path: r.size for r in db.execute(select(test_files.c.path, test_files.c.size).where(test_files.c.task_id == task))}
        return all(recorded.get(item['path']) == item['size'] for item in receipts)

    def recover_test_result(self, store, task_id):
        """A newly claimed owner can finish a complete file left before DB commit."""
        from sqlalchemy import select
        from webapp.database import tasks
        with self.connect(write=False) as db:
            rows = db.execute("SELECT * FROM leases WHERE category='test' AND task=? ORDER BY created", (task_id,)).fetchall()
        for row in rows:
            if alive(json.loads(row['owner'])):
                continue
            with file_lock(self.directory / 'disk-locks' / (row['id'] + '.lock')) as owned:
                if not owned or any(alive(child) for child in json.loads(row['children'])):
                    continue
                root = safe_path(row['path'], self.directory)
                receipt_path = root / '.completed-files.json'
                if not receipt_path.is_file():
                    continue
                receipts = json.loads(receipt_path.read_text(encoding='utf-8'))
                task = store.get(task_id, private=True)
                # Older/multi-video compatibility work has no unambiguous whole
                # task completion receipt. Preserve it for inspection.
                if not task or len(task['payload'].get('urls', [])) != 1 or len(receipts) != 1:
                    continue
                item = receipts[0]
                path = safe_path(item['path'], root)
                if not item.get('complete') or not path.is_file() or path.stat().st_size != item['size']:
                    continue
                if not store.heartbeat(task_id, store.owner):
                    return None
                with store.connect() as db:
                    current = db.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one()
                    payload = json.loads(current['payload'])
                    session_id = payload.get('download_session_id')
                    if session_id and item.get('method'):
                        stats = store.statistics.methods
                        provider = stats.ensure(db, current, session_id)
                        if provider['method'] != item['method']:
                            stats.change(db, current, session_id, 'paused')
                            stats.activate(db, current, session_id, item['method'], 'running')
                store.statistics.record_file(task_id, path, item.get('video_id', ''), item.get('title', ''), test_mode=True)
                if not self.receipts_recorded(store, task_id, receipt_path):
                    raise DiskPressure('完整测试文件与统计登记尚未核对一致，已保留文件，等待核对。')
                try:
                    shutil.rmtree(root)
                    with self.connect() as db:
                        db.execute('DELETE FROM leases WHERE id=?', (row['id'],))
                    try:
                        root.parent.rmdir()
                    except OSError:
                        pass
                    deleted = True
                except OSError:
                    deleted = False
                return dict(message='已恢复上次完整测试结果，统计已核对，无需重新下载。',
                            test_mode=True, files_deleted=deleted, skipped=False, file_count=1,
                            file_bytes=item['size'], billable_bytes=0, download_method=item.get('method'), recovered=True)
        return None

    def _clean_imports(self, store):
        from sqlalchemy import select
        from webapp.database import imports, tasks
        files = [str(path) for path in (self.directory / 'imports').glob('*') if path.suffix.lower() in ('.txt', '.csv')]
        rows = []
        for offset in range(0, len(files), 500):
            with store.connect() as db:
                rows.extend(db.execute(select(imports.c.id, imports.c.path).where(imports.c.finished != 0,
                    imports.c.path.in_(files[offset:offset + 500]))).all())
        with store.connect() as db:
            active = db.execute(select(tasks.c.payload).where(tasks.c.action == 'import', tasks.c.status == 'running')).scalars()
            busy = {json.loads(value).get('import_id') for value in active}
        for row in rows:
            if row.id in busy:
                continue
            try:
                path = safe_path(row.path, self.directory / 'imports')
                path.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue

    def _clean_legacy_tests(self, store, grace):
        root = self.directory / 'test-downloads'
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            return
        with self.connect(write=False) as db:
            owned = {Path(r[0]) for r in db.execute('SELECT path FROM leases')}
        for path in root.iterdir():
            if not re.fullmatch('[0-9a-f]{32}', path.name) or not path.is_dir():
                continue
            if any(p == path or p.is_relative_to(path) for p in owned):
                continue
            task = store.get(path.name)
            if not task or task['status'] in ('queued', 'running'):
                continue
            try:
                safe_path(path, self.directory)
                if time.time() - path.stat().st_mtime >= grace:
                    shutil.rmtree(path)
            except (OSError, ValueError):
                pass

    def _clean_diagnostics(self, grace):
        for path in (self.directory / 'yt5s').glob('worker-*'):
            if not re.fullmatch(r'worker-\d+', path.name):
                continue
            with file_lock(self.directory / (path.name + '.lock')) as available:
                if not available:
                    continue
                for child in path.glob('resolve-*'):
                    try:
                        safe_path(child, self.directory)
                        if time.time() - child.stat().st_mtime >= grace:
                            shutil.rmtree(child)
                    except (OSError, ValueError):
                        pass
        for path in (self.runtime / 'cache').glob('worker-*'):
            if not re.fullmatch(r'worker-(\d+|maintenance)', path.name):
                continue
            with file_lock(self.directory / (path.name + '.lock')) as available:
                if not available:
                    continue
                try:
                    safe_path(path, self.directory)
                    if time.time() - path.stat().st_mtime >= grace:
                        shutil.rmtree(path)
                except (OSError, ValueError):
                    pass


class DiskLease:
    def __init__(self, budget, identity, category, path, task, reserve):
        self.budget, self.id, self.category, self.path, self.task = budget, identity, category, path, task
        self.reserved = 0
        self.initial = reserve if reserve is not None else min(GIB if category == 'test' else 16 * MIB,
                                                              budget.limits[category])
        self.stop = threading.Event()
        self.error = None
        self.monitor = None
        self.last_check = 0
        self.estimate = 0
        self.children = []
        self.recorded_children = '[]'
        self.check_lock = threading.RLock()
        self.child_root_pid = None
        self.on_pressure = None
        self.preserve = False
        self.config_reader = None
        self.media_directory = None

    def __enter__(self):
        self.lock = file_lock(self.budget.directory / 'disk-locks' / (self.id + '.lock'))
        if not self.lock.__enter__():
            raise DiskPressure('临时目录正在清理，请稍后重试。')
        try:
            self.budget._reserve(self, self.initial)
            self.path.mkdir(parents=True, exist_ok=True)
        except BaseException:
            try:
                if self.reserved:
                    self._retire()
            finally:
                self.lock.__exit__(None, None, None)
            raise
        return self

    def _retire(self):
        """Best-effort handoff; retained accounting is safer than losing ownership."""
        try:
            with self.budget.connect() as db:
                db.execute('UPDATE leases SET owner=?,children=? WHERE id=?',
                           (json.dumps({'pid': os.getpid(), 'created': 0}), json.dumps(self.children), self.id))
        except (DiskPressure, OSError):
            # Keep the reservation if metadata is unavailable. The sweep can
            # verify it after this process exits; never turn completion into retry.
            pass

    def check(self, estimate=0, *, force=False):
        # Progress callbacks and the watchdog share one reservation/estimate.
        with self.check_lock:
            return self._check(estimate, force=force)

    def _check(self, estimate=0, *, force=False):
        if self.error:
            raise self.error
        self.estimate = max(self.estimate, int(estimate or 0))
        if not force and self.estimate <= self.reserved and time.monotonic() - self.last_check < 1:
            return
        self.last_check = time.monotonic()
        if self.config_reader:
            self.budget.config.update(self.config_reader())
        else:
            local_config = self.budget.directory / 'disk-budget-config.json'
            try:
                self.budget.config.update(json.loads(local_config.read_text(encoding='utf-8')))
            except FileNotFoundError:
                pass
        if self.media_directory:
            if shutil.disk_usage(self.media_directory).free < max(
                    int(self.budget.config.get('min_free_gb', 2)), int(self.budget.config['disk_min_free_gb'])) * GIB:
                raise DiskPressure('素材目录所在磁盘剩余空间不足。')
        used = size_of(self.path)
        unit = min(16 * MIB, max(4096, self.budget.limits[self.category] // 16))
        needed = max(self.reserved, self.estimate, math.ceil((used + unit) / unit) * unit)
        try:
            self.budget._reserve(self, needed)
        except DiskPressure as error:
            self.error = error
            raise

    def track_children(self):
        found = {}
        for old in self.children:
            if alive(old):
                found[old['pid']] = old
        try:
            root = psutil.Process(self.child_root_pid) if self.child_root_pid else psutil.Process()
            children = root.children(recursive=True)
            if self.child_root_pid:
                children.append(root)
            for child in children:
                try:
                    if 'mihomo' not in child.name().lower():
                        found[child.pid] = process_identity(child.pid)
                except psutil.Error:
                    pass
        except psutil.Error:
            pass
        self.children = sorted(found.values(), key=lambda child: child['pid'])
        content = json.dumps(self.children)
        if content != self.recorded_children:
            with self.budget.connect() as db:
                db.execute('UPDATE leases SET children=? WHERE id=? AND children != ?', (content, self.id, content))
            self.recorded_children = content

    def start_monitor(self, on_pressure=None):
        self.on_pressure = on_pressure
        def watch():
            while not self.stop.wait(1):
                try:
                    self.track_children()
                    self.check(force=True)
                except Exception as error:
                    self.error = error if isinstance(error, DiskPressure) else DiskPressure('磁盘资源检查失败，执行已停止。')
                    if not isinstance(error, DiskPressure):
                        logging.getLogger(__name__).exception('磁盘资源监控检查失败')
                    if self.on_pressure:
                        self.on_pressure()
                    return
        self.monitor = threading.Thread(target=watch, name='disk-budget-monitor', daemon=True)
        self.monitor.start()

    def __exit__(self, kind, value, traceback):
        self.stop.set()
        if self.monitor:
            self.monitor.join(timeout=20)
        try:
            if self.monitor and self.monitor.is_alive():
                self.preserve = True
            if (self.error or kind) and self.on_pressure:
                try:
                    self.track_children()
                except (DiskPressure, OSError):
                    # Do not delete files if descendant tracking is unconfirmed.
                    self.preserve = True
                self.on_pressure()
                processes = []
                for child in self.children:
                    if alive(child):
                        try:
                            processes.append(psutil.Process(child['pid']))
                        except psutil.NoSuchProcess:
                            pass
                _, remaining = psutil.wait_procs(processes, timeout=5)
                if remaining:
                    self.preserve = True
            if self.preserve:
                self._retire()
            else:
                if self.path.exists():
                    safe_path(self.path, self.budget.directory)
                    shutil.rmtree(self.path)
                with self.budget.connect() as db:
                    db.execute('DELETE FROM leases WHERE id=?', (self.id,))
                try:
                    self.path.parent.rmdir()
                except OSError:
                    pass
        except (DiskPressure, OSError):
            # A later sweep verifies ownership and retries; accounting is retained.
            self._retire()
        finally:
            self.lock.__exit__(kind, value, traceback)
            if not self.path.exists():
                (self.budget.directory / 'disk-locks' / (self.id + '.lock')).unlink(missing_ok=True)
        if self.error and not isinstance(value, (KeyboardInterrupt, SystemExit)):
            raise self.error

