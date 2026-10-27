"""Explicit, restartable conversion of an idle cluster to one local executor."""
import json
from pathlib import Path
import re
import secrets
import uuid

from sqlalchemy import select

from webapp.database import instances, settings, tasks, workers


class ModeTransitionError(ValueError):
    pass


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


def check_idle(store, identity, db):
    from webapp.fleet import ONLINE_SECONDS, database_time
    stamp = database_time(db, store.db.mysql)
    other = db.execute(select(instances.c.name).where(
        instances.c.id != identity, instances.c.heartbeat > stamp - ONLINE_SECONDS).limit(1)).first()
    if other:
        raise ModeTransitionError('还有其他在线实例，请先停止其他管理端和下载实例，等待 45 秒后再切换单机模式。')
    if db.execute(select(tasks.c.id).where(tasks.c.status == 'running').limit(1)).first():
        raise ModeTransitionError('还有正在执行的任务，请先暂停下载并等待下载、导入和索引任务结束，再切换单机模式。')
    if db.execute(select(tasks.c.id).where(tasks.c.status == 'queued', tasks.c.action != 'download',
            tasks.c.target_instance_id.is_not(None), tasks.c.target_instance_id != identity).limit(1)).first():
        raise ModeTransitionError('其他实例还有待处理的本地维护任务，请先完成这些任务，再切换单机模式。')


def check_conversion(store, identity):
    with store.connect() as db:
        db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget').with_for_update()).first()
        check_idle(store, identity, db)


def prepare_single(store, config_path):
    """Run only during startup, before HTTP business routes or workers start."""
    state = single_state(store.directory)
    if not state:
        return
    from webapp.deployment import write_profile
    from Utils.config import load_config
    from webapp.settings import save_settings
    with store.connect() as db:
        if store.db.sqlite:
            db.exec_driver_sql('BEGIN IMMEDIATE')
        store.db.ensure(db, settings, {'key': 'fleet_budget', 'value': '32'})
        db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget').with_for_update()).scalar_one()
        mode = db.execute(select(settings.c.value).where(settings.c.key == 'fleet_enabled')).scalar_one_or_none()
        if mode != mode_key(state):
            if mode != 'true':
                raise ModeTransitionError('数据库模式与转换记录不一致，请确认使用原来的数据库和状态目录。')
            check_idle(store, state['instance_id'], db)
            original = db.execute(select(instances).where(instances.c.id == state['instance_id'])).mappings().first()
            local = json.loads((store.directory / 'instance.json').read_text(encoding='utf-8'))
            if not original or not secrets.compare_digest(original['token'], str(local.get('token', ''))):
                raise ModeTransitionError('数据库中的原实例身份不匹配，请确认使用原来的数据库和状态目录。')
            config, _ = load_config(config_path)
            local_config = json.loads(original['config']).get('config', {})
            config.update(local_config)
            config.update(json.loads(original['limits']))
            # Write local settings before committing the database mode. Retrying
            # after a crash must not replace subsequent single-mode edits.
            save_settings(Path(config_path), config)
            db.execute(settings.update().where(settings.c.key == 'fleet_enabled').values(value=mode_key(state)))
            db.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='true'))
            store.statistics.settle(db, stop=True)
            db.execute(instances.update().values(paused=1, stop_requested=1, heartbeat=0, boot_id=uuid.uuid4().hex))
            db.execute(workers.update().values(heartbeat=0, state='stopped', task_id=None))
    runtime = store.directory / 'fleet-runtime.json'
    if runtime.exists():
        runtime.replace(store.directory / 'fleet-runtime.cluster-backup.json')
    write_profile(store.directory / 'single-mode.json', {**state, 'status': 'completed'})
    store.fleet = None
    store.single = {**state, 'status': 'completed'}
