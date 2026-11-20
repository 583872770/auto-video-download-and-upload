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

from webapp.config.schema import DISK_DEFAULTS, DISK_FIELDS
from webapp.worker.formatting import format_bytes
from webapp.db_pool_calculator import target_workers

GIB = 1024 ** 3
MIB = 1024 ** 2
COORDINATION_TIMEOUT = 5
USAGE_INTERVAL = 5


class DiskPressure(RuntimeError):
    """A recoverable resource condition, never a provider/video failure."""
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail


class DiskCoordinationBusy(DiskPressure):
    """Local accounting could not be checked within the bounded lock wait."""


def is_disk_full(error):
    return isinstance(error, OSError) and error.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', 122))


def _path_comparison_key(path):
    value = str(path)
    if os.name == 'nt':
        # Windows realpath can retain the extended prefix if a directory
        # disappears between its two native lookups. This is spelling, not a
        # link; normalize only DOS/UNC prefixes, never device namespaces.
        if value.upper().startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif (value.startswith('\\\\?\\') and len(value) >= 7
              and value[4].isalpha() and value[5:7] == ':\\'):
            value = value[4:]
    return os.path.normcase(value)


def safe_path(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('临时文件路径超出受控目录。')
    for attempt in range(5):
        if root.is_symlink() or root.is_junction():
            raise ValueError('临时根目录不能是链接。')
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if current.is_symlink() or current.is_junction():
                raise ValueError('临时目录不能包含链接。')
        resolved_root, resolved_path = root.resolve(), path.resolve()
        if (_path_comparison_key(resolved_root) == _path_comparison_key(root)
                and _path_comparison_key(resolved_path) == _path_comparison_key(path)):
            return path
        # NTFS may briefly resolve a concurrently removed directory to its
        # internal $Deleted name. Recheck the full guard, never accept that
        # redirect. Stable redirects and real links still fail closed.
        if os.name != 'nt' or attempt == 4:
            raise ValueError('临时文件路径包含链接。')
        time.sleep(.001 * 2 ** attempt)


def _read_disk_metadata(operation):
    for attempt in range(5):
        try:
            return operation()
        except PermissionError as error:
            # Windows can report access denied while a directory is pending
            # deletion. Retry briefly; persistent ACL errors remain visible.
            if os.name != 'nt' or getattr(error, 'winerror', None) != 5 or attempt == 4:
                raise
            time.sleep(.001 * 2 ** attempt)


def size_of(path, *, measured=None):
    """Count entry bytes; only traverse directories, never links or sockets."""
    path = Path(path)
    try:
        if path.is_junction():
            return 0
        metadata = _read_disk_metadata(path.lstat)
        if stat.S_ISLNK(metadata.st_mode):
            return 0
        total = max(metadata.st_size, getattr(metadata, 'st_blocks', 0) * 512)
        # Chromium creates filesystem sockets alongside its temporary files.
        # FIFOs and device nodes likewise have metadata but no children.
        if stat.S_ISDIR(metadata.st_mode):
            total = max(total, 4096)
            with _read_disk_metadata(lambda: os.scandir(path)) as entries:
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
        self._configure_journal()
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

    def _configure_journal(self):
        # WAL keeps short read-only budget/progress checks from starving the
        # reservation writer. Keep FULL synchronization for crash-safe promises.
        try:
            db = sqlite3.connect(self.database, timeout=COORDINATION_TIMEOUT)
            try:
                mode = db.execute('PRAGMA journal_mode').fetchone()[0]
            finally:
                db.close()
            if mode == 'wal':
                return
            with file_lock(self.directory / 'disk-journal.lock', blocking=True) as acquired:
                if not acquired:
                    raise DiskCoordinationBusy('本机磁盘协调初始化繁忙，稍后重试。')
                db = sqlite3.connect(self.database, timeout=COORDINATION_TIMEOUT)
                try:
                    if db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
                        db.execute('PRAGMA journal_mode=WAL')
                finally:
                    db.close()
        except sqlite3.OperationalError as error:
            raise DiskCoordinationBusy('本机磁盘协调初始化繁忙，稍后重试。') from error

    @contextmanager
    def connect(self, *, write=True):
        # SQLite's busy handler retries transient locks within this deadline.
        db = sqlite3.connect(self.database, timeout=COORDINATION_TIMEOUT)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA wal_autocheckpoint=128')
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
                'cache': int(self.config['disk_cache_budget_mb']) * MIB,
                # Active downloads can be much larger than diagnostic cache.
                # They are governed by the global temporary-file budget and
                # live in their own root so a small cache budget cannot stop a
                # valid video halfway through transfer.
                'downloads': int(self.config['disk_budget_gb']) * GIB}

    def roots(self):
        return {'test': [self.directory / 'test-downloads', self.directory / 'test-samples'],
                'browsers': [self.runtime / 'browser-profiles'],
                'logs': [self.directory / 'logs'],
                'imports': [self.directory / 'imports', self.runtime / 'uploads'],
                'cache': [self.runtime / 'cache'],
                'downloads': [self.runtime / 'downloads']}

    def _get(self, db, key, default=None):
        row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, db, key, value):
        db.execute('''INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value
                      WHERE state.value != excluded.value''', (key, json.dumps(value)))

    def _refresh_usage(self, force=False):
        with self.connect(write=False) as db:
            cached = self._get(db, 'usage', {})
        # A newer release may add a temporary-file category. Do not reuse a
        # fresh usage sample from an older release until all category keys are
        # present; otherwise a new lease can be accounted against a missing
        # bucket during a rolling upgrade.
        cached_bytes = cached.get('bytes', {}) if cached else {}
        if (not force and cached and set(self.limits) <= set(cached_bytes)
                and time.time() - cached['time'] < USAGE_INTERVAL):
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
        usage = {key: int(cached.get('bytes', {}).get(key, 0)) for key in self.limits}
        committed = dict(usage)
        for row in db.execute('SELECT * FROM leases'):
            # Keep old/unknown rows visible in the total while avoiding a
            # KeyError during a rolling upgrade.
            category = row['category']
            committed[category] = committed.get(category, 0) + max(0, row['reserved'] - row['measured'])
        return usage, committed

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

    def record_video_size(self, size, *, merged=True):
        """Small bounded local sample for estimates; never changes the configured budget."""
        if size <= 0:
            return
        with self.connect() as db:
            samples = self._get(db, 'video_sizes', [])
            samples = [sample for sample in samples if sample['time'] > time.time() - 86400][-31:]
            samples.append({'time': time.time(), 'size': int(size), 'peak': int(size * (2 if merged else 1))})
            self._set(db, 'video_sizes', samples)

    def snapshot(self, *, force=False):
        self._refresh_usage(force)
        with self.connect(write=False) as db:
            usage, committed = self._usage(db)
            waiting, free = self._watermark(db, persist=False)
            watermark_changed = waiting != self._get(db, 'low_space', False)
            blocked = self._get(db, 'reason', '')
            if not waiting and not any(committed[k] >= v for k, v in self.limits.items()) and sum(committed.values()) < int(self.config['disk_budget_gb']) * GIB:
                blocked = ''
            rows = db.execute('SELECT category,COUNT(*) n FROM leases WHERE reserved > 0 GROUP BY category').fetchall()
            waiting_tasks = db.execute('SELECT COUNT(*) FROM requirements WHERE task NOT IN '
                '(SELECT task FROM leases WHERE task IS NOT NULL AND reserved > 0)').fetchone()[0]
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
            result['test_samples'] = dict(**self._get(db, 'sample_status', {}), budget_bytes=int(self.config.get('disk_sample_budget_mb', 3072)) * MIB,
                last_cleanup=self._get(db, 'sample_cleanup', {}))
            samples = [sample for sample in self._get(db, 'video_sizes', []) if sample['time'] > time.time() - 86400]
            if self.config.get('storage_mode') == 'test' and len(samples) >= 3:
                peaks = sorted(sample['peak'] for sample in samples)
                peak = max(min(GIB, self.limits['test']), peaks[math.ceil(len(peaks) * .9) - 1])
                other = sum(value for key, value in committed.items() if key != 'test')
                capacity = max(0, min(self.limits['test'], int(self.config['disk_budget_gb']) * GIB - other,
                                     free + usage['test'] - int(self.config['disk_min_free_gb']) * GIB))
                result['capacity_estimate'] = dict(sample_count=len(samples), peak_bytes=peak,
                    suggested_slots=capacity // peak, configured_slots=target_workers(self.config),
                    budget_for_configured_bytes=peak * target_workers(self.config),
                    additional_slots=max(0, min(self.limits['test']-committed['test'],
                        int(self.config['disk_budget_gb']) * GIB - sum(committed.values()),
                        free-int(self.config['disk_min_free_gb']) * GIB)) // peak)
        if watermark_changed:
            with self.connect() as db:
                self._watermark(db)
        try:
            warning = json.loads((self.directory / 'log-write-warning.json').read_text(encoding='utf-8'))
            if time.time() - warning['time'] < 300:
                result['log_warning'] = warning['message']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        from webapp.disk_diagnostics import explain
        result['diagnostic'] = explain(self, result)
        return result

    def publish(self, store, snapshot):
        from webapp.disk_diagnostics import publish
        publish(self, store, snapshot)
        # A local file keeps the independent disk watchdog off the remote DB.
        target = self.directory / 'disk-budget-config.json'
        content = json.dumps({key: self.config[key] for key in DISK_FIELDS})
        if not target.exists() or target.read_text(encoding='utf-8') != content:
            temporary = target.with_suffix('.tmp')
            temporary.write_text(content, encoding='utf-8')
            temporary.replace(target)
    def _version(self):
        policy_fields = ('storage_mode',)
        return json.dumps({'disk': {k: self.config[k] for k in sorted(DISK_FIELDS)},
                           'policy': {k: self.config.get(k) for k in policy_fields}}, sort_keys=True)

    def lease(self, category, *, task=None, reserve=None, path=None):
        identity = uuid.uuid4().hex
        if path is None:
            folder = {'test': 'test-downloads', 'browsers': 'runtime/browser-profiles',
                      'imports': 'runtime/uploads', 'cache': 'runtime/cache',
                      'downloads': 'runtime/downloads'}[category]
            path = self.directory / folder / identity
        return DiskLease(self, identity, category, safe_path(path, self.directory), task, reserve)

    def adopt(self, identity, task, path):
        with self.connect(write=False) as db:
            row = db.execute('SELECT * FROM leases WHERE id=? AND task=?', (identity, task)).fetchone()
        if not row or safe_path(row['path'], self.directory) != safe_path(path, self.directory):
            raise DiskPressure('待处理文件的空间登记缺失，已停止本地处理，请检查临时目录。')
        lease = DiskLease(self, identity, row['category'], safe_path(path, self.directory), task, 0)
        lease.processing = True
        return lease

    def blocked_tasks(self):
        """Cheap pre-claim hint. Actual admission still reserves atomically.

        Read only the bounded local requirements and cached usage, outside the
        business transaction. A retained workspace gets a recovery opportunity;
        it may finish without allocating another full download workspace.
        """
        with self.connect(write=False) as db:
            requirements = db.execute('SELECT * FROM requirements').fetchall()
            if not requirements:
                return set()
            cached = self._get(db, 'usage', {})
            if time.time() - cached.get('time', 0) > 3 * USAGE_INTERVAL:
                return set()  # Let admission refresh uncertain measurements.
            usage, committed = self._usage(db)
            waiting, free = self._watermark(db, persist=False)
            unused = sum(max(0, r['reserved'] - r['measured']) for r in db.execute('SELECT reserved,measured FROM leases'))
            recoverable = {r[0] for r in db.execute('SELECT task FROM leases WHERE reserved=0 AND task IS NOT NULL')}
        blocked = set()
        current = json.loads(self._version())
        for row in requirements:
            version = json.loads(row['version'])
            category = 'test' if version['policy'].get('storage_mode') == 'test' else 'downloads'
            # Settings changes receive one new authoritative admission check.
            policy_changed = any(value != current['policy'].get(key) for key, value in
                version['policy'].items() if key != 'storage_mode')
            if row['task'] in recoverable or version['disk'] != current['disk'] or policy_changed:
                continue
            available = min(free-unused-int(self.config['disk_min_free_gb'])*GIB,
                self.limits[category]-committed[category],
                int(self.config['disk_budget_gb'])*GIB-sum(committed.values()))
            if waiting or row['needed'] > available or any(usage[k] > v for k, v in self.limits.items()):
                blocked.add(row['task'])
        return blocked

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
                old = db.execute('SELECT reserved,measured FROM leases WHERE id=?', (lease.id,)).fetchone()
                previous = old['reserved'] if old else 0
                measured = old['measured'] if old else 0
                required = db.execute('SELECT * FROM requirements WHERE task=?', (lease.task or '',)).fetchone()
                if not old and lease.task and not required and db.execute('SELECT COUNT(*) FROM requirements').fetchone()[0] >= 10000:
                    raise DiskPressure('磁盘等待记录已达上限，请处理大视频预算后继续。')
                if not old and required:
                    previous_version = json.loads(required['version'])
                    current_version = json.loads(self._version())
                    if previous_version.get('policy') == current_version['policy']:
                        needed = max(needed, required['needed'])
                # A scan already includes measured bytes in category usage, and
                # disk free already excludes them. Grow only the unwritten
                # promise, including when writes have outrun the old reservation.
                extra = max(0, needed - measured) - max(0, previous - measured)
                extra = max(0, extra)
                unused = sum(max(0, row['reserved'] - row['measured']) for row in db.execute('SELECT reserved,measured FROM leases'))
                # Existing reservations can finish below the resume watermark, but
                # never below the low watermark or by extending an exhausted budget.
                low = int(self.config['disk_min_free_gb']) * GIB
                reason = ''
                if free < low:
                    reason = '磁盘实际剩余空间不足，等待释放空间。'
                elif waiting and not old:
                    reason = f'磁盘空间尚未达到恢复水位 {format_bytes(int(self.config["disk_resume_free_gb"]) * GIB)}，新任务等待恢复。'
                elif extra and free - unused - extra < low:
                    reason = '磁盘可分配的预留空间不足，等待其他任务释放空间。'
                elif committed[lease.category] + extra > self.limits[lease.category]:
                    category = {'test': '测试下载', 'downloads': '下载临时文件', 'cache': '缓存',
                                'imports': '导入文件', 'browsers': '浏览器', 'logs': '日志'}[lease.category]
                    reason = (f'本机临时文件分类预算不足（{category}）：已占用及预留 {format_bytes(committed[lease.category])}，'
                              f'分类上限 {format_bytes(self.limits[lease.category])}；等待空间或提高预算。')
                elif sum(committed.values()) + extra > int(self.config['disk_budget_gb']) * GIB:
                    reason = (f'本机临时文件总预算不足：已占用及预留 {format_bytes(sum(committed.values()))}，'
                              f'总上限 {format_bytes(int(self.config["disk_budget_gb"]) * GIB)}；等待其他任务完成或清理。')
                elif any(usage[key] > maximum for key, maximum in self.limits.items() if key != lease.category):
                    reason = '其他临时文件分类超出预算，等待自动清理。'
                if reason and not attempt:
                    # Refresh stale measurements once before interrupting work;
                    # filesystem scans must never hold the reservation writer.
                    continue
                if reason:
                    reason += (f' 实际剩余 {format_bytes(free)}，现有任务尚未写入的预留 {format_bytes(unused)}，'
                               f'本次新增预留 {format_bytes(extra)}，最低保留 {format_bytes(low)}。')
                    # Admission waits must not exhaust the interruption count.
                    if lease.task:
                        count = (required['interruptions'] if required and required['version'] == self._version() else 0) + int(bool(old))
                        db.execute('INSERT OR REPLACE INTO requirements VALUES (?,?,?,?,?)',
                                   (lease.task, self._version(), needed, count, free))
                    self._set(db, 'reason', reason)
                    kind = ('budget' if '预算' in reason.split(' 实际剩余')[0] else
                            'reservation' if '预留空间不足' in reason else 'space')
                    pressure = dict(kind=kind, category=lease.category, sampled_at=time.time(),
                        task_id=lease.task, version=self._version(), reason=reason,
                        free_bytes=free, unwritten_reserved_bytes=unused, requested_bytes=extra,
                        minimum_free_bytes=low, allocatable_bytes=max(0, min(free-unused-low,
                            self.limits[lease.category]-committed[lease.category],
                            int(self.config['disk_budget_gb'])*GIB-sum(committed.values()))))
                    self._set(db, 'pressure', pressure)
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
            raise DiskPressure(reason, pressure)

    def release_retained(self, identity):
        """Caller holds the workspace lock and has confirmed every writer stopped.

        Keep the recovery receipt and actual bytes, but no promise for future
        writes. Serialize with scanners so releasing a promise cannot expose a
        stale, smaller measurement to a concurrent admission.
        """
        self._refresh_usage()
        with file_lock(self.directory / 'disk-usage.lock', blocking=True) as acquired:
            if not acquired:
                raise DiskCoordinationBusy('本机磁盘用量核对繁忙，等待后自动重试。')
            with self.connect(write=False) as db:
                row = db.execute('SELECT * FROM leases WHERE id=?', (identity,)).fetchone()
            if not row or not row['reserved']:
                return
            measured = size_of(safe_path(row['path'], self.directory))
            with self.connect() as db:
                cached = self._get(db, 'usage')
                # Never reduce a sampled category here: overlapping paths and
                # removed entries are reconciled by the next full traversal.
                cached['bytes'][row['category']] += max(0, measured - row['measured'])
                self._set(db, 'usage', cached)
                db.execute('UPDATE leases SET reserved=0,measured=? WHERE id=?', (measured, identity))

    def cleanup(self, store=None, *, grace=300):
        """Only delete owned, unlocked directories after verifying all owners."""
        removed, freed, failed, pending = 0, 0, 0, 0
        failure_reasons = []
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
                                if len(failure_reasons) < 8:
                                    failure_reasons.append('残留子进程权限不足，暂时无法安全清理。')
                    if any(alive(child) for child in children):
                        continue
                    try:
                        path = safe_path(row['path'], self.directory)
                        task = store.get(row['task']) if row['task'] and store else None
                        if task and task['status'] == 'running':
                            continue
                        self.release_retained(row['id'])
                        receipts = path / '.completed-files.json'
                        if receipts.exists():
                            if not store or not self.receipts_recorded(store, row['task'], receipts):
                                pending += 1
                                continue
                        if store and task:
                            from webapp.delivery.samples import expire_failed
                            expire_failed(store, self, row, task)
                        if store and task and task['status'] not in ('removed', 'cancelled'):
                            from webapp.delivery.processing import retains_test_workspace
                            if retains_test_workspace(store, row):
                                pending += 1
                                continue
                        if store and task:
                            from webapp.delivery.samples import capture
                            capture(store, self, row, task)
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
                    except (OSError, ValueError) as error:
                        failed += 1
                        if len(failure_reasons) < 8:
                            from webapp.diagnostics import safe_text
                            failure_reasons.append(safe_text(str(error))[:300])
            if store:
                from webapp.delivery.samples import sweep
                sweep(store, self)
                self._clean_imports(store)
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
            empty_directories, empty_bytes = self._clean_empty_test_directories(grace)
            freed += empty_bytes
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
                                              failed=failed, pending_results=pending,
                                              empty_test_directories=empty_directories,
                                              failure_reasons=failure_reasons))
            self._refresh_usage(force=True)
            self._retention_health(store, failed, failure_reasons)
        return self.snapshot(force=True)

    def _clean_empty_test_directories(self, grace):
        """Remove only old empty task parents; never recurse into unknown files."""
        root = safe_path(self.directory / 'test-downloads', self.directory)
        if not root.is_dir():
            return 0, 0
        with self.connect(write=False) as db:
            owned = [Path(row[0]) for row in db.execute("SELECT path FROM leases WHERE category='test'")]
        removed, freed = 0, 0
        for path in root.iterdir():
            if not re.fullmatch(r'[0-9a-f]{32}', path.name) or any(
                    lease == path or lease.is_relative_to(path) for lease in owned):
                continue
            try:
                path = safe_path(path, self.directory)
                info = path.stat()
                if time.time() - info.st_mtime < grace:
                    continue
                path.rmdir()  # Atomic empty-directory check; files are never deleted.
                freed += max(info.st_size, getattr(info, 'st_blocks', 0)*512)
                removed += 1
            except (OSError, ValueError):
                continue
        return removed, freed

    def _retention_health(self, store, failed, failure_reasons=()):
        from datetime import datetime
        from sqlalchemy import select
        from webapp.database import tasks, delivery_work
        with self.connect(write=False) as db:
            leases = db.execute("SELECT task,path,measured,created FROM leases WHERE category='test'").fetchall()
            previous = self._get(db, 'retention_health', {})
        # Directory entries remain allocated after children are removed. They
        # are real budget usage, but are not unowned downloaded files. Inspect
        # only entries outside the retained workspaces; never count live growth
        # by subtracting two measurements taken at different times.
        untracked = self._untracked_test_bytes({Path(row['path']) for row in leases})
        untracked_since = (previous.get('untracked_since') or time.time()) if untracked > MIB else None
        known, local_work = {}, set()
        identities = [row['task'] for row in leases if row['task']]
        if store:
            for offset in range(0, len(identities), 500):
                with store.connect() as db:
                    known.update((row['id'], row) for row in db.execute(select(tasks.c.id, tasks.c.status,
                        tasks.c.finished_at).where(tasks.c.id.in_(identities[offset:offset+500]))).mappings())
                    local_work.update(db.execute(select(delivery_work.c.task_id).where(
                        delivery_work.c.task_id.in_(identities[offset:offset+500]),
                        delivery_work.c.instance_id == (store.fleet.id if store.fleet else 'local'))).scalars())
        health = dict(status='ok' if store else 'unavailable', sampled_at=time.time(), completed_count=0,
            completed_bytes=0, pending_count=0, pending_bytes=0, failed_count=failed,
            pending_processing_count=0, pending_processing_bytes=0,
            failed_processing_count=0,
            failure_reasons=list(failure_reasons), failure_checks=previous.get('failure_checks', 0)+1 if failed else 0,
            untracked_since=untracked_since, untracked_bytes=untracked,
            untracked_overdue=bool(untracked_since and time.time()-untracked_since > 600))
        for lease in leases:
            task = known.get(lease['task'])
            if task and task['status'] in ('queued', 'running'):
                health['pending_count'] += 1
                health['pending_bytes'] += lease['measured']
            elif task:
                try:
                    finished = datetime.fromisoformat(task['finished_at']).timestamp()
                except (TypeError, ValueError):
                    finished = lease['created']
                if time.time()-finished > 600:
                    health['completed_count'] += 1
                    health['completed_bytes'] += lease['measured']
                    if lease['task'] in local_work:
                        health['pending_processing_count'] += 1
                        health['pending_processing_bytes'] += lease['measured']
                        if task['status'] == 'failed':
                            health['failed_processing_count'] += 1
        with self.connect() as db:
            self._set(db, 'retention_health', health)

    def _untracked_test_bytes(self, owned):
        def visit(path):
            if path in owned or any(path.is_relative_to(root) for root in owned):
                return 0
            try:
                if path.is_symlink() or path.is_junction():
                    return 0
                if path.is_dir():
                    return sum(visit(child) for child in path.iterdir())
                return size_of(path)
            except (FileNotFoundError, NotADirectoryError):
                return 0
        return visit(self.directory / 'test-downloads')

    @staticmethod
    def receipts_recorded(store, task, path):
        from sqlalchemy import select
        from webapp.database import tasks
        from webapp.delivery.artifacts import recorded_test_output
        receipts = json.loads(path.read_text(encoding='utf-8'))
        if not receipts or not all(item.get('artifact') and item.get('complete') for item in receipts):
            return False
        with store.connect() as db:
            row = db.execute(select(tasks).where(tasks.c.id == task)).mappings().first()
            return bool(row and all(recorded_test_output(db, row, item['artifact']) for item in receipts))

    def recover_test_result(self, store, task_id):
        """Recover only typed delivery receipts owned by a newly claimed task."""
        from sqlalchemy import select
        from webapp.database import delivery_work
        with store.connect() as db:
            if db.execute(select(delivery_work.c.task_id).where(delivery_work.c.task_id == task_id)).first():
                return None
        with self.connect(write=False) as db:
            rows = db.execute("SELECT * FROM leases WHERE category='test' AND task=? ORDER BY created", (task_id,)).fetchall()
        for row in rows:
            if alive(json.loads(row['owner'])):
                continue
            with file_lock(self.directory / 'disk-locks' / (row['id'] + '.lock')) as owned:
                if not owned or any(alive(child) for child in json.loads(row['children'])):
                    continue
                root = safe_path(row['path'], self.directory)
                self.release_retained(row['id'])
                receipt_path = root / '.completed-files.json'
                if not receipt_path.is_file():
                    continue
                receipts = json.loads(receipt_path.read_text(encoding='utf-8'))
                task = store.get(task_id, private=True)
                if not task or not receipts or not all(item.get('artifact') for item in receipts):
                    continue
                from webapp.delivery.executor import recover_test_outputs
                result = recover_test_outputs(store, task_id, receipts, root)
                if not result and not self.receipts_recorded(store, task_id, receipt_path):
                    continue
                shutil.rmtree(root)
                with self.connect() as db:
                    db.execute('DELETE FROM leases WHERE id=?', (row['id'],))
                try:
                    root.parent.rmdir()
                except OSError:
                    pass
                if result:
                    result['files_deleted'] = True
                    return result
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

    def _clean_diagnostics(self, grace):
        for path in (self.runtime / 'cache').glob('worker-*'):
            if not re.fullmatch(r'worker-(\d+|maintenance|processing-[0-7])', path.name):
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
        self.processing = False

    def __enter__(self):
        self.lock = file_lock(self.budget.directory / 'disk-locks' / (self.id + '.lock'))
        if not self.lock.__enter__():
            raise DiskPressure('临时目录正在清理，请稍后重试。')
        try:
            if self.processing:
                with self.budget.connect() as db:
                    row = db.execute('SELECT * FROM leases WHERE id=?', (self.id,)).fetchone()
                    if (not row or alive(json.loads(row['owner'])) or
                            any(alive(child) for child in json.loads(row['children']))):
                        raise DiskPressure('下载进程尚未释放临时文件，等待本地处理交接。')
                    self.reserved = max(row['reserved'], size_of(self.path))
                    db.execute('UPDATE leases SET owner=?,children=? WHERE id=?',
                               (json.dumps(process_identity()), '[]', self.id))
            else:
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

    def _retire(self, *, quiescent=False):
        """Best-effort handoff; retained accounting is safer than losing ownership."""
        try:
            with self.budget.connect() as db:
                db.execute('UPDATE leases SET owner=?,children=? WHERE id=?',
                           (json.dumps({'pid': os.getpid(), 'created': 0}), json.dumps(self.children), self.id))
            if quiescent:
                self.budget.release_retained(self.id)
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
        needed = max(self.reserved, self.estimate, used if self.processing else math.ceil((used + unit) / unit) * unit)
        if self.processing and needed <= self.reserved:
            return  # Existing files can be moved out even under budget pressure.
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
        quiescent = not (self.monitor and self.monitor.is_alive())
        try:
            if self.monitor and self.monitor.is_alive():
                self.preserve = True
            if (self.error or kind) and self.on_pressure:
                try:
                    self.track_children()
                except (DiskPressure, OSError):
                    # Do not delete files if descendant tracking is unconfirmed.
                    self.preserve = True
                    quiescent = False
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
            quiescent = quiescent and not any(alive(child) for child in self.children)
            if self.preserve:
                self._retire(quiescent=quiescent)
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
            self._retire(quiescent=quiescent)
        finally:
            self.lock.__exit__(kind, value, traceback)
            if not self.path.exists():
                (self.budget.directory / 'disk-locks' / (self.id + '.lock')).unlink(missing_ok=True)
        if self.error and not isinstance(value, (KeyboardInterrupt, SystemExit)):
            raise self.error
