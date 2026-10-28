"""Read legacy standalone ownership and upgrade it to unified execution."""
import json
from pathlib import Path
import re
import secrets

from sqlalchemy import select

from webapp.database import instances, settings, tasks, workers


class ModeTransitionError(ValueError):
    pass


def upgrade_unified(fleet, db, mode, previous):
    """Upgrade a legacy standalone owner, under the fleet admission lock.

    Never unlock another installation's queue. Pending conversions are simply
    cancelled; a completed conversion retains its identity and local settings.
    """
    from webapp.fleet import ONLINE_SECONDS
    store = fleet.store
    state = store.single
    standalone = bool(mode and mode.startswith('standalone:'))
    if not state:
        if standalone:
            raise ModeTransitionError('此数据库属于旧单机实例，请先使用原状态目录升级该实例。')
        if mode is None:
            # The first unified owner of a legacy local queue also owns its
            # pending filesystem operations; those paths belong to this host.
            db.execute(tasks.update().where(tasks.c.status == 'queued', tasks.c.action != 'download',
                tasks.c.target_instance_id.is_(None)).values(target_instance_id=fleet.id))
        return False
    if (state['instance_id'] != fleet.id or not previous
            or not secrets.compare_digest(previous['token'], fleet.token)
            or (standalone and mode != mode_key(state)) or mode not in ('true', mode_key(state))):
        raise ModeTransitionError('旧单机实例身份不匹配，请使用原数据库、实例 ID 和状态目录升级。')
    completed = state['status'] == 'completed' or standalone
    if completed:
        stamp = fleet.clock(db)
        if db.execute(select(instances.c.id).where(instances.c.id != fleet.id,
                instances.c.heartbeat > stamp - ONLINE_SECONDS).limit(1)).first():
            raise ModeTransitionError('旧单机队列还有在线实例，请停止旧服务并等待 45 秒后升级。')
        if db.execute(select(workers.c.id).where(workers.c.heartbeat > stamp - ONLINE_SECONDS).limit(1)).first():
            raise ModeTransitionError('旧单机下载进程仍在线，请停止旧服务并等待 45 秒后升级。')
        if db.execute(select(tasks.c.id).where(tasks.c.status == 'running',
                tasks.c.lease_until > stamp).limit(1)).first():
            raise ModeTransitionError('旧单机任务租约尚未到期，请等待旧任务退出后升级。')
        # Old standalone workers have no instance boot fence. Revoke their
        # expired ownership before the queue opens to unified executors. Normal
        # recovery will requeue/settle these rows with the existing counters.
        db.execute(tasks.update().where(tasks.c.status == 'running').values(worker_id=None, lease_until=None))
        db.execute(tasks.update().where(tasks.c.status.in_(('queued', 'running')), tasks.c.action != 'download',
            tasks.c.target_instance_id.is_(None)).values(target_instance_id=fleet.id))
        db.execute(settings.update().where(settings.c.key == 'fleet_enabled').values(value='true'))
        store.db.ensure(db, settings, {'key': 'downloads_paused', 'value': 'true'})
        db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='true'))
    return completed


def single_state(directory):
    try:
        value = json.loads((Path(directory) / 'single-mode.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or value.get('status') not in ('requested', 'completed')
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(value.get('instance_id', '')))
            or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('owner', '')))):
        raise ValueError('单机转换记录无效，请检查状态目录中的 single-mode.json。')
    return value


def mode_key(state):
    return 'standalone:' + state['owner'] if state else None
