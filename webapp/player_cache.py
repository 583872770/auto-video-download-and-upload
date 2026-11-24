"""Bounded, instance-local public player code; never store task challenges."""
from collections import defaultdict
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from urllib.parse import urlsplit

import yt_dlp.version
from yt_dlp.extractor.youtube import YoutubeIE
from yt_dlp.extractor.youtube.jsc._builtin.node import NodeJCP
from yt_dlp.extractor.youtube.jsc._director import validate_response
from yt_dlp.extractor.youtube.jsc.provider import JsChallengeProviderError

from webapp.disk_budget import MIB, GIB, file_lock, safe_path, size_of

MAX_BYTES = 128 * MIB
MAX_ENTRIES = 16
MAX_ENTRY_BYTES = 32 * MIB
TTL = 86400


def public_player(url):
    try:
        u = urlsplit(url)
        return (u.scheme == 'https' and u.netloc == 'www.youtube.com' and not u.query and not u.fragment
                and re.fullmatch(r'/s/player/[0-9a-fA-F]{8,}/[A-Za-z0-9_./-]+\.js', u.path)
                and '..' not in u.path.split('/'))
    except (TypeError, ValueError):
        return False


def note(action, **values):
    from webapp.diagnostics import event
    event('player_cache', action=action, **values)


