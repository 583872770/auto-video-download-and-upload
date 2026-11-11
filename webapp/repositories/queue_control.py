"""QueueControl contracts used by the composed TaskStore repository."""
import hashlib
import json
import time

from sqlalchemy import and_, func, or_, select

from webapp.database import counters, download_lists, imports, rotations, settings, tasks, workers, job_profiles, job_imports


class QueueControl:
    def downloads_paused(self, db=None, *, lock=False):
        """Read shared execution permission in the caller's transaction.

        Web processes and workers can all change this flag; a process-local
        cache cannot be invalidated reliably across those writers.
        """
        if db is None:
            with self.connect() as connection:
                return self.downloads_paused(connection, lock=lock)
        else:
            statement = select(settings.c.value).where(settings.c.key == 'downloads_paused')
            if lock and not self.db.sqlite:
                statement = statement.with_for_update(read=True)
            return db.execute(statement).scalar_one() == 'true'

    def workbench_job(self, db):
        if not self.fleet:
            return None
        return db.execute(select(settings.c.value).where(
            settings.c.key == 'workbench_job:' + self.fleet.id)).scalar_one_or_none()

    def downloads_stopped(self, db=None, *, lock=False):
        """A scoped instance drains on pause and terminates only on stop."""
        if db is None:
            with self.connect() as connection:
                return self.downloads_stopped(connection, lock=lock)
        if self.fleet and self.workbench_job(db):
            return bool(self.fleet.row(db)['stop_requested'])
        return self.downloads_paused(db, lock=lock)

    def claims_paused(self, db=None, *, lock=False):
        if db is None:
            with self.connect() as connection:
                return self.claims_paused(connection, lock=lock)
        if self.fleet and self.workbench_job(db):
            row = self.fleet.row(db)
            return bool(row['paused'] or row['stop_requested'])
        return self.downloads_paused(db, lock=lock)

    def released_condition(self):
        selected = select(settings.c.value).where(settings.c.key == 'selected_download_job').scalar_subquery()
        if self.fleet:
            own = select(settings.c.value).where(settings.c.key == 'workbench_job:' + self.fleet.id).scalar_subquery()
            valid = select(job_profiles.c.id).where(job_profiles.c.id == tasks.c.batch_id,
                job_profiles.c.archived == 0, job_profiles.c.deleted == 0).exists()
            legacy = and_(or_(selected.is_(None), selected == '', tasks.c.batch_id == selected),
                ~select(download_lists.c.id).where(download_lists.c.id == tasks.c.batch_id,
                    download_lists.c.released == 0).exists())
            return or_(tasks.c.action != 'download',
                and_(own.is_not(None), tasks.c.batch_id == own, valid), and_(own.is_(None), legacy))
        return or_(tasks.c.action != 'download', and_(
            or_(selected.is_(None), selected == '', tasks.c.batch_id == selected),
            ~select(download_lists.c.id).where(
                download_lists.c.id == tasks.c.batch_id, download_lists.c.released == 0).exists()))

    def selected_download_job(self, db=None):
        if db is None:
            with self.connect() as connection:
                return self.selected_download_job(connection)
        return db.execute(select(settings.c.value).where(
            settings.c.key == 'selected_download_job')).scalar_one_or_none() or None

    def set_downloads_paused(self, paused):
        with self.connect() as db:
            if self.fleet:
                if self.db.sqlite:
                    db.exec_driver_sql('BEGIN IMMEDIATE')
                db.execute(select(settings.c.key).where(settings.c.key == 'fleet_budget').with_for_update()).first()
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='true' if paused else 'false'))
            if not paused and not self.fleet:
                from webapp.download_policy import execution_decision
                execution_decision(self, db, 'local')
            if paused:
                if self.fleet:
                    from webapp.database import instances
                    db.execute(instances.update().where(instances.c.role.in_(('worker', 'hybrid')))
                               .values(paused=1, stop_requested=1))
                self.statistics.stop_all(db)

    def activate_download_lists(self, expected_policy_version=None):
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            db.execute(select(settings.c.key).where(settings.c.key == 'downloads_paused').with_for_update())
            if self.media_clear_busy(db):
                raise ValueError('素材库正在清空，请完成后再开始下载。')
            if self.fleet:
                self.fleet.activate(db, expected_policy_version)
            # Release the new lists and resume claims in one transaction.
            self.lists.release(db)
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='false'))
            if not self.fleet:
                from webapp.download_policy import execution_decision
                execution_decision(self, db, 'local')

    def media_clear_busy(self, db=None):
        if db is None:
            with self.connect() as connection:
                return self.media_clear_busy(connection)
        counts = self._counts(db, ['action:clear_media'])
        return bool(counts.get('queued', 0) or counts.get('running', 0))

    def start_media_clear(self, payload):
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            db.execute(select(settings.c.key).where(settings.c.key == 'downloads_paused').with_for_update())
            if self.fleet:
                from webapp.database import instances
                if db.execute(select(instances.c.id).where(instances.c.paused == 0,
                        instances.c.role.in_(('worker', 'hybrid'))).limit(1)).first():
                    raise ValueError('请先暂停全部下载实例，再清空素材库。')
            if (not self.fleet and not self.downloads_paused(db)) or self._counts(db, ['action:download', 'action:subtitles']).get('running', 0):
                raise ValueError('请先暂停全部下载，并等待所有实例的下载任务停止后再清空素材库。')
            if self.media_clear_busy(db):
                raise ValueError('素材库正在清空，请勿重复提交。')
            indexing = self._counts(db, ['action:index'])
            if indexing.get('queued', 0) or indexing.get('running', 0):
                raise ValueError('请等待素材索引同步完成后再清空。')
            task_id = self._create(db, 'clear_media', payload, self.db.sequence(db, 'task'))
            row = db.execute(select(tasks).where(tasks.c.id == task_id)).mappings().one()
            self._transition(db, row, 'running')
            db.execute(tasks.update().where(tasks.c.id == task_id).values(
                status='running', started_at=self._timestamp(), worker_id=self.owner,
                instance_id=self.fleet.id if self.fleet else None,
                lease_until=(self.fleet.clock(db) if self.fleet else time.time()) + 60, attempts=1))
        return self.get(task_id)

    def download_queue(self):
        with self.connect() as db:
            counts = self._counts(db, ['action:download'])
            importing = self._counts(db, ['action:import'])
            waiting_lists = db.execute(select(func.count()).select_from(download_lists).where(
                download_lists.c.released == 0, or_(select(counters.c.scope).where(
                    counters.c.scope == 'batch:' + download_lists.c.id, counters.c.status == 'queued', counters.c.value > 0).exists(),
                    select(imports.c.id).where(imports.c.id == download_lists.c.id, imports.c.finished == 0).exists()))).scalar_one()
            return {'paused': self.downloads_paused(db), 'queued': counts.get('queued', 0), 'running': counts.get('running', 0),
                    'waiting_lists': waiting_lists, 'importing': importing.get('running', 0) + importing.get('queued', 0)}

    def apply_download_connection(self, config, clash=None):
        """Override existing jobs at execution time without rewriting their snapshots."""
        connection = {'proxy_mode': config.get('proxy_mode', 'manual'), 'proxy': config.get('proxy', '')}
        if connection['proxy_mode'] == 'clash' and not clash:
            raise ValueError('请先配置并启动 Clash 代理。')
        with self.connect() as db:
            if self.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            queue = self.connection_queue(db, lock=True)
            if not queue['paused']:
                raise ValueError('请先停止当前实例的下载，再应用当前连接。' if self.fleet else '请先停止全部下载，再应用当前连接。')
            if queue['running']:
                raise ValueError('当前下载进程尚未停止，请稍后再应用当前连接。')
            if not (queue['queued'] or queue['importing']):
                raise ValueError('当前没有排队的下载或导入任务。')
            cutoff = db.execute(select(func.max(tasks.c.seq))).scalar_one() or 0
            value = {'cutoff_seq': cutoff, 'config': connection, 'updated_at': self._timestamp()}
            if connection['proxy_mode'] == 'clash':
                value['clash'] = clash
            db.execute(self.db.upsert(settings, dict(key=self._connection_key(),
                value=json.dumps(value, ensure_ascii=False)), ['key']))
        return {'mode': connection['proxy_mode'], 'updated_at': value['updated_at']}

    def connection_queue(self, db=None, *, lock=False):
        """Connection checks concern this executor, not other nodes' activity."""
        if db is None:
            with self.connect() as connection:
                return self.connection_queue(connection, lock=lock)
        paused = self.downloads_paused(db, lock=lock)
        job = None
        if self.fleet:
            from webapp.database import instances
            statement = select(instances.c.paused).where(instances.c.id == self.fleet.id)
            node_paused = db.execute(statement.with_for_update() if lock else statement).scalar_one()
            job = self.workbench_job(db)
            paused = bool(node_paused or (paused and not job))
        counts = self._counts(db, ['batch:' + job] if job else ['action:download'])
        running = counts.get('running', 0)
        if self.fleet:
            running = db.execute(select(func.count()).select_from(tasks).where(
                tasks.c.instance_id == self.fleet.id, tasks.c.action == 'download',
                tasks.c.status == 'running')).scalar_one()
        if job:
            importing = db.execute(select(func.count()).select_from(imports.join(job_imports,
                imports.c.id == job_imports.c.id)).where(job_imports.c.job_id == job,
                imports.c.finished == 0)).scalar_one()
        else:
            pending = self._counts(db, ['action:import'])
            importing = pending.get('running', 0) + pending.get('queued', 0)
        return dict(paused=paused, queued=counts.get('queued', 0), running=running, importing=importing)

    def _connection_key(self):
        prefix = ('i:' + hashlib.sha256(self.fleet.id.encode()).hexdigest()[:16] + ':') if self.fleet else ''
        return prefix + 'download_connection_override'

    def download_connection(self):
        """Public summary; proxy credentials and controller paths stay private."""
        with self.connect() as db:
            saved = db.execute(select(settings.c.value).where(
                settings.c.key == self._connection_key())).scalar_one_or_none()
        value = json.loads(saved) if saved else None
        return {'mode': value['config']['proxy_mode'], 'updated_at': value['updated_at']} if value else None

    def set_download_settings(self, config, base, clash_directory):
        """One current configuration shared by all pending downloads."""
        value = {'config': config, 'base': str(base), 'clash_directory': str(clash_directory)}
        with self.connect() as db:
            db.execute(self.db.upsert(settings, dict(key='download_settings',
                value=json.dumps(value, ensure_ascii=False)), ['key']))

    def effective_download_payload(self, task_id, payload, db=None):
        # A claimed task keeps its start configuration until this attempt ends.
        if payload.get('_execution_settings'):
            return payload
        if not task_id:
            return payload
        if db is None:
            with self.connect() as connection:
                return self.effective_download_payload(task_id, payload, connection)
        if self.fleet:
            from webapp.jobs import apply_job_scope
            return apply_job_scope(self, db, task_id, self.fleet.execution(payload, db))
        current = db.execute(select(settings.c.value).where(settings.c.key == 'download_settings')).scalar_one_or_none()
        if current:
            value = json.loads(current)
            result = {**payload, 'config': value['config'], 'base': value['base']}
            result.pop('clash', None)
            if value['config'].get('proxy_mode') == 'clash':
                result['clash'] = {'directory': value['clash_directory']}
            from webapp.jobs import apply_job_scope
            return apply_job_scope(self, db, task_id, result)
        # Compatibility for standalone stores without a Web configuration.
        saved = db.execute(select(settings.c.value).where(
            settings.c.key == 'download_connection_override')).scalar_one_or_none()
        if not saved:
            return payload
        value = json.loads(saved)
        eligible = db.execute(select(tasks.c.id).where(tasks.c.id == task_id,
            tasks.c.seq <= value['cutoff_seq'], tasks.c.action.in_(('download', 'import')))).first()
        if not eligible:
            return payload
        result = {**payload, 'config': {**payload.get('config', {}), **value['config']}}
        result.pop('clash', None)
        if value.get('clash'):
            result['clash'] = value['clash']
        return result

    def stop_running_downloads(self, terminate):
        """Stop only verified owners, then release their tasks under the same row lock."""
        with self.connect() as db:
            statement = select(tasks.c.id, tasks.c.worker_id).where(tasks.c.status == 'running', tasks.c.action.in_(('download', 'subtitles')))
            if self.fleet:
                statement = statement.where(tasks.c.instance_id == self.fleet.id)
            owners = db.execute(statement.limit(64)).all()
        stopped = 0
        for task_id, owner in owners:
            with self.connect() as db:
                if self.db.sqlite:
                    db.exec_driver_sql('BEGIN IMMEDIATE')
                # Resume and new claims cannot pass until the old executor is gone.
                if not self.downloads_stopped(db, lock=True) and not (self.fleet and self.fleet.row(db)['stop_requested']):
                    break
                row = db.execute(self._locked(select(tasks).where(tasks.c.id == task_id))).mappings().first()
                if not row or row['status'] != 'running' or row['worker_id'] != owner or not terminate(owner):
                    continue
                progress = json.loads(row['progress']) if row['progress'] else {}
                progress.update(stage='paused', speed=None, eta=None)
                self._transition(db, row, 'queued')
                db.execute(tasks.update().where(tasks.c.id == task_id).values(status='queued', worker_id=None,
                    lease_until=None, available_at=0, finished_at=None, error=None,
                    attempts=max(0, row['attempts'] - 1), progress=json.dumps(progress, ensure_ascii=False)))
                self.statistics.transition(db, row, 'paused')
                self.statistics.settle(db, instance_id=row.get('instance_id'))
                if owner:
                    db.execute(workers.update().where(workers.c.id == owner).values(state='stopped', task_id=None, message='用户已停止下载。'))
                stopped += 1
        return stopped

    def rotation_completed(self, rotation_id, count_failures=True):
        with self.connect() as db:
            row = db.execute(select(rotations).where(rotations.c.id == rotation_id)).mappings().first()
            return (row['succeeded'] + (row['failed'] if count_failures else 0)) if row else 0

