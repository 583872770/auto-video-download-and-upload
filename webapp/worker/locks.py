"""Cross-process worker exclusivity."""
from contextlib import contextmanager


@contextmanager
def worker_lock(path):
    from webapp.disk_budget import file_lock
    with file_lock(path) as acquired:
        yield acquired
