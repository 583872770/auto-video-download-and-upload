"""Task-level routing and shared permits, fenced by existing task leases.

All admissions use short transactions. Browser, HTTP, disk and backoff waits
take place AFTER the transaction closes. Scope IDs contain no signed URLs.
"""
from contextlib import contextmanager
from dataclasses import replace
from email.utils import parsedate_to_datetime
import hashlib
import json
import random
import time
import uuid

from sqlalchemy import func, select

from webapp.channels import group_caps, observed_group, registry
from webapp.database import download_permits as permits, download_resources as resources, tasks
from webapp.throughput import PipelineBusy, runtime_limits


def retry_after(value, now=None):
    now = time.time() if now is None else now
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(str(value)).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    if not 0 <= seconds < float('inf'):
        return None
    return min(86400, max(1, seconds))


def throttle_details(error):
    seen = set()
    limited = False
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        status = getattr(error, 'http_status', None) or getattr(error, 'status', None)
        if status in (429, 503):
            headers = getattr(error, 'headers', None) or {}
            value = getattr(error, 'retry_after', None) or headers.get('Retry-After') or headers.get('retry-after')
            return status, retry_after(value)
        # yt-dlp preserves the underlying networking.HTTPError as a cause.
        if '429' in str(error) and any(x in str(error).lower() for x in ('http', 'rate')):
            limited = True
        error = error.__cause__ or error.__context__
    return (429, None) if limited else (None, None)


def _key(scope):
    return hashlib.sha256(scope.encode()).hexdigest()


