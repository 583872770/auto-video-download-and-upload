"""Small process-local caches for disposable display data, never execution rights."""
from collections import OrderedDict
from copy import deepcopy
import threading
import time


class ReadCache:
    def __init__(self, ttl=3, maximum=64):
        self.ttl, self.maximum = ttl, maximum
        self.values = OrderedDict()
        self.lock = threading.RLock()

    def read(self, key, load):
        # Serialize a cold read to avoid a polling burst rebuilding the same view.
        with self.lock:
            stamp = time.monotonic()
            entry = self.values.get(key)
            if entry and 0 <= stamp - entry[0] < self.ttl:
                self.values.move_to_end(key)
                return deepcopy(entry[1])
            value = load()
            self.values[key] = (time.monotonic(), deepcopy(value))
            self.values.move_to_end(key)
            while len(self.values) > self.maximum:
                self.values.popitem(last=False)
            return value

    def clear(self):
        with self.lock:
            self.values.clear()