class PlayerCache:
    def __init__(self, state, config):
        self.state = Path(state)
        self.root = safe_path(self.state / 'runtime/cache/youtube-players', self.state)
        self.category_bytes = max(0, int(config.get('disk_cache_budget_mb', 256))) * MIB
        self.maximum = min(MAX_BYTES, self.category_bytes)
        self.minimum_free = max(0, int(config.get('disk_min_free_gb', 2))) * GIB
        self.local = threading.local()

    @property
    def active(self):
        return self.maximum > 0 and not getattr(self.local, 'bypassed', False)

    @contextmanager
    def bypass(self):
        previous = getattr(self.local, 'bypassed', False)
        self.local.bypassed = True
        try:
            yield
        finally:
            self.local.bypassed = previous

    @staticmethod
    def key(url):
        return hashlib.sha256(url.encode()).hexdigest()

    @contextmanager
    def lock(self, url, *, blocking=True):
        with self.key_lock(self.key(url), blocking=blocking) as owned:
            yield owned

    @contextmanager
    def key_lock(self, key, *, blocking=True):
        # Fixed stripes also bound lock-file growth when players change.
        stripe = int(key[:2], 16) % 16
        held = getattr(self.local, 'held', set())
        if stripe in held:
            yield True
            return
        with ExitStack() as stack:
            try:
                path = safe_path(self.root / f'lock-{stripe}.lock', self.state)
                owned = stack.enter_context(file_lock(path, blocking=blocking))
            except (OSError, ValueError) as error:
                note('unavailable', reason=type(error).__name__)
                yield False
                return
            if owned:
                self.local.held = held | {stripe}
            try:
                yield owned
            finally:
                self.local.held = held

    def path(self, url):
        return safe_path(self.root / (self.key(url) + '.json'), self.state)

    def discard(self, url):
        try:
            self.path(url).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def read(self, url):
        try:
            path = self.path(url)
            if path.stat().st_size > MAX_ENTRY_BYTES:
                raise ValueError('oversized entry')
            value = json.loads(path.read_text(encoding='utf-8'))
            if (value['schema'] != 1 or value['url'] != url or not isinstance(value['created'], (int, float))
                    or not 0 <= time.time() - value['created'] < TTL):
                raise ValueError('expired entry')
            for kind in ('source', 'prepared'):
                item = value.get(kind)
                if item is not None and (not isinstance(item['code'], str) or not item['code']
                        or hashlib.sha256(item['code'].encode()).hexdigest() != item['sha256']):
                    raise ValueError('invalid code')
            if not value.get('source'):
                raise ValueError('missing source')
            os.utime(path, None)
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
            self.discard(url)
            note('invalid', reason=type(error).__name__)
            return None

    @staticmethod
    def blob(code, **values):
        return dict(code=code, sha256=hashlib.sha256(code.encode()).hexdigest(), **values)

    def write(self, url, source, prepared=None, compat=None):
        if not source:
            return False
        value = dict(schema=1, url=url, created=time.time(), source=self.blob(source))
        if prepared:
            value['prepared'] = self.blob(prepared, compat=compat)
        payload = json.dumps(value, ensure_ascii=False).encode()
        if len(payload) > min(MAX_ENTRY_BYTES, self.maximum):
            note('skipped', reason='entry_size')
            return False
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with file_lock(safe_path(self.root / 'quota.lock', self.state), blocking=True) as owned:
                if not owned:
                    return False
                # The caller owns this entry's stripe. Replacing disposable
                # code may drop the old copy before staging, bounding peak use.
                self.discard(url)
                reserve = (len(payload) + 4095) // 4096 * 4096
                if not self._prune(reserve=reserve, protected=self.key(url))['fits']:
                    note('skipped', reason='capacity_or_space')
                    return False
                fd, name = tempfile.mkstemp(prefix='write-', suffix='.tmp', dir=self.root)
                temporary = Path(name)
                with os.fdopen(fd, 'wb') as output:
                    output.write(payload)
                    output.flush()
                os.replace(temporary, self.path(url))
                note('stored', kind='prepared' if prepared else 'source', bytes=len(payload))
                return True
        except (OSError, ValueError) as error:
            note('unavailable', reason=type(error).__name__)
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prune(self, *, reserve=0, protected=None, pressure=False):
        # quota.lock is held. Other stripes are acquired without waiting, so
        # builders may wait for quota without a lock-order deadlock.
        paths = []
        removed = freed = 0
        for stripe in range(16):
            lock = safe_path(self.root / f'lock-{stripe}.lock', self.state)
            try:
                with lock.open('xb') as stream:
                    stream.write(b'0')
            except FileExistsError:
                pass
        for path in self.root.iterdir():
            safe_path(path, self.state)
            if path.name.startswith('write-') and path.suffix == '.tmp':
                # Writers hold quota.lock until rename. Remaining temps are
                # from terminated writers and cannot be live here.
                freed += size_of(path);path.unlink();removed += 1
            elif re.fullmatch(r'[0-9a-f]{64}\.json', path.name):
                meta = path.stat()
                paths.append((meta.st_mtime, path, size_of(path)))
        other = sum(size_of(p) for p in self.root.parent.iterdir() if p != self.root)
        overhead = max(0, size_of(self.root) - sum(item[2] for item in paths))
        capacity = max(0, min(self.maximum, self.category_bytes - other) - overhead)
        used = sum(item[2] for item in paths)
        free = shutil.disk_usage(self.root).free
        count = len(paths)
        for modified, path, amount in sorted(paths):
            expired = time.time() - modified >= TTL
            # Read only the small header's created value via the bounded entry
            # parser when an accessed entry's mtime no longer indicates age.
            if not expired:
                try:
                    if path.stat().st_size > MAX_ENTRY_BYTES:
                        expired = True
                    else:
                        expired = not 0 <= time.time() - json.loads(path.read_text(encoding='utf-8'))['created'] < TTL
                except (ValueError, KeyError, TypeError, UnicodeError):
                    expired = True
            if not (expired or pressure or used + reserve > capacity
                    or count + bool(reserve) > MAX_ENTRIES or free - reserve < self.minimum_free):
                continue
            key = path.stem
            if key == protected:
                continue
            # A colliding stripe in this thread can be removed safely unless
            # it is the protected key. No other thread owns that stripe.
            with self.key_lock(key, blocking=False) as owned:
                if not owned:
                    continue
                path.unlink(missing_ok=True)
                used -= amount;count -= 1;free += amount;removed += 1;freed += amount
        fits = (not pressure and used + reserve <= capacity and count + bool(reserve) <= MAX_ENTRIES
                and free - reserve >= self.minimum_free)
        return dict(removed=removed, freed_bytes=freed, used_bytes=used, entries=count, fits=fits)

    def prune(self, *, pressure=False):
        if not self.root.exists():
            return dict(removed=0, freed_bytes=0)
        try:
            with file_lock(safe_path(self.root / 'quota.lock', self.state)) as owned:
                if not owned:
                    return dict(removed=0, freed_bytes=0)
                result = self._prune(pressure=pressure)
                if result['removed']:
                    note('evicted', **result)
                return result
        except (OSError, ValueError) as error:
            note('unavailable', reason=type(error).__name__)
            return dict(removed=0, freed_bytes=0)


def cache_options(config):
    state = config.get('_disk_state')
    if state:
        return {'_player_cache': dict(state=str(state), config={key:config[key] for key in
            ('disk_cache_budget_mb', 'disk_min_free_gb') if key in config})}
    return {}



