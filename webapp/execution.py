"""Execution commands keep system permission, job intent and node health separate."""
from sqlalchemy import select
from werkzeug.exceptions import Conflict

from webapp.database import instances, settings


class ExecutionControlService:
    def __init__(self, store):
        self.store = store
        self.prepare_resume = None

    def status(self, queue=None):
        from webapp.disk_budget import resource_blocked, resource_blocks
        queue = queue if queue is not None else self.store.download_queue()
        blockers = []
        if queue['paused']:
            blockers.append(dict(code='system_paused', message='系统已暂停领取。'))
        with self.store.connect() as db:
            released = self.store._released_queued_count(db)
            if self.store.fleet:
                from webapp.fleet import DOWNLOAD_ROLES, ONLINE_SECONDS
                rows = db.execute(select(instances.c.id, instances.c.paused).where(
                    instances.c.role.in_(DOWNLOAD_ROLES),
                    instances.c.heartbeat > self.store.fleet.clock(db) - ONLINE_SECONDS)).all()
                if not rows:
                    blockers.append(dict(code='no_online_instances', message='没有在线的下载实例；仅有管理端在线无法执行下载。',
                        href='/#instances', label='查看实例与环境'))
                elif all(row.paused for row in rows):
                    blockers.append(dict(code='instances_paused', message=f'{len(rows)} 个在线下载实例均已暂停，请单独恢复至少一个实例。',
                        href='/#instances', label='恢复下载实例'))
                elif len(resource_blocks(db, [row.id for row in rows if not row.paused])) == len([row for row in rows if not row.paused]):
                    blockers.append(dict(code='disk_wait', message='下载实例正在等待磁盘空间恢复。', href='/#settings', label='查看磁盘管理'))
            elif resource_blocked(self.store, db):
                blockers.append(dict(code='disk_wait', message='本机正在等待磁盘空间恢复。', href='/#settings', label='查看磁盘管理'))
        if queue['queued'] and not released:
            blockers.append(dict(code='jobs_not_started', message='待处理视频所属任务尚未开始或已暂停，请先选择任务并开始下载。',
                href='/#overview', label='选择下载任务'))
        elif not queue['queued'] and not queue['running'] and not queue['importing']:
            blockers.append(dict(code='no_pending_videos', message='当前没有待处理视频。',
                href='/#overview', label='选择下载任务'))
        message = ('系统已暂停领取。' if queue['paused'] else '系统已恢复领取。')
        remaining = [item['message'] for item in blockers if item['code'] != 'system_paused']
        message += ''.join(remaining) if remaining else '仅执行当前开始的任务，已暂停的实例需单独恢复。'
        return dict(paused=queue['paused'], released_queued=released, blockers=blockers, message=message,
                    selected_job=self.selected_job())

    def selected_job(self):
        from webapp.jobs import Jobs
        identity = self.store.selected_download_job()
        job = Jobs(self.store).detail(identity) if identity else None
        return job if job and not job['deleted'] else None

    def resume_system(self):
        queue = self.store.download_queue()
        if queue['paused'] and queue['running']:
            raise Conflict('当前下载进程尚未停止，请稍后恢复系统领取。')
        if self.store.media_clear_busy():
            raise Conflict('素材库正在清空，请完成后再恢复系统领取。')
        if self.prepare_resume:
            self.prepare_resume()
        # Lock the same flag as clear/stop. Never release a not-yet-started job.
        with self.store.connect() as db:
            if self.store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            db.execute(select(settings.c.key).where(settings.c.key == 'downloads_paused').with_for_update()).first()
            if self.store.media_clear_busy(db):
                raise Conflict('素材库正在清空，请完成后再恢复系统领取。')
            if self.store.downloads_paused(db) and self.store._counts(db, ['action:download']).get('running', 0):
                raise Conflict('当前下载进程尚未停止，请稍后恢复系统领取。')
            if self.store.fleet:
                self.store.fleet.activate(db)
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='false'))
        queue = self.store.download_queue()
        execution = self.status(queue)
        return dict(message=execution['message'], download_queue=queue, execution=execution)


def isolate_download_failure(store, reason):
    """An executor's environment failure must not change a customer's intent."""
    if store.fleet:
        store.fleet.quarantine(store.fleet.id, str(reason), boot_id=store.fleet.boot_id)
    else:
        # In standalone mode this flag controls the only executor.
        store.set_downloads_paused(True)
