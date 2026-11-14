"""TaskLeases contracts used by the composed TaskStore repository."""
import hashlib
import json
import os
import random
import time

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import OperationalError

from webapp.database import reservations, rotations, settings, tasks, workers


class TaskLeases:
    def _locked(self, statement, *, skip=False):
        # MySQL 5.7 waits for the row lock, then rechecks the queued predicate.
        # Claim transactions commit before downloads start, so only dispatch
        # waits; workers still download concurrently without duplicate claims.
        return statement if self.db.sqlite else statement.with_for_update(
            skip_locked=skip and self.db.supports_skip_locked)

    def claim(self, worker_id=None, actions=None, exclude=None, lease_seconds=60):
        # A blocking 5.7 locking scan can deadlock with a transaction changing
        # the queue's status index. InnoDB rolls back the losing transaction;
        # retry the entire claim (including counters), never just its SELECT.
        for attempt in range(5):
            try:
                return self._claim(worker_id, actions, exclude, lease_seconds)
            except OperationalError as error:
                code = error.orig.args[0] if error.orig.args else None
                if not self.db.mysql or code != 1213 or attempt == 4:
                    raise
                time.sleep(0.01 * 2 ** attempt)

    def _claim(self, worker_id, actions, exclude, lease_seconds):
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            stamp = self.fleet.clock(db) if self.fleet else time.time()
            if self.fleet or self.single:
                # Admission allocates slots under this exclusive lock. Taking a
                # shared lock first makes concurrent MySQL claims deadlock when
                # they both upgrade to the exclusive admission lock below.
                db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget').with_for_update()).first()
            mode = db.execute(select(settings.c.value).where(settings.c.key == 'fleet_enabled')).scalar_one_or_none()
            if self.fleet and mode != 'true':
                return None
            if self.fleet:
                from webapp.modes import single_state
                if (single_state(self.directory) or {}).get('status') == 'requested':
                    return None  # Freeze local claims until the requested restart.
            if not self.fleet and mode is not None:
                from webapp.modes import mode_key
                if mode != mode_key(self.single):
                    return None
            statement = select(tasks).where(tasks.c.status == 'queued', tasks.c.available_at <= stamp)
            if self.fleet:
                statement = statement.where(or_(tasks.c.target_instance_id == self.fleet.id,
                    and_(tasks.c.target_instance_id.is_(None), tasks.c.action.in_(self.fleet.untargeted_actions()))))
                if not self.fleet.admission(db):
                    statement = statement.where(tasks.c.action != 'download')
            # Destructive cleanup runs only in the Web instance that accepted confirmation.
            statement = statement.where(tasks.c.action != 'clear_media')
            statement = statement.where(self.released_condition())
            if self.claims_paused(db, lock=True):
                statement = statement.where(tasks.c.action != 'download')
            elif not self._released_queued_count(db):
                statement = statement.where(tasks.c.action != 'download')
            from webapp.disk_budget import resource_blocked
            if resource_blocked(self, db):
                statement = statement.where(tasks.c.action != 'download')
            if self.media_clear_busy(db):
                statement = statement.where(~tasks.c.action.in_(('download', 'import', 'index')))
            if actions:
                statement = statement.where(tasks.c.action.in_(actions))
            if exclude:
                statement = statement.where(~tasks.c.action.in_(exclude))
            download_only = actions and set(actions) == {'download'}
            regular_worker = actions is None and set(exclude or ()) == {'import', 'index'}
            workbench_download = self.workbench_job(db) if self.fleet and (download_only or regular_worker) else None
            row = None
            if workbench_download and regular_worker:
                check = statement.where(tasks.c.action == 'check').order_by(tasks.c.available_at, tasks.c.seq).limit(1)
                row = db.execute(self._locked(check, skip=True)).mappings().first()
            if workbench_download:
                statement = statement.where(tasks.c.action == 'download', tasks.c.batch_id == workbench_download)
                statement = statement.with_hint(tasks, 'FORCE INDEX (ix_tasks_batch_ready)', dialect_name='mysql')
            ordering = (tasks.c.seq,) if workbench_download else (tasks.c.available_at, tasks.c.seq)
            if row is None:
                row = db.execute(self._locked(statement.order_by(*ordering).limit(1), skip=True)).mappings().first()
            if row is None:
                return None
            self._transition(db, row, 'running')
            values = dict(status='running', worker_id=worker_id or self.owner,
                          started_at=self._timestamp(), finished_at=None, lease_until=time.time() + lease_seconds,
                          attempts=row['attempts'] + 1, error=None)
            if self.fleet:
                values.update(instance_id=self.fleet.id, lease_until=self.fleet.clock(db) + lease_seconds)
            if row['action'] == 'download':
                payload = self.decode(row, private=True, db=db)['payload']
                previous_config = (payload.get('config', {})
                                   if payload.get('_execution_settings') or row['attempts'] else None)
                previous_method = previous_config.get('download_method', 'yt_dlp') if previous_config is not None else None
                payload.pop('_execution_settings', None)
                payload.pop('download_session_id', None)
                payload = self.effective_download_payload(row['id'], payload, db)
                payload['_execution_settings'] = True
                transfer_keys = ('download_method', 'download_methods', 'download_format', 'yt5s_quality',
                                 'downmp3_quality', 'yt1d_quality', 'bergung_quality')
                payload['_restart_transfer'] = bool(payload.get('_restart_transfer') or (
                    previous_config is not None and any(
                        previous_config.get(key) != payload.get('config', {}).get(key) for key in transfer_keys)))
                if self.fleet and row.get('instance_id') and row['instance_id'] != self.fleet.id:
                    payload['_restart_transfer'] = True
                values['max_attempts'] = int(payload.get('config', {}).get('download_attempts', row['max_attempts']))
                values['policy_version'] = payload.get('policy_version')
                from webapp.downloads import initial_progress
                initial = initial_progress(payload.get('config', {}), payload.get('policy_version'))
                if payload.get('_retry_cookie_profile_id'):
                    initial['retry_cookie_profile_id'] = payload['_retry_cookie_profile_id']
                values['progress'] = json.dumps(initial, ensure_ascii=False)
                # Generate snapshot BEFORE adding download_session_id
                packed_without_session = self._pack(db, payload)
                row = {**row, 'instance_id': values.get('instance_id', row.get('instance_id')),
                       'payload': packed_without_session, '_previous_download_method': previous_method}
                identity = self.statistics.begin(db, row)
                # Add download_session_id and create final snapshot
                payload_with_session = {**payload, 'download_session_id': identity}
                values['payload'] = self._pack(db, payload_with_session)
                row.pop('_previous_download_method', None)
            db.execute(tasks.update().where(tasks.c.id == row['id']).values(**values))
            return self.decode({**row, **values}, private=True, db=db)

    def heartbeat(self, task_id, worker_id, lease_seconds=60):
        with self.connect() as db:
            if self.fleet and self.fleet.row(db)['boot_id'] != self.fleet.boot_id:
                return False
            stamp = self.fleet.clock(db) if self.fleet else time.time()
            statement = tasks.update().where(tasks.c.id == task_id, tasks.c.status == 'running',
                                             tasks.c.worker_id == worker_id)
            if self.fleet:
                statement = statement.where(tasks.c.lease_until > stamp)
            return db.execute(statement.values(lease_until=stamp + lease_seconds)).rowcount > 0

    def batch_heartbeat(self, updates):
        """批量更新租约（减少数据库往返）。

        Args:
            updates: [(task_id, worker_id, lease_seconds), ...]

        Returns:
            成功更新的任务数
        """
        if not updates:
            return 0

        with self.connect() as db:
            if self.fleet and self.fleet.row(db)['boot_id'] != self.fleet.boot_id:
                return 0

            stamp = self.fleet.clock(db) if self.fleet else time.time()

            from sqlalchemy import case
            leases = {(task_id, worker_id): stamp + lease_seconds
                      for task_id, worker_id, lease_seconds in updates}
            whens = [(and_(tasks.c.id == task_id, tasks.c.worker_id == worker_id), expiry)
                     for (task_id, worker_id), expiry in leases.items()]
            statement = tasks.update().where(
                or_(*(condition for condition, _ in whens)),
                tasks.c.status == 'running'
            ).values(
                lease_until=case(*whens, else_=tasks.c.lease_until)
            )

            if self.fleet:
                statement = statement.where(tasks.c.lease_until > stamp)

            return db.execute(statement).rowcount

    def release(self, task_id, worker_id, reason, delay=30, *, resource=False):
        with self.connect() as db:
            row = db.execute(self._locked(select(tasks).where(tasks.c.id == task_id,
                             tasks.c.worker_id == worker_id, tasks.c.status == 'running'))).mappings().first()
            if row:
                if self.fleet and (self.fleet.row(db)['boot_id'] != self.fleet.boot_id
                                  or not row['lease_until'] or row['lease_until'] <= self.fleet.clock(db)):
                    return False
                self._transition(db, row, 'queued')
                db.execute(tasks.update().where(tasks.c.id == task_id).values(status='queued', worker_id=None,
                    lease_until=None, attempts=max(0, row['attempts'] - 1),
                    available_at=(self.fleet.clock(db) if self.fleet else time.time()) + delay, error=reason))
                if resource:
                    progress = json.loads(row['progress']) if row.get('progress') else {}
                    progress.update(stage='disk_wait', speed=None, eta=None)
                    db.execute(tasks.update().where(tasks.c.id == task_id).values(progress=json.dumps(progress)))
                self.statistics.transition(db, row, 'paused', reason)
                self.statistics.settle(db, instance_id=row.get('instance_id'))

    def update_progress(self, task_id, progress, worker_id=None):
        if worker_id:
            progress = {**progress, '_reported_at': time.time()}
        criteria = [tasks.c.id == task_id]
        if worker_id:
            criteria.extend((tasks.c.worker_id == worker_id, tasks.c.status == 'running'))
        values = {'progress': json.dumps(progress, ensure_ascii=False)}
        if progress.get('title'):
            values['title'] = str(progress['title']).casefold()
        with self.connect() as db:
            if self.fleet and worker_id:
                if self.fleet.row(db)['boot_id'] != self.fleet.boot_id:
                    return False
                criteria.append(tasks.c.lease_until > self.fleet.clock(db))
            checkpoint = None
            if worker_id and self.progress_cache.enabled:
                # MVCC ownership reads do not wait for task row write locks.
                # Never let Redis authorize execution or bypass stop/lease checks.
                row = db.execute(select(tasks.c.id, tasks.c.status, tasks.c.worker_id,
                    tasks.c.started_at, tasks.c.attempts, tasks.c.lease_until).where(
                        *criteria)).mappings().first()
                if not row:
                    return False
                if self.progress_cache.publish(row, progress):
                    checkpoint = (worker_id, row['started_at'], row['attempts'],
                                  progress.get('stage'), progress.get('download_method'))
                    previous = self._progress_checkpoints.get(task_id)
                    if previous and previous[0] == checkpoint and time.monotonic() - previous[1] < 5:
                        return True
            updated = db.execute(tasks.update().where(*criteria).values(**values)).rowcount > 0
            if updated and checkpoint:
                if len(self._progress_checkpoints) >= 256:
                    self._progress_checkpoints.clear()
                self._progress_checkpoints[task_id] = (checkpoint, time.monotonic())
            return updated

    def finish(self, task_id, *, result=None, error=None, worker_id=None, retryable=False, retry_after=None):
        from webapp.db_retry import transaction_retry
        return transaction_retry(lambda: self._finish(task_id, result=result, error=error, worker_id=worker_id,
                                                     retryable=retryable, retry_after=retry_after))

    def _finish(self, task_id, *, result=None, error=None, worker_id=None, retryable=False, retry_after=None):
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            row = db.execute(self._locked(select(tasks).where(tasks.c.id == task_id))).mappings().first()
            if not row or row['status'] not in ('queued', 'running'):
                return False
            if worker_id and (row['worker_id'] != worker_id or row['status'] != 'running'):
                return False
            settled_stamp = self.fleet.clock(db) if self.fleet else time.time()
            if self.fleet and worker_id and (not row['lease_until'] or row['lease_until'] <= settled_stamp
                    or self.fleet.row(db)['boot_id'] != self.fleet.boot_id):
                return False
            if result:
                result = dict(result)
            retry = bool(error and retryable and row['attempts'] < row['max_attempts'])
            delay = min(300, 5 * 2 ** max(0, row['attempts'] - 1))
            delay = max(delay, min(86400, retry_after or 0)) + random.uniform(0, delay * .2)
            status = 'queued' if retry else 'failed' if error else 'succeeded'
            live = self.progress_cache.read(row)
            progress = live['progress'] if live else json.loads(row['progress']) if row['progress'] else {}
            if progress:
                progress.update(stage='retrying' if retry else 'failed' if error else
                                'skipped' if (result or {}).get('skipped') else 'completed', speed=None, eta=None)
                if not error:
                    progress['percent'] = 100
            extra = {}
            failure = progress.get('last_failure') or {}
            retry_profile = failure.get('profile_id') or progress.get('cookie_profile_id')
            pin_retry = bool(error) and retry_profile and (failure.get('reason') == 'no_data'
                or failure.get('kind') in ('account_rate_limit', 'request_rate_limit',
                                          'authentication_required', 'verification_required'))
            if pin_retry or not retry and progress.get('retry_cookie_profile_id'):
                payload = self.decode(row, private=True, db=db)['payload']
                if pin_retry:
                    payload.setdefault('_retry_original_target', row.get('target_instance_id'))
                    payload.update(_retry_cookie_profile_id=retry_profile,
                                   _retry_cookie_instance_id=row.get('instance_id') or 'local')
                    if row.get('instance_id'):
                        extra['target_instance_id'] = row['instance_id']
                else:
                    extra['target_instance_id'] = payload.pop('_retry_original_target', row.get('target_instance_id'))
                    payload.pop('_retry_cookie_profile_id', None)
                    payload.pop('_retry_cookie_instance_id', None)
                extra['payload'] = self._pack(db, payload)
            self._transition(db, row, status)
            db.execute(tasks.update().where(tasks.c.id == task_id).values(
                status=status, finished_at=None if retry else self._timestamp(), result=json.dumps(result, ensure_ascii=False) if result else None,
                error=error, progress=json.dumps(progress, ensure_ascii=False) if progress else None,
                available_at=settled_stamp + delay if retry else 0,
                lease_until=None, worker_id=None, **extra))
            outcome = ('retrying' if retry else 'failed' if error else 'test_succeeded' if (result or {}).get('test_mode')
                       else 'skipped' if (result or {}).get('skipped') else 'succeeded')
            self.statistics.transition(db, row, outcome, error,
                fallback_used=bool(progress.get('fallback_used') or (result or {}).get('fallback_used')))
            if not retry and row['action'] == 'download':
                self.lists.outcome(db, row, outcome)
                if self.fleet:
                    self._counter(db, 'instance:' + self.fleet.id, outcome, 1)
            self.statistics.settle(db, instance_id=row.get('instance_id'))
            if not retry:
                generation = progress.get('rotation_id')
                if generation and not (result or {}).get('skipped'):
                    field = 'failed' if error else 'succeeded'
                    self.db.ensure(db, rotations, {'id': generation, 'succeeded': 0, 'failed': 0})
                    db.execute(rotations.update().where(rotations.c.id == generation).values({field: rotations.c[field] + 1}))
                if row['source_key'] and not row['source_key'].startswith('job:') and (error or (result or {}).get('test_mode') or row['source_key'].startswith('test:')):
                    db.execute(reservations.delete().where(reservations.c.key == row['source_key'], reservations.c.task_id == task_id))
            if not retry:
                # Keep the hourly bucket's write lock as close to COMMIT as
                # possible; unrelated statistics/ledger work precedes it.
                from webapp.runtime_summary import record_terminal
                record_terminal(self, db, row, outcome, progress, result, settled_stamp)
            return True

    def recover(self, *, expired_only=True):
        """Recover running tasks that are no longer being executed.

        Args:
            expired_only: If True (default), only recover tasks whose lease has expired.
                         If False, recover ALL running tasks regardless of lease status.
                         WARNING: expired_only=False will interrupt tasks from other instances
                         in multi-instance deployments. Only use during single-instance recovery.
        """
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            statement = select(tasks).where(tasks.c.status == 'running')
            if expired_only:
                statement = statement.where(or_(tasks.c.lease_until < (self.fleet.clock(db) if self.fleet else time.time()), tasks.c.lease_until.is_(None)))
            elif self.fleet:
                # In fleet mode with expired_only=False, only recover tasks owned by this instance
                # to avoid interrupting other instances' work during emergency recovery
                statement = statement.where(tasks.c.instance_id == self.fleet.id)
            rows = db.execute(self._locked(statement.limit(1000), skip=True)).mappings().all()
            scopes = set()
            for row in rows:
                scopes.add(row.get('instance_id'))
                retry = expired_only and row['action'] in ('download', 'import', 'index') and row['attempts'] < row['max_attempts']
                status = 'queued' if retry else 'interrupted'
                self._transition(db, row, status)
                db.execute(tasks.update().where(tasks.c.id == row['id']).values(status=status, lease_until=None, worker_id=None,
                    available_at=(self.fleet.clock(db) if self.fleet else time.time()) + 2 if retry else 0, finished_at=None if retry else self._timestamp(),
                    error='执行进程已退出，等待恢复。' if retry else '执行进程已退出。'))
                self.statistics.transition(db, row, 'retrying' if retry else 'failed', '执行进程已退出。')
                if not retry and not (row['source_key'] or '').startswith('job:'):
                    db.execute(reservations.delete().where(reservations.c.task_id == row['id']))
            for scope in scopes:
                self.statistics.settle(db, instance_id=scope)
            return len(rows)

    def worker_status(self, worker_id, slot, state, task_id=None, message=''):
        with self.connect() as db:
            if self.fleet:
                slot = hashlib.sha256(self.fleet.id.encode()).hexdigest()[:16] + ':' + str(slot)
            values = dict(id=worker_id, slot=slot, pid=os.getpid(), heartbeat=time.time(), state=state, task_id=task_id, message=message)
            db.execute(self.db.upsert(workers, values, ['id']))

    def workers(self):
        with self.connect() as db:
            latest = {}
            statement = select(workers).where(workers.c.heartbeat > time.time() - 90)
            if self.fleet and self.fleet.can_download:
                prefix = hashlib.sha256(self.fleet.id.encode()).hexdigest()[:16] + ':'
                statement = statement.where(workers.c.slot.startswith(prefix, autoescape=True))
            for row in db.execute(statement.order_by(workers.c.heartbeat.desc()).limit(500)).mappings():
                latest.setdefault(row['slot'], dict(row))
            return list(latest.values())

