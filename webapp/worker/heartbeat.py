"""Task lease renewal and watchdog ownership for a single execution."""
import os
import threading
import time

from webapp.diagnostics import event, error_details
from webapp.lease_guard import LeaseGuard


class LeaseHeartbeat:
    def __init__(self, store, task_id, name, context):
        self.store = store
        self.task_id = task_id
        self.name = name
        self.context = context
        self.stop = threading.Event()
        self.guard = LeaseGuard(self.stop)
        self.deadline = time.monotonic() + 45
        self.heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.watchdog_thread = threading.Thread(target=self._watchdog, daemon=True) if store.fleet else None

    def start(self):
        self.heartbeat_thread.start()
        if self.watchdog_thread:
            self.watchdog_thread.start()

    def close(self):
        self.stop.set()
        self.heartbeat_thread.join(timeout=2)
        if self.watchdog_thread:
            self.watchdog_thread.join(timeout=2)

    def _abandon(self, reason):
        from webapp.control import terminate_own_descendants
        try:
            event('lease_abandoned', context=self.context, reason=reason, console=False)
            terminate_own_descendants()
        finally:
            os._exit(2)

    def _heartbeat(self):
        last_error = 0
        while not self.stop.wait(10):
            try:
                renewal_started = time.monotonic()
                owned = self.guard.renew(self.store.heartbeat, self.task_id, self.store.owner)
                if self.stop.is_set():
                    return
                if not owned:
                    self._abandon('heartbeat_ownership_lost')
                self.deadline = renewal_started + 45
                self.store.worker_status(self.store.owner, self.name, 'running', self.task_id)
            except Exception as error:
                if time.monotonic() - last_error >= 30:
                    last_error = time.monotonic()
                    event('heartbeat_error', context=self.context, console=False, **error_details(error))

    def _watchdog(self):
        while not self.stop.wait(1):
            if time.monotonic() >= self.deadline:
                self._abandon('heartbeat_deadline_exceeded')
