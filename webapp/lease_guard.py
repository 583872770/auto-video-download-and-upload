"""Serialize lease renewal with the worker's own task settlement."""
import threading


class LeaseGuard:
    def __init__(self, stop):
        self.stop = stop
        self.lock = threading.Lock()

    def renew(self, operation, *args, **kwargs):
        with self.lock:
            if self.stop.is_set():
                return None
            return operation(*args, **kwargs)

    def settle(self, operation, *args, **kwargs):
        # A completed/released task no longer matches the heartbeat UPDATE.
        # Publish stop under the same lock before a renewal can observe that.
        # Exceptions leave renewal active so error handling can still settle.
        with self.lock:
            result = operation(*args, **kwargs)
            self.stop.set()
            return result
