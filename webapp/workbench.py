"""The download workbench controls one executor, with a durable job selection."""
import math
import time

from flask import jsonify, request
from sqlalchemy import func, select
from werkzeug.exceptions import Conflict

from webapp.database import download_lists, instances, job_profiles, settings, tasks
from webapp.fleet import DOWNLOAD_ROLES, ONLINE_SECONDS
from webapp.jobs import Jobs


def compact_status(value):
    """Keep task details in the inspector; the workbench needs one speed sum."""
    value = dict(value)
    stamp = time.time()
    transferring, measured, speed = 0, 0, 0
    for task in value.pop('active_tasks', []):
        progress = task.get('progress') or {}
        if task.get('status') != 'running' or progress.get('stage') != 'downloading':
            continue
        transferring += 1
        reported = task.get('progress_updated_at') or progress.get('_reported_at')
        changed = progress.get('last_bytes_at')
        rate = progress.get('speed')
        if ((reported and stamp - reported > 10) or (changed and stamp - changed > 10)
                or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0):
            continue
        measured += 1
        speed += rate
    value['transfer_summary'] = dict(time=stamp, transferring=transferring, measured=measured, speed=speed)
    if value.get('job'):
        value['job'] = {key: item for key, item in value['job'].items()
                        if key not in ('running_videos', 'imports', 'transfer_speed')}
    return value


class Workbench:
    def __init__(self, store):
        self.store = store
        self.jobs = Jobs(store)

    @staticmethod
    def selection(db, key):
        return db.execute(select(settings.c.value).where(settings.c.key == key)).scalar_one_or_none()

    def visible_job(self, identity, *, compact=False):
        job = self.jobs.detail(identity, details=not compact) if identity else None
        return job if job and not job['deleted'] else None

    def node(self, identity, db, *, lock=False):
        statement = select(instances).where(instances.c.id == identity, instances.c.role.in_(DOWNLOAD_ROLES))
        row = db.execute(statement.with_for_update() if lock else statement).mappings().first()
        if not row:
            raise ValueError('请先选择一个下载实例。')
        return row

    @staticmethod
    def decorate_cookie_names(tasks, base, store, *, scope=None):
        """Attach the current Cookie label without exposing Cookie contents."""
        if not tasks or not (base or scope):
            return tasks
        ids = {(task.get('progress') or {}).get('cookie_profile_id') for task in tasks
               if not (task.get('progress') or {}).get('cookie_profile_name')}
        ids.discard(None)
        if not ids:
            return tasks
        try:
            from webapp.cookie_profiles import CookieProfiles
            from webapp.database import cookie_profiles
            scope = scope or CookieProfiles.scope_for(base, store)
            # Display reads need metadata only, without legacy migration or
            # credential materialization on every workbench poll.
            with store.connect() as db:
                names = dict(db.execute(select(cookie_profiles.c.id, cookie_profiles.c.name).where(
                    cookie_profiles.c.scope == scope, cookie_profiles.c.id.in_(ids),
                    cookie_profiles.c.deleted == 0)).all())
        except Exception:
            names = {}
        result = []
        for task in tasks:
            progress = task.get('progress') or {}
            identity = progress.get('cookie_profile_id')
            if identity and not progress.get('cookie_profile_name'):
                task = {**task, 'progress': {**progress,
                    'cookie_profile_name': names.get(identity, identity)}}
            result.append(task)
        return result

    def status(self, identity, preview=None, *, compact=False):
        with self.store.connect() as db:
            row = self.node(identity, db)
            selected = self.selection(db, 'workbench_job:' + identity)
            # Legacy global selections are intentionally not inherited by a new instance.
            active = self.store.decode_many(db.execute(select(tasks).where(
                tasks.c.instance_id == identity, tasks.c.action == 'download',
                tasks.c.status == 'running').order_by(tasks.c.seq).limit(100)).mappings())
            running = db.execute(select(func.count()).select_from(tasks).where(
                tasks.c.instance_id == identity, tasks.c.action == 'download',
                tasks.c.status == 'running')).scalar_one()
            online = row['heartbeat'] > self.store.fleet.clock(db) - ONLINE_SECONDS
            paused = bool(row['paused'])
            enabled = not paused and not row['stop_requested']
            from webapp.disk_budget import resource_blocked
            blocked = resource_blocked(self.store, db, identity)
            error = row['error']
            session = self.store.statistics.current_session(db, identity)
        active = self.decorate_cookie_names(active, None, self.store, scope=identity)
        job = self.visible_job(preview or selected, compact=compact)
        if job:
            desired = selected == job['id'] and enabled
            job = {**job, 'running': sum(task['batch_id'] == job['id'] for task in active),
                   'released': desired, 'desired_running': desired,
                   'waiting_reason': (error or ('当前实例离线。' if not online else
                       '等待下载实例释放磁盘空间' if blocked else '')) if desired and job['queued'] else '',
                   'running_videos': [task for task in active if task['batch_id'] == job['id']]}
            job['state'] = self.jobs.execution_state(job)
        return dict(instance_id=identity, selected_job_id=selected, job=job, paused=paused,
                    stop_requested=bool(row['stop_requested']), online=online, running=running,
                    queued=job['queued'] if job else 0, active_tasks=active, error=error, session=session)

    def control(self, identity, action, job_id=None, expected=None):
        if action not in ('start', 'pause', 'stop'):
            raise ValueError('不支持的工作台操作。')
        if action == 'start' and not isinstance(job_id, str):
            raise ValueError('请选择一个有效的下载任务。')
        with self.store.connect() as db:
            if self.store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            # Share admission's lock order so no claim can slip past a stop or job switch.
            db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget').with_for_update()).first()
            db.execute(select(settings.c.key).where(settings.c.key == 'downloads_paused').with_for_update()).first()
            row = self.node(identity, db, lock=True)
            key = 'workbench_job:' + identity
            selected = db.execute(select(settings.c.value).where(settings.c.key == key)).scalar_one_or_none()
            if action == 'start':
                if expected and any(row[k] != expected[k] for k in
                        ('boot_id', 'config', 'limits', 'paused', 'stop_requested')):
                    raise Conflict('检查期间实例设置或开关发生变化，请重新开始。')
                if expected and selected != expected['selected_job_id']:
                    raise Conflict('检查期间实例任务发生变化，请重新开始。')
                if row['heartbeat'] <= self.store.fleet.clock(db) - ONLINE_SECONDS:
                    raise ValueError('当前实例离线，请恢复连接后开始。')
                if self.store.media_clear_busy(db):
                    raise Conflict('素材库正在清空，请完成后再开始。')
                self.jobs.require(db, job_id)
                self.jobs.ensure(db, job_id, {})
                counts = self.store._counts(db, ['batch:' + job_id])
                from webapp.database import imports, job_imports
                importing = db.execute(select(imports.c.id).join(job_imports, imports.c.id == job_imports.c.id).where(
                    job_imports.c.job_id == job_id, imports.c.finished == 0).limit(1)).first()
                if not (counts.get('queued') or counts.get('running') or importing):
                    raise ValueError('任务没有待处理视频，请先追加视频或重试失败条目。')
                self.jobs.stop_running(db, (tasks.c.instance_id == identity) &
                    ((tasks.c.batch_id != job_id) | tasks.c.batch_id.is_(None)))
                db.execute(self.store.db.upsert(settings, {'key': key, 'value': job_id}, ['key']))
                db.execute(instances.update().where(instances.c.id == identity).values(
                    paused=0, stop_requested=0, error=''))
                self.store.statistics.start_session(db, identity)
            else:
                db.execute(instances.update().where(instances.c.id == identity).values(
                    paused=1, stop_requested=int(action == 'stop')))
                if action == 'stop':
                    self.jobs.stop_running(db, tasks.c.instance_id == identity)
                self.store.statistics.stop_session(db, identity)
            self.store.statistics.settle(db, instance_id=identity)


