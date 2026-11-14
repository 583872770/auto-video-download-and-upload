"""Task-level routing and shared permits, fenced by existing task leases.

All admissions use short transactions. Browser, HTTP, disk and backoff waits
take place AFTER the transaction closes. Scope IDs contain no signed URLs.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import random
import time
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from webapp.channels import backend_key, group_caps, observed_group, registry
from webapp.database import download_permits as permits, download_resources as resources, tasks
from webapp.throughput import PipelineBusy, runtime_limits
from webapp.concurrency_policy import (assignment_transfer_limit, backend_limits, live_policies,
                                      resolver_requirements, resource_capacity, transfer_requirements,
                                      youtube_start_interval)
from webapp.download_policy import (failure_evidence, recovery_policy, retry_after,
                                    cooldown_deadline, remember_decision)
from webapp.cookie_policy import task_rest_until


def throttle_details(error):
    """Actual HTTP status only; account limits are a separate failure kind."""
    if error is None:
        return None, None
    failure = failure_evidence(error)
    delay = max(3600, failure.retry_after or 0) if failure.kind == 'account_rate_limit' else failure.retry_after
    return failure.http_status, delay


def _key(scope):
    return hashlib.sha256(scope.encode()).hexdigest()


def error_traits(error):
    """Compatibility view of the shared failure classifier."""
    if error is None:
        return False, False
    failure = failure_evidence(error)
    return failure.retryable, failure.kind == 'content_restriction'


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
    def __init__(self, store, config, task_id=None, *, base=None):
        self.store, self.config, self.task_id = store, config, task_id
        self.channels = registry(config)
        self.caps = group_caps(self.channels)
        self.backend_caps = backend_limits(self.channels)
        self.instance = store.fleet.id if store and store.fleet else 'local'
        self.enabled = bool(store and task_id)
        self.youtube_pacing = None
        if self.enabled and config.get('youtube_pacing_mode') == 'adaptive' and not config.get('youtube_cookie_pool'):
            from webapp.youtube_pacing import YouTubePacing
            self.youtube_pacing = YouTubePacing(self, base)

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
            raise DownloadLeaseLost('渠道准入时任务租约已失效。')
        for scope, cap, units in requirements:
            state = self._state(db, scope)
            if state.get('cooldown_until', 0) > stamp:
                self.blocked_resource = scope.split(':', 1)[0] + ':cooldown'
                return None, state['cooldown_until'] - stamp
            # Take the minimum of live instance policies for this shared group.
            policies = live_policies(state, stamp)
            if cap is None:
                # Unlimited participants publish no numeric ceiling. This also
                # keeps stored policies readable by older finite-only instances.
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
        def assign(db, stamp):
            available = []
            for order, channel in enumerate(candidates):
                if not allow_fallback and channel.adapter != candidates[0].adapter:
                    continue
                scopes = ['channel:' + channel.id, 'resolver:' + channel.resolver_group,
                          'quota:' + channel.quota_group, 'upstream:' + channel.upstream_group]
                if any(self._state(db, key).get('cooldown_until', 0) > stamp for key in scopes):
                    continue
                local = runtime_limits(self.config, self.store.directory)
                limit = assignment_transfer_limit(channel, local)
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
            return channel, token
        channel, token = self.transaction_call(assign)
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
        def assign(db, stamp):
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
                group_limit = assignment_transfer_limit(channel, local)
                preparing = load.get('local-assignment:' + self.instance + ':' + group, 0)
                if preparing >= group_limit + min(4, local['browsers'] * 2):
                    continue
                active = load.get('route:' + channel.id, 0)
                backend_active = load.get('backend-route:' + backend_key(channel), 0)
                backend_limit = self.backend_caps[backend_key(channel)][0]
                if ((channel.task_limit is not None and active >= channel.task_limit)
                        or (backend_limit is not None and backend_active >= backend_limit)):
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
            return channel, token
        channel, token = self.transaction_call(assign)
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
        def prepare(db, stamp):
            if self._owned(db, stamp):
                db.execute(permits.delete().where(permits.c.task_id == self.task_id,
                    permits.c.owner == self.store.owner,
                    permits.c.resource == _key('route-probe:' + self.instance)))
        self.transaction_call(prepare)

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

    def wait_youtube_start(self, channel, on_wait=None):
        """Space video starts across processes without holding DB connections asleep.

        This is a local request budget, not a claim about YouTube's quota. It is
        shared by this instance even when its proxy node changes.
        """
        if self.youtube_pacing and channel.adapter == 'yt_dlp':
            def paced(db, stamp):
                self._check_cookie_cooldown(db, stamp)
                self._check_task_rest(db, stamp)
                return self.youtube_pacing.reserve(db, stamp, channel)
            delay, reason = self.transaction_call(paced)
            if delay:
                if on_wait:
                    on_wait()
                error = PipelineBusy(reason + '，已保留任务等待。')
                error.retry_after = max(2, min(300, delay))
                raise error
            return
        interval = youtube_start_interval(self.config)
        if not self.enabled or channel.adapter != 'yt_dlp' or (interval <= 0 and not self.config.get('_cookie_pool_selected')
                and not self.config.get('youtube_task_rest_interval')):
            return
        def reserve(db, stamp):
            self._check_cookie_cooldown(db, stamp)
            if not self._owned(db, stamp):
                from webapp.downloads import DownloadLeaseLost
                raise DownloadLeaseLost('请求节奏等待期间任务租约已失效。')
            scope = 'youtube-start:' + self.instance
            state = self._state(db, scope)
            self._check_task_rest(db, stamp, state)
            # A settings save must not cancel a previously reserved window,
            # including when shared spacing is changed to zero.
            delay = max(0, state.get('next_at', 0) - stamp)
            if delay:
                return delay
            state.update(next_at=stamp + interval, interval=interval)
            self._save(db, scope, state)
            return 0
        deadline = time.monotonic() + 90
        while True:
            delay = self.transaction_call(reserve)
            if not delay:
                return
            if on_wait:
                on_wait()
            if time.monotonic() >= deadline:
                raise PipelineBusy('正在按 YouTube 请求节奏等待，稍后继续。')
            time.sleep(min(.5, delay))

    def _check_task_rest(self, db, stamp, state=None):
        if self.config.get('_cookie_pool_selected'):
            return  # CookiePool checks the account before assigning a task.
        state = self._state(db, 'youtube-start:' + self.instance) if state is None else state
        delay = task_rest_until(self.config, state) - stamp
        if delay > 0:
            error = PipelineBusy('等待任务结束后休息时间，已保留任务稍后启动。')
            error.retry_after = max(2, min(300, delay))
            raise error

    def finish_youtube_task(self):
        """Record a completed non-pool attempt independently of source health."""
        def finish(db, stamp):
            if not self._owned(db, stamp):
                return
            scope = 'youtube-start:' + self.instance
            state = self._state(db, scope)
            state['last_finished_at'] = max(state.get('last_finished_at', 0), stamp)
            self._save(db, scope, state)
        self.transaction_call(finish)

    @contextmanager
    def permit(self, channel, phase, *, info=None, streams=1, on_wait=None, timeout=90):
        if not self.enabled:
            yield channel
            return
        actual = replace(channel, upstream_group=observed_group(channel, info or {})) if phase == 'transfer' else channel
        if phase == 'resolve':
            requirements = resolver_requirements(actual, self.caps)
        backend = []
        if self.config.get('scheduler_mode') == 'auto':
            _, resolving, quota = self.backend_caps[backend_key(channel)]
            backend.append(('backend-quota:' + backend_key(channel), quota, 1))
            if phase == 'resolve':
                backend.append(('backend-resolver:' + backend_key(channel), resolving, 1))
        deadline = time.monotonic() + timeout
        while True:
            # Waiting owners must see a controller limit change immediately;
            # capturing the old limit until timeout caused needless re-parses.
            current = (transfer_requirements(self.config, self.instance, actual, self.caps,
                       runtime_limits(self.config, self.store.directory), streams)
                       if phase == 'transfer' else requirements) + backend
            def admit(db, stamp):
                self._check_cookie_cooldown(db, stamp)
                health = self._state(db, 'channel:' + channel.id)
                if health.get('cooldown_kind') == 'server_failure' and health.get('cooldown_until', 0) > stamp:
                    self.blocked_resource = 'channel:' + channel.id
                    return None, health['cooldown_until'] - stamp
                token, delay = self._admit(db, stamp, current)
                scope = self.config.get('_cookie_pool_scope')
                if token and phase == 'resolve' and channel.adapter == 'yt_dlp' and scope:
                    state = self._state(db, scope)
                    pending = state.get('pending_starts', {})
                    if pending.get(self.task_id) == self.config.get('_cookie_pool_admitted_at'):
                        pending.pop(self.task_id)
                        self._save(db, scope, state)
                return token, delay
            token, delay = self.transaction_call(admit)
            if token:
                activity = self.config.get('_youtube_task_activity')
                if channel.adapter == 'yt_dlp' and activity is not None:
                    activity['started'] = True
                break
            if on_wait:
                on_wait()
            if delay > 2 or time.monotonic() >= deadline:
                error = PipelineBusy('渠道连续返回服务端错误，短暂等待后重试。'
                                     if (self.blocked_resource or '').startswith('channel:') else
                                     '上游额度繁忙或已限流，任务稍后继续。')
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
        failure = failure_evidence(error, phase=phase,
            source='youtube' if channel.adapter == 'yt_dlp' else 'provider', task_id=self.task_id,
            profile_id=self.config.get('_cookie_pool_selected'),
            profile_name=self.config.get('_cookie_pool_profile_name')) if error else None
        policy = recovery_policy(failure, self.config, youtube=channel.adapter == 'yt_dlp') if failure else None
        if (failure and failure.phase == 'subtitles' and channel.adapter == 'yt_dlp'
                and failure.http_status in (429, 503)):
            from webapp.subtitle_lane import failed as subtitle_failed
            subtitle_failed(self, error)
            return
        if failure and failure.kind == 'transient_failure' and 'channel' not in policy.targets:
            # Connectivity failures remain in task/Cookie diagnostics. They
            # must not degrade channel health or trigger a second backoff.
            return
        if (failure and policy.targets == ('session',) and channel.adapter == 'yt_dlp'):
            # CookiePool owns this account's cooldown. Neither channel health,
            # shared quotas nor instance pacing should block unrelated accounts.
            return
        if not success and local_failure(error):
            return
        status, delay = (failure.http_status, policy.delay) if failure else (None, None)
        limited = bool(failure and failure.kind in ('account_rate_limit', 'request_rate_limit'))
        transient, video_error = error_traits(error)
        if not success and (video_error or failure.kind == 'content_restriction') and not limited:
            return  # Content-specific failures must not poison shared health.
        def record(db, stamp):
            nonlocal transient, delay
            key = 'channel:' + channel.id
            state = self._state(db, key)
            if failure and state.get('last_decision', {}).get('incident_id') == failure.incident_id:
                return
            if failure and failure.kind == 'transient_failure':
                # Three different tasks reporting 5xx within a minute indicate
                # a channel outage. A single bad video never slows an account.
                samples = [item for item in state.get('server_failures', [])
                           if item['time'] > stamp - 60 and item['task'] != self.task_id]
                samples.append({'task': self.task_id, 'time': stamp})
                state['server_failures'] = samples[-16:]
                if len(samples) >= 3:
                    state.update(cooldown_until=max(state.get('cooldown_until', 0),
                                 cooldown_deadline(error, stamp, max(30, failure.retry_after or 0))),
                                 cooldown_status=status, cooldown_kind='server_failure')
                remember_decision(state, failure, layer='channel', scope='渠道服务端故障',
                    action='cooldown' if state.get('cooldown_until', 0) > stamp else 'record',
                    until=state.get('cooldown_until', 0), stamp=stamp)
                self._save(db, key, state)
                return
            if self.youtube_pacing and channel.adapter == 'yt_dlp':
                delay = self.youtube_pacing.outcome(db, stamp, channel, success=success, size=size,
                    failure=failure, delay=delay, error=error)
            until = cooldown_deadline(error, stamp, delay) if delay else 0
            throttle = {'last_throttle_status': status, 'last_throttle_at': stamp,
                        'last_throttle_kind': ('account' if failure.kind == 'account_rate_limit' else phase)
                       } if limited or status == 503 else {}
            state.update(throttle)
            if success:
                state.pop('server_failures', None)
                state['failures'] = 0
                if state.get('cooldown_until', 0) <= stamp:
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
                transient = failure.kind == 'transient_failure'
                if (limited or (transient or self.config.get('scheduler_mode') == 'auto')
                        and (self.config.get('scheduler_mode') == 'auto' or state['failures'] >= 3)):
                    state['cooldown_until'] = max(state.get('cooldown_until', 0), until or
                        stamp + min(300, 15 * 2 ** min(4, state['failures'] - 1)))
                    state['cooldown_status'] = status
                    state['cooldown_kind'] = throttle.get('last_throttle_kind')
                remember_decision(state, failure, layer='channel', scope='渠道健康',
                    action='cooldown' if state.get('cooldown_until', 0) > stamp else 'record',
                    until=state.get('cooldown_until', 0), stamp=stamp)
            self._save(db, key, state)
            if policy and 'shared' in policy.targets:
                scope = ('resolver:' + channel.resolver_group if phase == 'resolve' else 'upstream:' + (upstream_group or channel.upstream_group))
                scopes = [scope, 'quota:' + channel.quota_group]
                if self.config.get('scheduler_mode') == 'auto':
                    scopes.append('backend-quota:' + backend_key(channel))
                    if phase == 'resolve':
                        scopes.append('backend-resolver:' + backend_key(channel))
                for key in scopes:
                    shared = self._state(db, key)
                    shared.update(throttle)
                    shared['cooldown_status'] = status
                    shared['cooldown_kind'] = throttle.get('last_throttle_kind')
                    shared['cooldown_until'] = max(shared.get('cooldown_until', 0), until)
                    shared['recover_until'] = shared['cooldown_until'] + 60
                    remember_decision(shared, failure, layer='shared', scope='共享解析与渠道配额' if phase == 'resolve' else
                        '共享字幕与渠道配额' if phase == 'subtitles' else '共享传输与渠道配额',
                        action='cooldown', until=shared['cooldown_until'], stamp=stamp)
                    self._save(db, key, shared)
        self.transaction_call(record)
        if limited and error is not None:
            error.retry_after = max(getattr(error, 'retry_after', 0), delay or 0)
