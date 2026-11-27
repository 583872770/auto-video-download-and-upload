"""Bounded storage for plain native HLS fragments."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from mmap import mmap
from pathlib import Path
from shutil import copyfileobj
from struct import Struct
from sys import getsizeof
from threading import Lock
import time

from yt_dlp.downloader.fragment import HttpQuietDownloader
from yt_dlp.utils import timeconvert

from webapp.disk_budget import file_lock
from webapp.resource_pressure import memory_thresholds
from webapp.throughput import resource_limits


HLS_FILE_BUFFER = 32 * 1024**2
HLS_INSTANCE_BUFFER = 256 * 1024**2
FRAGMENT_IO_BLOCK = 256 * 1024
USAGE = Struct('=I')


@contextmanager
def buffer_slot(directory):
    """Reserve one share of the instance's budget for a single media file."""
    for index in range(HLS_INSTANCE_BUFFER // HLS_FILE_BUFFER):
        acquired = False
        try:
            path = Path(directory) / 'hls-buffer' / f'{index}.lock'
            with file_lock(path) as acquired:
                if acquired:
                    yield path
                    return
        except OSError:
            if acquired:
                raise
            break
    yield None


def buffer_usage(directory):
    """Read only the counters that still belong to a held allocation."""
    used = occupied = 0
    total_slots = HLS_INSTANCE_BUFFER // HLS_FILE_BUFFER
    for index in range(total_slots):
        path = Path(directory) / 'hls-buffer' / f'{index}.lock'
        if not path.is_file():
            continue
        try:
            with file_lock(path) as free:
                if free:
                    continue
                with path.with_suffix('.usage').open('rb') as stream:
                    data = stream.read(USAGE.size)
                if len(data) != USAGE.size:
                    return None
                size, = USAGE.unpack(data)
                if size > HLS_FILE_BUFFER:
                    return None
                used += size
                occupied += 1
        except OSError:
            return None
    return dict(sample_time=time.time(), used_bytes=used, limit_bytes=HLS_INSTANCE_BUFFER,
                per_file_bytes=HLS_FILE_BUFFER, occupied_slots=occupied, total_slots=total_slots)


class OrderedFragmentPool(ThreadPoolExecutor):
    """Keep at most one worker's worth of lookahead per active connection."""
    def __init__(self, workers):
        super().__init__(max_workers=workers)
        self.window = workers * 2

    def map(self, fn, fragments):
        source = iter(fragments)
        pending = deque()

        def fill():
            while len(pending) < self.window:
                fragment = next(source, None)
                if fragment is None:
                    break
                pending.append(self.submit(fn, fragment))

        try:
            fill()
            while pending:
                yield pending.popleft().result()
                fill()
        finally:
            for future in pending:
                future.cancel()


class FragmentBuffers:
    def __init__(self, slot):
        self.lock = Lock()
        self.files = {}
        self.used = self.peak = self.memory_fragments = self.disk_fragments = 0
        self.next_memory_check = 0 if slot else float('inf')
        self.memory_available = False
        self.counter = None
        if slot:
            try:
                with slot.with_suffix('.usage').open('w+b') as stream:
                    stream.write(USAGE.pack(0))
                    stream.flush()
                    self.counter = mmap(stream.fileno(), USAGE.size)
            except (OSError, ValueError):
                pass  # Telemetry must never interrupt a media download.

    def reserve(self, size):
        with self.lock:
            now = time.monotonic()
            if now >= self.next_memory_check:
                memory = resource_limits()
                threshold, recovery = memory_thresholds(memory['memory_total'])
                minimum = threshold if self.memory_available else recovery
                self.memory_available = memory['memory_available'] >= minimum
                self.next_memory_check = now + 1
            if not self.memory_available or self.used + size > HLS_FILE_BUFFER:
                return False
            self.used += size
            if self.counter is not None:
                USAGE.pack_into(self.counter, 0, self.used)
            self.peak = max(self.peak, self.used)
            return True

    def release(self, size):
        with self.lock:
            self.used -= size
            if self.counter is not None:
                USAGE.pack_into(self.counter, 0, self.used)

    def clear(self):
        # The executor has joined before its buffer slot may be released.
        try:
            for body in self.files.values():
                body.discard()
            self.files.clear()
        finally:
            if self.counter is not None:
                self.counter.close()
                self.counter = None


class FragmentBody:
    def __init__(self, buffers, path):
        self.buffers, self.path = buffers, path
        self.blocks = deque()
        self.charged = 0
        self.disk = False
        self.stream = None

    def __bool__(self):
        return self.disk or bool(self.blocks)

    def write(self, data):
        # Charge the retained bytes object and its share of deque overhead.
        size = getsizeof(data) + 16
        if not self.disk:
            if self.buffers.reserve(size):
                self.blocks.append(data)
                self.charged += size
                return
            self.stream = open(self.path, 'wb')
            self.disk = True
            self.write_blocks(self.stream)
        self.stream.write(data)

    def write_blocks(self, stream):
        while self.blocks:
            data = self.blocks.popleft()
            size = getsizeof(data) + 16
            try:
                stream.write(data)
            finally:
                del data
                self.charged -= size
                self.buffers.release(size)

    def append_to(self, stream):
        if self.disk:
            with open(self.path, 'rb') as source:
                copyfileobj(source, stream, FRAGMENT_IO_BLOCK)
        else:
            self.write_blocks(stream)

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def discard(self):
        try:
            self.close()
        finally:
            self.blocks.clear()
            self.buffers.release(self.charged)
            self.charged = 0


class BufferedFragmentHttpFD(HttpQuietDownloader):
    """Keep yt-dlp's HTTP transport, progress hooks and retry handling."""
    def __init__(self, original, buffers):
        super().__init__(original.ydl, {**original.params,
            'buffersize': FRAGMENT_IO_BLOCK, 'noresizebuffer': True})
        self._progress_hooks = list(original._progress_hooks)
        self.buffers = buffers

    def sanitize_open(self, filename, mode):
        previous = self.buffers.files.get(filename)
        if previous is not None:
            previous.discard()
        body = FragmentBody(self.buffers, filename)
        if previous is not None and previous.disk:
            body.disk = True
            body.stream = open(filename, mode)
        self.buffers.files[filename] = body
        return body, filename

    def try_rename(self, old_filename, new_filename):
        body = self.buffers.files[old_filename]
        if body.disk:
            super().try_rename(old_filename, new_filename)
            body.path = new_filename
        if old_filename != new_filename:
            del self.buffers.files[old_filename]
            self.buffers.files[new_filename] = body

    def try_utime(self, filename, last_modified_hdr):
        if self.buffers.files[filename].disk:
            return super().try_utime(filename, last_modified_hdr)
        return timeconvert(last_modified_hdr) if last_modified_hdr is not None else None
