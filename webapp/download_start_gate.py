"""Local pre-claim waiting only; the database Cookie permit remains authoritative."""
import json
from pathlib import Path
import time
import uuid

from webapp.disk_budget import alive, file_lock, process_identity
from webapp.throughput import _atomic


class StartTicket:
    def __init__(self, gate, token):
        self.gate, self.token = gate, token
        self.closed = False

    def finish(self, delay=1):
        if self.closed:
            return
        with file_lock(self.gate.lock, blocking=True) as acquired:
            if not acquired:
                return  # A fenced, expiring hint cannot grant a source permit.
            state = self.gate.read()
            if state.get('token') == self.token:
                state.update(token=None, owner=None, expires_at=0,
                             next_at=time.time() + max(0, min(60, delay)))
                _atomic(self.gate.path, state)
            self.closed = True


class DownloadStartGate:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'download-start-gate.json'
        self.lock = self.directory / 'download-start-gate.lock'

    def read(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def acquire(self):
        """Return (ticket, waiting). Missing/old snapshots use original DB admission.

        Only one local worker may claim and prepare a pooled download at a time.
        That worker publishes the actual gate delay as soon as Cookie admission
        finishes, before transferring data. Dead workers cannot strand the hint.
        """
        now = time.time()
        try:
            snapshot = json.loads((self.directory / 'throughput.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None, False
        if snapshot.get('mode') != 'cookie_pool' or not 0 <= now - snapshot.get('time', 0) < 20:
            return None, False
        summary = snapshot.get('cookie_pool') or {}
        if summary.get('halted') or (summary.get('next_start_at') or 0) > now:
            return None, True
        with file_lock(self.lock) as acquired:
            if not acquired:
                return None, True
            state = self.read()
            policy = snapshot.get('policy')
            if state.get('policy') != policy:
                state = {'policy': policy}
            if state.get('next_at', 0) > now:
                return None, True
            if state.get('token') and state.get('expires_at', 0) > now and alive(state.get('owner') or {}):
                return None, True
            token = uuid.uuid4().hex
            state.update(token=token, owner=process_identity(), expires_at=now + 60)
            _atomic(self.path, state)
            return StartTicket(self, token), False

    def claim(self, store, *, actions=None, exclude=None):
        """Keep check/import/subtitle lanes runnable while downloads wait."""
        if actions and 'download' not in actions:
            return store.claim(store.owner, actions=actions, exclude=exclude), None, False
        ticket, waiting = self.acquire()
        if waiting:
            if actions and set(actions) == {'download'}:
                return None, None, True
            exclude = tuple(set(exclude or ()) | {'download'})
        try:
            task = store.claim(store.owner, actions=actions, exclude=exclude)
        except BaseException:
            if ticket:
                ticket.finish()
            raise
        if ticket and (not task or task['action'] != 'download'):
            ticket.finish()
            ticket = None
        return task, ticket, waiting
