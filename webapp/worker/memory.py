"""Reclaim task-local graphs between attempts; never interrupt a live transfer."""
from contextlib import contextmanager
import ctypes
import gc
import sys

import psutil

MIB = 1024 ** 2
RECLAIM_GROWTH = 32 * MIB
RECYCLE_RSS = 256 * MIB


def trim_allocator():
    # glibc may keep freed network/player buffers in per-thread arenas. This
    # releases free pages; it does not cap buffers or alter download concurrency.
    if sys.platform != 'linux':
        return
    try:
        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        trim(0)
    except (AttributeError, OSError):
        pass


class WorkerMemory:
    def __init__(self):
        self.process = psutil.Process()
        self.baseline = self.process.memory_info().rss
        self.retire = False
        self.completed = 0

    @contextmanager
    def task(self):
        try:
            yield
        finally:
            self.completed += 1
            rss = self.process.memory_info().rss
            if rss > self.baseline + RECLAIM_GROWTH:
                gc.collect()
                trim_allocator()
                rss = self.process.memory_info().rss
            # A process still retaining excessive memory exits after settlement.
            # The existing supervisor refills its slot without reducing capacity.
            if rss > max(RECYCLE_RSS, self.baseline + 2 * RECLAIM_GROWTH):
                self.retire = True
                from webapp.diagnostics import event
                event('worker_retire', reason='retained_memory', rss_bytes=rss,
                      completed_attempts=self.completed, console=False)
