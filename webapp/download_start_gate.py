"""Local pre-claim waiting only; the database Cookie permit remains authoritative."""
import json
from pathlib import Path
import time
import uuid

from webapp.disk_budget import alive, file_lock, process_identity
from webapp.throughput import _atomic, policy_key
from webapp.wait_diagnostics import START_WAITS, start_wait


class StartTicket:
    def __init__(self, gate, token):
        self.gate, self.token = gate, token
        self.closed = False

    def finish(self, delay=1, *, wait=None):
        if self.closed:
            return
        with file_lock(self.gate.lock, blocking=True) as acquired:
            if not acquired:
                return  # A fenced, expiring hint cannot grant a source permit.
            state = self.gate.read()
            if state.get('token') == self.token:
                state.update(token=None, owner=None, expires_at=0,
                             next_at=time.time() + max(0, min(60, delay)), wait=wait)
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

    def status(self, config, stamp=None):
        """Publish only live local claim waits, without exposing owner tokens."""
        if not config.get('youtube_cookie_pool'):
            return {}
        stamp = time.time() if stamp is None else stamp
        state = self.read()
        if state.get('policy') != policy_key(config):
            return {}
        if state.get('next_at', 0) > stamp:
            return {'next_at': state['next_at']}
        if (state.get('token') and state.get('expires_at', 0) > stamp
                and alive(state.get('owner') or {})):
            return {'preparing_until': state['expires_at']}
        return {}

    def acquire(self):
        """Return (ticket, wait details or False). Old snapshots use DB admission.

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
        next_start = summary.get('next_start_at') or 0
        if summary.get('known') and not summary.get('capacity'):
            if summary.get('cooling_sessions'):
                reason = summary.get('next_start_reason') or 'account_cooldown'
                code = reason if reason in START_WAITS else 'account_cooldown'
                return None, start_wait(code, until=next_start if next_start > now else None)
            if summary.get('manual_sessions') or summary.get('effective_sessions') == 0:
                return None, start_wait('account_review')
            return None, start_wait('account_capacity')
        if next_start > now:
            reason = summary.get('next_start_reason')
            if reason in START_WAITS:
                return None, start_wait(reason, until=next_start)
            return None, dict(code='account_start_time', label='等待账号可启动', until=next_start,
                              detail='账号已上报下次可开工时间。具体限制尚未采到，可在账号管理查看。')
        with file_lock(self.lock) as acquired:
            if not acquired:
                return None, dict(code='start_check', label='等待启动检查',
                                  detail='其他进程正在更新本机启动状态，稍后会自动重试。')
            state = self.read()
            policy = snapshot.get('policy')
            if state.get('policy') != policy:
                state = {'policy': policy}
            if state.get('next_at', 0) > now:
                wait = state.get('wait') or start_wait('start_interval')
                return None, {**wait, 'until': state['next_at']}
            if state.get('token') and state.get('expires_at', 0) > now and alive(state.get('owner') or {}):
                return None, dict(code='another_start', label='等待其他进程完成启动',
                                  detail='其他下载进程正在为新任务准备账号。准备完成后会自动重新检查。')
            token = uuid.uuid4().hex
            state.update(token=token, owner=process_identity(), expires_at=now + 60)
            _atomic(self.path, state)
            return StartTicket(self, token), False

    def claim(self, store, *, actions=None, exclude=None):
        """Keep auxiliary check/import work runnable while downloads wait."""
        if actions and 'download' not in actions:
            return store.claim(store.owner, actions=actions, exclude=exclude), None, False
        ticket, waiting = self.acquire()
        if waiting:
            if actions and set(actions) == {'download'}:
                return None, None, waiting
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
