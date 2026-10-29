"""The download workbench controls one executor, with a durable job selection."""
from flask import jsonify, request
from sqlalchemy import func, select
from werkzeug.exceptions import Conflict

from webapp.database import instances, settings, tasks
from webapp.fleet import DOWNLOAD_ROLES, ONLINE_SECONDS
from webapp.jobs import Jobs


class Workbench:
    def __init__(self, store):
        self.store = store
        self.jobs = Jobs(store)

    def node(self, identity, db, *, lock=False):
        statement = select(instances).where(instances.c.id == identity, instances.c.role.in_(DOWNLOAD_ROLES))
        row = db.execute(statement.with_for_update() if lock else statement).mappings().first()
        if not row:
            raise ValueError('请先选择一个下载实例。')
        return row

    def status(self, identity, preview=None):
        with self.store.connect() as db:
            row = self.node(identity, db)
            selected = db.execute(select(settings.c.value).where(
                settings.c.key == 'workbench_job:' + identity)).scalar_one_or_none()
            # Legacy global selections are intentionally not inherited by a new instance.
            active = [self.store.decode(task) for task in db.execute(select(tasks).where(
                tasks.c.instance_id == identity, tasks.c.action == 'download',
                tasks.c.status == 'running').order_by(tasks.c.seq).limit(100)).mappings()]
            running = db.execute(select(func.count()).select_from(tasks).where(
                tasks.c.instance_id == identity, tasks.c.action == 'download',
                tasks.c.status == 'running')).scalar_one()
            online = row['heartbeat'] > self.store.fleet.clock(db) - ONLINE_SECONDS
            paused = bool(row['paused'])
            enabled = not paused and not row['stop_requested']
            from webapp.disk_budget import resource_blocked
            blocked = resource_blocked(self.store, db, identity)
            error = row['error']
        job = self.jobs.detail(preview or selected) if preview or selected else None
        if job and job['deleted']:
            job = None
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
                    queued=job['queued'] if job else 0, active_tasks=active, error=error)

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
            else:
                db.execute(instances.update().where(instances.c.id == identity).values(
                    paused=1, stop_requested=int(action == 'stop')))
                if action == 'stop':
                    self.jobs.stop_running(db, tasks.c.instance_id == identity)
            self.store.statistics.settle(db)


def register_workbench(app, store, configuration, preflight):
    workbench = Workbench(store)

    @app.route('/api/jobs/workbench', methods=['GET', 'POST'])
    def download_workbench():
        fleet = store.fleet
        if not fleet:
            # A standalone service has exactly one executor.
            if request.method == 'POST':
                data = request.get_json(silent=True)
                if not isinstance(data, dict):
                    raise ValueError('请提交 JSON 对象。')
                config, base = configuration()
                action = data.get('action')
                if action == 'start':
                    preflight(config, base)
                    from webapp.fleet import POLICY_FIELDS
                    latest, _ = configuration()
                    if any(config.get(key) != latest.get(key) for key in POLICY_FIELDS):
                        raise Conflict('检查期间下载策略发生变化，请重新开始。')
                workbench.jobs.control(data.get('job_id') or store.selected_download_job(), action,
                    config, {'resume_system': action == 'start'})
            identity = store.selected_download_job()
            job = workbench.jobs.detail(request.args.get('job_id') or identity) if request.args.get('job_id') or identity else None
            queue = store.download_queue()
            active = store.page(actions=['download'], status='active', limit=100, order='asc')['items']
            return jsonify(instance_id=None, selected_job_id=identity, job=job,
                paused=queue['paused'] or not job or not job['released'], running=queue['running'],
                queued=job['queued'] if job else 0, online=True, active_tasks=active, error='')
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
        return jsonify(workbench.status(identity, request.args.get('job_id')))