class ChannelScheduler:
    def __init__(self, store, config, task_id=None):
        self.store, self.config, self.task_id = store, config, task_id
        self.channels = registry(config)
        self.caps = group_caps(self.channels)
        self.instance = store.fleet.id if store and store.fleet else 'local'
        self.enabled = bool(store and task_id)

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
            # Independent of fleet_budget: browser and transfer waits never hold
            # up task claims or task heartbeats.
            key = _key('router-v1')
            self.store.db.ensure(db, resources, {'key': key, 'state': '{}'})
            db.execute(select(resources.c.key).where(resources.c.key == key).with_for_update()).first()
            yield db, self.clock(db)

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

    def _owned(self, db, stamp):
        if self.store.fleet and self.store.fleet.row(db)['boot_id'] != self.store.fleet.boot_id:
            return False
        return db.execute(select(tasks.c.id).where(tasks.c.id == self.task_id,
            tasks.c.status == 'running', tasks.c.worker_id == self.store.owner,
            tasks.c.lease_until > stamp)).first()

    def _admit(self, db, stamp, requirements):
        if not self._owned(db, stamp):
            from webapp.downloads import DownloadLeaseLost
            raise DownloadLeaseLost('渠道准入时任务租约已失效。')
        for scope, cap, units in requirements:
            state = self._state(db, scope)
            if state.get('cooldown_until', 0) > stamp:
                return None, state['cooldown_until'] - stamp
            # Take the minimum of live instance policies for this shared group.
            policies = {k: v for k, v in state.get('policies', {}).items() if v['time'] > stamp - 90}
            policies[self.instance] = {'limit': cap, 'time': stamp}
            state['policies'] = policies
            cap = min(v['limit'] for v in policies.values())
            if state.get('recover_until', 0) > stamp:
                cap = 1
            self._save(db, scope, state)
            if self._active(db, scope, stamp) + units > cap:
                return None, 2
        token = uuid.uuid4().hex
        for scope, cap, units in requirements:
            # Clean abandoned rows using the same task fence as admission.
            valid = select(tasks.c.id).where(tasks.c.status == 'running', tasks.c.lease_until > stamp)
            db.execute(permits.delete().where(permits.c.resource == _key(scope), permits.c.task_id.not_in(valid)))
            db.execute(permits.insert().values(token=token, resource=_key(scope), task_id=self.task_id,
                                               owner=self.store.owner, units=units))
        return token, 0

    def _release(self, token):
        with self.store.connect() as db:
            db.execute(permits.delete().where(permits.c.token == token, permits.c.owner == self.store.owner))

    @contextmanager
    def assignment(self, candidates, *, allow_fallback=False):
        """Choose a group before an entrance; aliases do not gain more share."""
        if not self.enabled:
            yield candidates[0]
            return
        with self.transaction() as (db, stamp):
            available = []
            for order, channel in enumerate(candidates):
                if not allow_fallback and channel.adapter != candidates[0].adapter:
                    continue
                scopes = ['channel:' + channel.id, 'resolver:' + channel.resolver_group,
                          'quota:' + channel.quota_group, 'upstream:' + channel.upstream_group]
                if any(self._state(db, key).get('cooldown_until', 0) > stamp for key in scopes):
                    continue
                local = runtime_limits(self.config, self.store.directory)
                limit = min(channel.transfer_limit, local.get('groups', {}).get(channel.upstream_group, {}).get('limit', local['transfers']))
                if channel.upstream_group == 'unverified-media':
                    limit = min(channel.transfer_limit, max(limit, local.get('groups', {}).get('youtube-origin', {}).get('limit', limit)))
                backlog = 'local-assignment:' + self.instance + ':' + channel.upstream_group
                if self._active(db, backlog, stamp) >= limit + min(2, local['browsers']) + local['browsers']:
                    continue
                state = self._state(db, 'channel:' + channel.id)
                group = self._state(db, 'assignment:' + channel.upstream_group)
                active = self._active(db, 'assignment:' + channel.upstream_group, stamp)
                # Active load dominates. Decaying recent starts prevent a quick
                # resolver from monopolizing the next available task.
                recent = group.get('recent', 0) * .5 ** (max(0, stamp - group.get('time', stamp)) / 60)
                rate = state.get('rate', 0)
                estimate = 64 * 1024 ** 2 / rate if rate > 0 else 30
                cost = (active + 1 + recent * .1) * min(300, max(5, estimate))
                cost /= channel.weight
                cost *= 1 + min(3, state.get('failures', 0)) * .5
                # Explore at most once per 20 selections, deterministically.
                routing = self._state(db, 'routing-count')
                count = routing.get('count', 0)
                if count % 20 == 19:
                    cost = group.get('time', 0)  # oldest group gets the probe
                available.append((cost if self.config.get('scheduler_mode') == 'balanced' else order, order, channel))
            if not available:
                raise PipelineBusy('可用渠道正在退避，稍后重新调度。')
            # Provider order also encodes quality/authentication choices. Balance
            # compatible entrances within that provider, retain provider fallback.
            first_adapter = min(available, key=lambda item:item[1])[2].adapter
            available = [item for item in available if item[2].adapter == first_adapter]
            channel = min(available, key=lambda item: item[:2])[2]
            scope = 'assignment:' + channel.upstream_group
            token, delay = self._admit(db, stamp, [(scope, 4096, 1),
                ('local-assignment:' + self.instance + ':' + channel.upstream_group, 256, 1)])
            if not token:
                raise PipelineBusy('上游准备队列繁忙。')
            group = self._state(db, scope)
            recent = group.get('recent', 0) * .5 ** (max(0, stamp - group.get('time', stamp)) / 60)
            group.update(recent=recent + 1, time=stamp)
            self._save(db, scope, group)
            self._save(db, 'routing-count', {'count': count + 1})
        try:
            yield channel
        finally:
            self._release(token)

    @contextmanager
    def permit(self, channel, phase, *, info=None, streams=1, on_wait=None, timeout=90):
        if not self.enabled:
            yield channel
            return
        actual = replace(channel, upstream_group=observed_group(channel, info or {})) if phase == 'transfer' else channel
        if phase == 'resolve':
            requirements = [('resolver:' + actual.resolver_group, self.caps['resolver:' + actual.resolver_group], 1),
                            ('quota:' + actual.quota_group, self.caps['quota:' + actual.quota_group], 1)]
        else:
            ceiling = self.caps.get('upstream:' + actual.upstream_group, actual.transfer_limit)
            local = runtime_limits(self.config, self.store.directory).get('groups', {}).get(actual.upstream_group, {})
            limit = min(ceiling, local.get('limit', ceiling))
            requirements = [('upstream:' + actual.upstream_group, ceiling, 1),
                            ('local-transfers:' + self.instance, runtime_limits(self.config, self.store.directory)['transfers'], 1),
                            ('local-upstream:' + self.instance + ':' + actual.upstream_group, limit, 1),
                            ('connections:' + actual.upstream_group, self.caps.get('connections:' + actual.upstream_group, actual.connection_limit), streams),
                            ('local-connections:' + self.instance, int(self.config.get('download_connection_limit', 32)), streams),
                            ('quota:' + actual.quota_group, self.caps['quota:' + actual.quota_group], 1)]
        deadline = time.monotonic() + timeout
        while True:
            with self.transaction() as (db, stamp):
                token, delay = self._admit(db, stamp, requirements)
            if token:
                break
            if on_wait:
                on_wait()
            if delay > 2 or time.monotonic() >= deadline:
                error = PipelineBusy('上游额度繁忙或已限流，任务稍后继续。')
                error.retry_after = max(2, min(60, delay)) + random.uniform(0, 2)
                raise error
            time.sleep(.5 + random.uniform(0, .2))
        try:
            yield actual
        finally:
            self._release(token)

    def outcome(self, channel, *, success, elapsed=0, size=0, error=None, phase='resolve', upstream_group=None):
        if not self.enabled:
            return
        status, delay = throttle_details(error) if error else (None, None)
        with self.transaction() as (db, stamp):
            key = 'channel:' + channel.id
            state = self._state(db, key)
            if success:
                state['failures'] = 0
                if size > 0 and elapsed > 0:
                    rate = size / elapsed
                    state['rate'] = rate if not state.get('rate') else state['rate'] * .7 + rate * .3
            else:
                state['failures'] = state.get('failures', 0) + 1
                if status == 429 or getattr(error, 'retryable', False) and state['failures'] >= 3:
                    state['cooldown_until'] = stamp + (delay or min(300, 15 * 2 ** min(4, state['failures'] - 1)))
            self._save(db, key, state)
            if status == 429 or status == 503 and delay:
                scope = ('resolver:' + channel.resolver_group if phase == 'resolve' else 'upstream:' + (upstream_group or channel.upstream_group))
                for key in (scope, 'quota:' + channel.quota_group):
                    shared = self._state(db, key)
                    shared['cooldown_until'] = max(shared.get('cooldown_until', 0), stamp + (delay or 30))
                    shared['recover_until'] = shared['cooldown_until'] + 60
                    self._save(db, key, shared)