class CachedNodeJCP(NodeJCP):
    PROVIDER_NAME = 'node'
    PROVIDER_KEY = 'Node'

    def _get_player(self, video_id, player_url):
        if self.prepared:
            return ''
        self.source = super()._get_player(video_id, player_url)
        return self.source

    def _construct_stdin(self, player, preprocessed, requests):
        return super()._construct_stdin(self.prepared or player, bool(self.prepared), requests)

    def _run_js_runtime(self, stdin):
        output = super()._run_js_runtime(stdin)
        self.built = json.loads(output).get('preprocessed_player')
        return output

    def _solve(self, group):
        replies = list(super()._real_bulk_solve(group))
        valid = len(replies) == len(group) and all(r.response is not None and r.error is None
            and validate_response(r.response, r.request) is True for r in replies)
        return replies, valid

    def _build(self, url, group, compat):
        cache = self.ie.public_player_cache
        replies, valid = self._solve(group)
        if valid and isinstance(self.built, str) and self.built:
            cache.write(url, self.source, self.built, compat)
        return replies

    def _real_bulk_solve(self, requests):
        try:
            yield from self._cached_bulk_solve(requests)
        finally:
            # Large task-local strings must not survive until cyclic GC.
            self.prepared = self.source = self.built = None

    def _cached_bulk_solve(self, requests):
        cache = self.ie.public_player_cache
        groups = defaultdict(list)
        for request in requests:
            groups[request.input.player_url].append(request)
        for url, group in groups.items():
            self.prepared = self.source = self.built = None
            if not cache.active or not public_player(url):
                with cache.bypass():
                    yield from super()._real_bulk_solve(group)
                continue
            started = time.monotonic()
            compat = f'1:{yt_dlp.version.__version__}:{self._SCRIPT_VERSION}:{self.runtime_info.version_tuple}'
            replies = None
            with cache.lock(url) as owned:
                if not owned:
                    with cache.bypass():
                        yield from super()._real_bulk_solve(group)
                    continue
                saved = cache.read(url)
                item = (saved or {}).get('prepared') or {}
                self.prepared = item.get('code') if item.get('compat') == compat else None
                hit = bool(self.prepared)
                note('hit' if hit else 'miss', kind='prepared')
                if not hit:
                    replies = self._build(url, group, compat)
            # Code has been copied into this task's memory. Warm solving
            # must not hold the file lock or serialize other task inputs.
            if hit:
                failed_code = self.prepared
                try:
                    replies, valid = self._solve(group)
                    if not valid:
                        raise JsChallengeProviderError('Cached player result failed validation')
                except (JsChallengeProviderError, ValueError, KeyError, TypeError):
                    self.prepared = self.source = self.built = None
                    self.ie._code_cache.pop(self.ie._player_js_cache_key(url), None)
                    note('rebuild', reason='cached_solver_failed')
                    with cache.lock(url) as owned:
                        if owned:
                            # A different task may already have replaced
                            # the bad entry while this one was computing.
                            saved = cache.read(url)
                            item = (saved or {}).get('prepared') or {}
                            if item.get('compat') == compat and item.get('code') != failed_code:
                                self.prepared = item.get('code')
                                replies = None
                            else:
                                cache.discard(url)
                                replies = self._build(url, group, compat)
                        else:
                            with cache.bypass():
                                replies, _ = self._solve(group)
                    if replies is None:
                        replies, _ = self._solve(group)
                    hit = False
            note('solved', hit=hit, seconds=round(time.monotonic()-started, 4))
            yield from replies

class CachedYoutubeIE(YoutubeIE):
    def __init__(self, downloader, cache):
        super().__init__(downloader)
        self.public_player_cache = cache

    @classmethod
    def ie_key(cls):
        return 'Youtube'

    def _real_initialize(self):
        super()._real_initialize()
        original = self._jsc_director.providers.get('Node')
        if original is not None:
            self._jsc_director.providers['Node'] = CachedNodeJCP(self, original.logger, original.settings)

    def _load_player(self, video_id, player_url, fatal=True):
        cache = self.public_player_cache
        key = self._player_js_cache_key(player_url)
        if key in self._code_cache or not cache.active or not public_player(player_url):
            return super()._load_player(video_id, player_url, fatal=fatal)
        with cache.lock(player_url) as owned:
            if not owned:
                with cache.bypass():
                    return super()._load_player(video_id, player_url, fatal=fatal)
            saved = cache.read(player_url)
            if saved:
                self._code_cache[key] = saved['source']['code']
                note('hit', kind='source')
                return self._code_cache[key]
            note('miss', kind='source')
            source = super()._load_player(video_id, player_url, fatal=fatal)
            if source:
                cache.write(player_url, source)
            return source


def install(downloader):
    options = downloader.params.get('_player_cache')
    if not options:
        return
    try:
        cache = PlayerCache(options['state'], options['config'])
    except (OSError, ValueError) as error:
        note('unavailable', reason=type(error).__name__)
        return
    downloader.add_info_extractor(CachedYoutubeIE(downloader, cache))
