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
from sqlalchemy.exc import SQLAlchemyError

from webapp.channels import backend_key, group_caps, observed_group, registry
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


def error_traits(error):
    """Preserve typed errors through downloader/IPC exception wrappers."""
    seen, transient, video = set(), False, False
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        video |= getattr(error, 'failure_scope', None) == 'video'
        transient |= bool(getattr(error, 'retryable', False)) or type(error).__name__ == 'WebsiteTimeoutError'
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return transient, video


def local_failure(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if (type(error).__name__ in ('DownloadLeaseLost', 'DiskPressure', 'DiskCoordinationBusy',
                'BrowserEnvironmentRequired', 'DownloadEnvironmentRequired', 'YouTubeAuthRequired', 'PipelineBusy')
                or isinstance(error, SQLAlchemyError)
                or isinstance(error, OSError) and error.errno in (5, 13, 28, 30, 122)):
            return True
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return False


class ChannelScheduler:
    def __init__(self, store, config, task_id=None):
        self.store, self.config, self.task_id = store, config, task_id
        self.channels = registry(config)
        self.caps = group_caps(self.channels)
        self.backend_caps = {}
        for channel in self.channels:
            key = backend_key(channel)
            limits = (channel.task_limit, channel.resolver_limit, channel.transfer_limit)
            previous = self.backend_caps.get(key, limits)
            self.backend_caps[key] = tuple(min(a, b) for a, b in zip(previous, limits))
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
        if self.config.get('scheduler_mode') == 'auto':
            with self.auto_assignment(candidates) as channel:
                yield channel
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

    def routing_snapshot(self, db, scopes, stamp):
        """Two bounded reads, independent of the number of candidate routes."""
        keys = {_key(scope): scope for scope in scopes}
        states = {keys[key]: json.loads(value) for key, value in db.execute(
            select(resources.c.key, resources.c.state).where(resources.c.key.in_(keys)))}
        load = {keys[key]: int(units) for key, units in db.execute(
            select(permits.c.resource, func.sum(permits.c.units)).select_from(permits.join(tasks,
                (permits.c.task_id == tasks.c.id) & (permits.c.owner == tasks.c.worker_id)))
            .where(permits.c.resource.in_(keys), tasks.c.status == 'running', tasks.c.lease_until > stamp)
            .group_by(permits.c.resource))}
        return states, load

    @contextmanager
    def auto_assignment(self, candidates):
        """Route across providers, with one unproven entrance per instance.

        Entrances sharing a group compete for that group's single allocation.
        A hostname or an additional alias never creates extra bandwidth share.
        Cold routes are explored separately so a failing site cannot occupy all
        preparation workers while a proven route could be delivering files.
        """
        local = runtime_limits(self.config, self.store.directory)
        probe_scope = 'route-probe:' + self.instance
        with self.transaction() as (db, stamp):
            scopes = {probe_scope, 'routing-auto'}
            for channel in candidates:
                scopes.update(('channel:' + channel.id, 'route:' + channel.id,
                    'backend-route:' + backend_key(channel), 'backend-resolver:' + backend_key(channel),
                    'backend-quota:' + backend_key(channel),
                    'resolver:' + channel.resolver_group, 'quota:' + channel.quota_group,
                    'upstream:' + channel.upstream_group, 'assignment:' + channel.upstream_group,
                    'local-assignment:' + self.instance + ':' + channel.upstream_group))
            states, load = self.routing_snapshot(db, scopes, stamp)
            count = states.get('routing-auto', {}).get('count', 0)
            warm, probes = [], []
            for order, channel in enumerate(candidates):
                if any(states.get(scope, {}).get('cooldown_until', 0) > stamp for scope in
                       ('channel:' + channel.id, 'resolver:' + channel.resolver_group,
                        'backend-resolver:' + backend_key(channel), 'backend-quota:' + backend_key(channel),
                        'quota:' + channel.quota_group, 'upstream:' + channel.upstream_group)):
                    continue
                state = states.get('channel:' + channel.id, {})
                group = channel.upstream_group
                group_limit = min(channel.transfer_limit, local.get('groups', {}).get(group, {}).get('limit', local['transfers']))
                if group == 'unverified-media':
                    group_limit = min(channel.transfer_limit, max(group_limit,
                        local.get('groups', {}).get('youtube-origin', {}).get('limit', group_limit)))
                preparing = load.get('local-assignment:' + self.instance + ':' + group, 0)
                if preparing >= group_limit + min(4, local['browsers'] * 2):
                    continue
                active = load.get('route:' + channel.id, 0)
                backend_active = load.get('backend-route:' + backend_key(channel), 0)
                if active >= channel.task_limit or backend_active >= self.backend_caps[backend_key(channel)][0]:
                    continue
                known = state.get('rate', 0) > 0 or state.get('successes', 0) > 0
                if not known:
                    if not load.get(probe_scope, 0) and not active and not backend_active:
                        probes.append((state.get('last_started', 0), order, channel))
                    continue
                probability = max(.1, state.get('success_ewma', 1))
                # Compare the same reference size; random video lengths must
                # not make an entrance look faster just because it saw a clip.
                rate = max(1, state.get('transfer_rate') or state.get('rate', 0))
                seconds = min(300, max(2, state.get('resolve_seconds', 0) + 64 * 1024 ** 2 / rate)) / probability
                group_load = (load.get('assignment:' + group, 0) + 1) / max(1, group_limit)
                # Group score excludes alias count; entrance load only selects
                # a route inside the winning group's bounded capacity.
                warm.append((seconds * group_load, seconds * (max(active, backend_active) + 1) / channel.weight, order, channel))
            probing = bool(probes and (not warm or count % 10 == 0))
            if probing:
                channel = min(probes, key=lambda item: item[:2])[2]
            elif warm:
                group_scores = {}
                for group_cost, route_cost, order, candidate in warm:
                    group_scores[candidate.upstream_group] = min(group_scores.get(candidate.upstream_group, float('inf')), group_cost)
                group = min(group_scores, key=group_scores.get)
                channel = min((item for item in warm if item[3].upstream_group == group), key=lambda item: item[1:3])[3]
            else:
                raise PipelineBusy('候选渠道繁忙或正在恢复，等待下一次自动调度。')
            requirements = [('assignment:' + channel.upstream_group, 4096, 1),
                ('local-assignment:' + self.instance + ':' + channel.upstream_group, 256, 1),
                ('route:' + channel.id, channel.task_limit, 1),
                ('backend-route:' + backend_key(channel), self.backend_caps[backend_key(channel)][0], 1)]
            if probing:
                requirements.append((probe_scope, 1, 1))
            token, _ = self._admit(db, stamp, requirements)
            if not token:
                raise PipelineBusy('渠道准备额度繁忙，稍后继续。')
            state = self._state(db, 'channel:' + channel.id)
            state['last_started'] = stamp
            self._save(db, 'channel:' + channel.id, state)
            self._state(db, 'routing-auto')
            self._save(db, 'routing-auto', {'count': count + 1})
        try:
            yield channel
        finally:
            self._release(token)

    def prepared(self):
        """Free cold-resolution capacity while a large first file transfers.

        Its route/backend permits remain owned until the file finishes; another
        unmeasured task cannot duplicate this entrance's initial trial.
        """
        if not self.enabled or self.config.get('scheduler_mode') != 'auto':
            return
        with self.transaction() as (db, stamp):
            if self._owned(db, stamp):
                db.execute(permits.delete().where(permits.c.task_id == self.task_id,
                    permits.c.owner == self.store.owner,
                    permits.c.resource == _key('route-probe:' + self.instance)))

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
        if self.config.get('scheduler_mode') == 'auto':
            _, resolving, quota = self.backend_caps[backend_key(channel)]
            requirements.append(('backend-quota:' + backend_key(channel), quota, 1))
            if phase == 'resolve':
                requirements.append(('backend-resolver:' + backend_key(channel), resolving, 1))
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

    def outcome(self, channel, *, success, elapsed=0, size=0, error=None, phase='resolve', upstream_group=None,
                resolve_elapsed=0, transfer_elapsed=0):
        if not self.enabled:
            return
        if not success and local_failure(error):
            return
        status, delay = throttle_details(error) if error else (None, None)
        transient, video_error = error_traits(error)
        if not success and video_error and status != 429:
            return  # Content-specific failures must not poison shared health.
        with self.transaction() as (db, stamp):
            key = 'channel:' + channel.id
            state = self._state(db, key)
            if success:
                state['failures'] = 0
                state.pop('cooldown_until', None)
                state['successes'] = state.get('successes', 0) + 1
                state['success_ewma'] = state.get('success_ewma', 1) * .8 + .2
                for name, value in (('completion_seconds', elapsed), ('resolve_seconds', resolve_elapsed),
                                    ('transfer_rate', size / transfer_elapsed if size and transfer_elapsed else 0)):
                    if value > 0:
                        state[name] = state.get(name, value) * .7 + value * .3
                if size > 0 and elapsed > 0:
                    rate = size / elapsed
                    state['rate'] = rate if not state.get('rate') else state['rate'] * .7 + rate * .3
            else:
                state['failures'] = state.get('failures', 0) + 1
                state['success_ewma'] = state.get('success_ewma', 1) * .8
                from webapp.proxy_health import connection_failure
                transient = bool(transient or connection_failure(error)
                                 or status in (408, 500, 502, 503, 504))
                if (status == 429 or (transient or self.config.get('scheduler_mode') == 'auto')
                        and (self.config.get('scheduler_mode') == 'auto' or state['failures'] >= 3)):
                    state['cooldown_until'] = stamp + (delay or min(300, 15 * 2 ** min(4, state['failures'] - 1))) + random.uniform(0, 3)
            self._save(db, key, state)
            if status == 429 or status == 503 and delay:
                scope = ('resolver:' + channel.resolver_group if phase == 'resolve' else 'upstream:' + (upstream_group or channel.upstream_group))
                scopes = [scope, 'quota:' + channel.quota_group]
                if self.config.get('scheduler_mode') == 'auto':
                    scopes.append('backend-quota:' + backend_key(channel))
                    if phase == 'resolve':
                        scopes.append('backend-resolver:' + backend_key(channel))
                for key in scopes:
                    shared = self._state(db, key)
                    shared['cooldown_until'] = max(shared.get('cooldown_until', 0), stamp + (delay or 30))
                    shared['recover_until'] = shared['cooldown_until'] + 60
                    self._save(db, key, shared)
