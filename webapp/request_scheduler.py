"""Account and source permits, fenced by existing task leases.

All admissions use short transactions. HTTP, disk and backoff waits
take place AFTER the transaction closes. Scope IDs contain no signed URLs.
"""
from contextlib import contextmanager
import hashlib
import json
import random
import time
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from webapp.database import download_permits as permits, download_resources as resources, tasks
from webapp.throughput import PipelineBusy
from webapp.concurrency_policy import live_policies, resource_capacity
from webapp.download_policy import (failure_evidence,
                                    cooldown_deadline, remember_decision)


def throttle_details(error):
    """Actual HTTP status only; account limits are a separate failure kind."""
    if error is None:
        return None, None
    failure = failure_evidence(error)
    delay = max(3600, failure.retry_after or 0) if failure.kind == 'account_rate_limit' else failure.retry_after
    return failure.http_status, delay


def _key(scope):
    return hashlib.sha256(scope.encode()).hexdigest()


def local_failure(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if (type(error).__name__ in ('DownloadLeaseLost', 'DiskPressure', 'DiskCoordinationBusy',
                'DownloadEnvironmentRequired', 'YouTubeAuthRequired', 'PipelineBusy')
                or isinstance(error, SQLAlchemyError)
                or isinstance(error, OSError) and error.errno in (5, 13, 28, 30, 122)):
            return True
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return False


class RequestScheduler:
    def __init__(self, store, config, task_id=None, *, base=None):
        self.store, self.config, self.task_id = store, config, task_id
        self.instance = store.fleet.id if store and store.fleet else 'local'
        self.enabled = bool(store and task_id)
        self.blocked_resource = None

    def clock(self, db):
        if self.store.fleet:
            return self.store.fleet.clock(db)
        from webapp.fleet import database_time
        return database_time(db, self.store.db.mysql)

    @contextmanager
    def transaction(self):
        with self.store.connect() as db:
            if self.store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            # Independent of fleet_budget: source request waits never hold
            # up task claims or task heartbeats.
            key = _key('router-v1')
            if db.execute(select(resources.c.key).where(resources.c.key == key)).first() is None:
                self.store.db.ensure(db, resources, {'key': key, 'state': '{}'})
            db.execute(select(resources.c.key).where(resources.c.key == key).with_for_update()).first()
            yield db, self.clock(db)

    def transaction_call(self, operation):
        from webapp.db_retry import transaction_retry
        def run():
            with self.transaction() as (db, stamp):
                return operation(db, stamp)
        return transaction_retry(run)

    def _state(self, db, scope):
        key = _key(scope)
        row = db.execute(select(resources.c.state).where(resources.c.key == key)).scalar_one_or_none()
        if row is None:
            self.store.db.ensure(db, resources, {'key': key, 'state': '{}'})
        return json.loads(row or '{}')

    @staticmethod
    def _save(db, scope, state):
        db.execute(resources.update().where(resources.c.key == _key(scope)).values(state=json.dumps(state)))

    def _active(self, db, scope, stamp):
        # MySQL SUM(integer) returns Decimal; SQLite returns int. Resource units
        # are integers on both backends, including the empty COALESCE result.
        return int(db.execute(select(func.coalesce(func.sum(permits.c.units), 0)).select_from(
            permits.join(tasks, (permits.c.task_id == tasks.c.id) & (permits.c.owner == tasks.c.worker_id)))
            .where(permits.c.resource == _key(scope), tasks.c.status == 'running', tasks.c.lease_until > stamp)).scalar_one())

    def snapshot(self, scopes):
        """Display-only batch read; never create rows or acquire router-v1.

        Admission still uses transaction_call and live owner/lease checks. These
        READ COMMITTED observations can differ slightly while tasks transition.
        """
        with self.store.connect() as db:
            stamp = self.clock(db)
            states = self._states(db, scopes)
            active = self._active_many(db, scopes, stamp)
        return states, active, stamp

    @staticmethod
    def _states(db, scopes):
        keys = {_key(scope): scope for scope in scopes}
        return {keys[key]: json.loads(value or '{}') for key, value in db.execute(
            select(resources.c.key, resources.c.state).where(resources.c.key.in_(keys)))}

    @staticmethod
    def _active_many(db, scopes, stamp):
        keys = {_key(scope): scope for scope in scopes}
        return {keys[key]: int(value) for key, value in db.execute(select(
                permits.c.resource, func.sum(permits.c.units)).select_from(permits.join(
                    tasks, (permits.c.task_id == tasks.c.id) & (permits.c.owner == tasks.c.worker_id))).where(
                        permits.c.resource.in_(keys), tasks.c.status == 'running', tasks.c.lease_until > stamp
                    ).group_by(permits.c.resource))}

    def _owned(self, db, stamp):
        if self.store.fleet and self.store.fleet.row(db)['boot_id'] != self.store.fleet.boot_id:
            return False
        return db.execute(select(tasks.c.id).where(tasks.c.id == self.task_id,
            tasks.c.status == 'running', tasks.c.worker_id == self.store.owner,
            tasks.c.lease_until > stamp)).first()

    def _admit(self, db, stamp, requirements):
        self.blocked_resource = None
        if not self._owned(db, stamp):
            from webapp.downloads import DownloadLeaseLost
            raise DownloadLeaseLost('请求准入时任务租约已失效。')
        for scope, cap, units in requirements:
            state = self._state(db, scope)
            if state.get('cooldown_until', 0) > stamp:
                self.blocked_resource = scope.split(':', 1)[0] + ':cooldown'
                return None, state['cooldown_until'] - stamp
            # Take the minimum of live instance policies for this shared group.
            policies = live_policies(state, stamp)
            if cap is None:
                # Unlimited participants publish no numeric ceiling.
                policies.pop(self.instance, None)
            else:
                policies[self.instance] = {'limit': cap, 'time': stamp}
            state['policies'] = policies
            capacity = resource_capacity(cap, state, stamp)
            self._save(db, scope, state)
            if capacity.effective is not None and self._active(db, scope, stamp) + units > capacity.effective:
                self.blocked_resource = scope.split(':', 1)[0]
                return None, 2
        token = uuid.uuid4().hex
        for scope, cap, units in requirements:
            state = self._state(db, scope)
            if stamp - state.get('permit_cleanup_at', 0) >= 30:
                self._cleanup_permits(db, scope, stamp)
                state['permit_cleanup_at'] = stamp
                self._save(db, scope, state)
            db.execute(permits.insert().values(token=token, resource=_key(scope), task_id=self.task_id,
                                               owner=self.store.owner, units=units))
        return token, 0

    def _cleanup_permits(self, db, scope, stamp):
        # A DELETE containing a task subquery takes task record locks in MySQL.
        # An importer can then block this transaction while it owns router-v1.
        # Select stale immutable permit IDs with an ordinary MVCC read, then
        # delete only those IDs. Admission still counts live owner-fenced tasks.
        stale = db.execute(select(permits.c.token).select_from(permits.outerjoin(
            tasks, tasks.c.id == permits.c.task_id)).where(
                permits.c.resource == _key(scope), or_(tasks.c.id.is_(None),
                    tasks.c.status != 'running', tasks.c.worker_id.is_(None),
                    tasks.c.worker_id != permits.c.owner, tasks.c.lease_until.is_(None),
                    tasks.c.lease_until <= stamp)).limit(256)).scalars().all()
        if stale:
            db.execute(permits.delete().where(permits.c.resource == _key(scope),
                                             permits.c.token.in_(stale)))

    def _release(self, token):
        def release(db, stamp):
            db.execute(permits.delete().where(permits.c.token == token, permits.c.owner == self.store.owner))
        self.transaction_call(release)


    def _check_cookie_cooldown(self, db, stamp):
        scope = self.config.get('_cookie_pool_scope')
        if not scope or not scope.startswith('youtube-account:'):
            return
        state = self._state(db, scope)
        if state.get('halted'):
            error = PipelineBusy('该账号需要人工处理登录或验证；其他健康账号继续。')
            error.retry_after = 60
            raise error
        until = state.get('cooldown_until', 0)
        if until > stamp:
            name = self.config.get('_cookie_pool_profile_name', self.config.get('_cookie_pool_selected'))
            error = PipelineBusy(f'账号「{name}」正在冷却，已保留任务等待；其他健康账号继续。')
            error.retry_after = max(2, min(60, until - stamp))
            raise error


    @contextmanager
    def permit(self, phase, *, on_wait=None, timeout=60):
        if not self.enabled:
            raise RuntimeError('下载请求必须通过账号池任务执行。')
        deadline = time.monotonic() + timeout
        while True:
            # Account assignment and supervisor sizing already admit the task.
            # In-flight tasks finish after a resource reduction; do not create
            # a second transfer queue or charge fragments as task slots.
            def admit(db, stamp):
                if not self._owned(db, stamp):
                    from webapp.downloads import DownloadLeaseLost
                    raise DownloadLeaseLost('请求准入时任务租约已失效。')
                self._check_cookie_cooldown(db, stamp)
                health = self._state(db, 'source:youtube')
                if health.get('cooldown_until', 0) > stamp:
                    self.blocked_resource = 'source:youtube'
                    return None, health['cooldown_until'] - stamp
                return True, 0
            # Read-only admission has no router lock, no resource-row writes,
            # and no SQL permit rows. Account assignment owns the sole slot.
            with self.store.connect() as db:
                token, delay = admit(db, self.clock(db))
            if token:
                break
            if on_wait:
                on_wait()
            if delay > 2 or time.monotonic() >= deadline:
                error = PipelineBusy('来源服务暂时故障，等待恢复。' if self.blocked_resource == 'source:youtube'
                                     else '服务器资源保护正在收敛并发，稍后继续。')
                error.retry_after = max(2, min(60, delay)) + random.uniform(0, 2)
                raise error
            time.sleep(.5 + random.uniform(0, .2))
        from webapp.request_activity import RequestActivity
        activity = RequestActivity(self.store)
        token = activity.enter(self.task_id, self.store.owner, self.instance, phase)
        try:
            scope = self.config.get('_cookie_pool_scope')
            if phase == 'resolve' and scope:
                def consume(db, stamp):
                    if not self._owned(db, stamp):
                        from webapp.downloads import DownloadLeaseLost
                        raise DownloadLeaseLost('解析前任务租约已失效。')
                    state = self._state(db, scope)
                    pending = state.get('pending_starts', {})
                    if pending.get(self.task_id) == self.config.get('_cookie_pool_admitted_at'):
                        pending.pop(self.task_id)
                        self._save(db, scope, state)
                self.transaction_call(consume)
            yield
        finally:
            activity.leave(token)

    def outcome(self, *, success, error=None, phase='resolve'):
        if not self.enabled:
            return
        failure = failure_evidence(error, source='youtube', phase=phase, task_id=self.task_id,
            profile_id=self.config.get('_cookie_pool_selected'),
            profile_name=self.config.get('_cookie_pool_profile_name')) if error else None
        # Account throttling belongs exclusively to CookiePool. A bad video,
        # proxy, disk or login must never cool every healthy account.
        if failure and not (failure.kind == 'transient_failure' and failure.http_status
                            and 500 <= failure.http_status < 600):
            return
        def record(db, stamp):
            key = 'source:youtube'
            state = self._state(db, key)
            if success:
                state.pop('server_failures', None)
            else:
                if state.get('last_decision', {}).get('incident_id') == failure.incident_id:
                    return
                samples = [item for item in state.get('server_failures', [])
                           if item['time'] > stamp - 60 and item['task'] != self.task_id]
                samples.append({'task': self.task_id, 'time': stamp})
                state['server_failures'] = samples[-16:]
                if len(samples) >= 3:
                    state['cooldown_until'] = cooldown_deadline(error, stamp, max(30, failure.retry_after or 0))
                remember_decision(state, failure, layer='source', scope='YouTube 服务端故障',
                    action='cooldown' if state.get('cooldown_until', 0) > stamp else 'record',
                    until=state.get('cooldown_until', 0), stamp=stamp)
            self._save(db, key, state)
        self.transaction_call(record)