def register_workbench(app, store, configuration, preflight):
    workbench = Workbench(store)
    from webapp.display_cache import display_cache
    views = display_cache(app)

    @app.route('/api/jobs/workbench', methods=['GET', 'POST'])
    def download_workbench():
        compact = request.args.get('compact') == '1'
        fleet = store.fleet
        if not fleet:
            # A standalone service has exactly one executor.
            config, base = configuration()
            if request.method == 'POST':
                data = request.get_json(silent=True)
                if not isinstance(data, dict):
                    raise ValueError('请提交 JSON 对象。')
                action = data.get('action')
                if action == 'start':
                    preflight(config, base)
                    from webapp.fleet import POLICY_FIELDS
                    latest, _ = configuration()
                    if any(config.get(key) != latest.get(key) for key in POLICY_FIELDS):
                        raise Conflict('检查期间下载策略发生变化，请重新开始。')
                workbench.jobs.control(data.get('job_id') or store.selected_download_job(), action,
                    config, {'resume_system': action == 'start'})
            with store.connect() as db:
                identity = workbench.selection(db, 'selected_download_job')
            job = workbench.visible_job(request.args.get('job_id') or identity, compact=compact)
            queue = store.download_queue()
            active = store.page(actions=['download'], status='active', limit=100, order='asc')['items']
            active = Workbench.decorate_cookie_names(active, base, store)
            with store.connect() as db:
                session = store.statistics.current_session(db)
            value = dict(instance_id=None, selected_job_id=identity, job=job,
                paused=queue['paused'] or not job or not job['released'], running=queue['running'],
                queued=job['queued'] if job else 0, online=True, active_tasks=active, error='', session=session)
            return jsonify(compact_status(value) if compact else value)
        if not fleet.can_manage:
            return jsonify(error='此接口仅供管理端使用。'), 403
        from webapp.fleet_routes import selected_id, rpc
        identity = selected_id()
        if request.method == 'POST':
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise ValueError('请提交 JSON 对象。')
            expected = None
            if data.get('action') == 'start':
                with store.connect() as db:
                    expected = dict(workbench.node(identity, db))
                    expected['selected_job_id'] = db.execute(select(settings.c.value).where(
                        settings.c.key == 'workbench_job:' + identity)).scalar_one_or_none()
                rpc(fleet, identity, '/api/fleet/preflight', method='POST', data={})
            workbench.control(identity, data.get('action'), data.get('job_id'), expected)
        preview = request.args.get('job_id')
        value = views.read(('workbench', identity, preview, compact),
                           lambda: workbench.status(identity, preview, compact=compact))
        return jsonify(compact_status(value) if compact else value)
