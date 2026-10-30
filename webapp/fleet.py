"""MySQL-backed instance registry, execution policy and transactional admission."""
import json
import os
from pathlib import Path
import re
import secrets
import socket
import time
import uuid

from sqlalchemy import func, select, text

from webapp.database import instances, settings, tasks, counters, download_lists
from webapp.disk_budget import DISK_FIELDS
from webapp.service_role import normalize_role

POLICY_FIELDS = {'download_methods', 'download_method', 'download_format', 'download_attempts',
                 'yt5s_quality', 'downmp3_quality', 'downmp3_timeout', 'yt1d_quality', 'yt1d_timeout',
                 'bergung_quality', 'bergung_timeout',
                 'fragment_concurrency', 'storage_mode'}
POLICY_FIELDS |= {'scheduler_mode', 'download_channels', 'auto_channel_attempts'}
LOCAL_FIELDS = {'path', 'dir', 'url', 'proxy_mode', 'proxy', 'youtube_cookie', 'browser_executable',
                'js_runtimes', 'worker_count', 'yt5s_browser_count', 'min_free_gb',
                'max_log_mb', 'log_retention_days', 'shard_downloads'}
LOCAL_FIELDS |= DISK_FIELDS
LOCAL_FIELDS |= {'throughput_mode', 'bandwidth_mbps'}
LOCAL_FIELDS |= {'download_connection_limit', 'shared_parser'}
ONLINE_SECONDS = 45
# Recognize peers that have not been upgraded yet; new rows always use hybrid.
DOWNLOAD_ROLES = ('worker', 'hybrid')
RESOURCE_LIMITS = {'worker_count': (1, 64), 'yt5s_browser_count': (1, 16), 'min_free_gb': (0, 10000)}


class InstanceIdentityConflict(ValueError):
    def __init__(self, identity, host, retry_after):
        self.identity, self.host = identity, host
        self.retry_after = max(0, min(ONLINE_SECONDS, retry_after))
        super().__init__(f'实例 ID 已在线：{identity}，登记主机：{host}。'
                         '请为每个同时运行的实例使用独立状态目录及 VIDEO_INSTANCE_ID。')


def database_time(db, mysql):
    return float(db.execute(text('SELECT UNIX_TIMESTAMP(NOW(6))')).scalar_one()) if mysql else time.time()


class Fleet:
    def __init__(self, store, role, *, identity=None, url='', name=None):
        self.store, self.role = store, normalize_role(role)
        path = store.directory / 'instance.json'
        try:
            with path.open('x', encoding='utf-8') as output:
                json.dump({'id': uuid.uuid4().hex, 'token': secrets.token_urlsafe(40)}, output)
        except FileExistsError:
            pass
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.id = (identity or os.environ.get('VIDEO_INSTANCE_ID')
                   or (store.single or {}).get('instance_id')
                   or (store.fleet.id if store.fleet else None) or saved['id'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.id):
            raise ValueError('VIDEO_INSTANCE_ID 只能包含 1 到 64 位字母、数字、下划线或短横线。')
        self.token, self.boot_id = saved['token'], uuid.uuid4().hex
        self.url = (url or os.environ.get('VIDEO_INSTANCE_URL', '')).rstrip('/')
        if self.url:
            from urllib.parse import urlsplit
            parsed = urlsplit(self.url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                raise ValueError('VIDEO_INSTANCE_URL 请填写管理端可访问的实例 HTTP(S) 地址，不带路径或账号。')
        self.name = name or os.environ.get('VIDEO_INSTANCE_NAME') or socket.gethostname()
        self.local = None
        store.fleet = self

    @property
    def can_manage(self):
        return True

    @property
    def can_download(self):
        return True

    def untargeted_actions(self):
        return ('download', 'import')

    def clock(self, db):
        return database_time(db, self.store.db.mysql)

    def initialize(self, config, base, clash_directory):
        with self.store.connect() as db:
            if self.store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            self.store.db.ensure(db, settings, {'key': 'fleet_budget', 'value': '0'})
            db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget').with_for_update()).scalar_one()
            mode = db.execute(select(settings.c.value).where(settings.c.key == 'fleet_enabled')).scalar_one_or_none()
            previous = db.execute(select(instances).where(instances.c.id == self.id)).mappings().first()
            stamp = self.clock(db)
            if previous and previous['heartbeat'] > stamp - ONLINE_SECONDS:
                import psutil
                if previous['host'] != socket.gethostname() or psutil.pid_exists(previous['pid']):
                    raise InstanceIdentityConflict(self.id, previous['host'], previous['heartbeat'] + ONLINE_SECONDS - stamp)
            from webapp.modes import upgrade_unified
            upgraded = upgrade_unified(self, db, mode, previous)
            self.store.db.ensure(db, settings, {'key': 'fleet_enabled', 'value': 'true'})
            values = {'id': self.id, 'name': self.name[:100], 'role': self.role, 'host': socket.gethostname(),
                      'pid': os.getpid(), 'boot_id': self.boot_id, 'heartbeat': self.clock(db),
                      'url': self.url, 'token': self.token, 'config': previous['config'] if previous else '{}', 'limits': '{}',
                      'paused': int(self.can_download), 'stop_requested': 0, 'error': ''}
            updates = {key: values[key] for key in values if key not in ('id', 'limits', 'paused', 'stop_requested', 'name')}
            if previous and previous['role'] not in DOWNLOAD_ROLES and self.can_download:
                updates.update(paused=1, stop_requested=0)
            if previous:
                updates['limits'] = json.dumps(self.resource_limits(previous))
            if upgraded:
                updates.update(limits='{}', paused=1, stop_requested=0)
            db.execute(self.store.db.upsert(instances, values, ['id'], updates))
        self.save_local(config, base, clash_directory)
        runtime = self.store.directory / 'fleet-runtime.json'
        temporary = runtime.with_suffix('.tmp')
        temporary.write_text(json.dumps({'role': self.role, 'id': self.id, 'boot_id': self.boot_id}), encoding='utf-8')
        temporary.replace(runtime)
        if self.store.single:
            # Archive only after the new registry/runtime are durable. A failed
            # filesystem write can safely retry the same ownership migration.
            (self.store.directory / 'single-mode.json').replace(self.store.directory / 'single-mode.legacy.json')
            self.store.single = None

    def row(self, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.row(connection)
        return db.execute(select(instances).where(instances.c.id == self.id)).mappings().one()

    def save_local(self, config, base, clash_directory):
        from Utils.config import video_directory
        self.local = {'config': dict(config), 'base': str(base), 'clash_directory': str(clash_directory),
                      'directory': str(video_directory(config, Path(base)).resolve())}
        with self.store.connect() as db:
            previous = json.loads(self.row(db)['config'])
            changed = any(previous.get('config', {}).get(k) != config.get(k) for k in POLICY_FIELDS)
            self.local['policy_version'] = max(1, previous.get('policy_version', 0) + int(changed))
            db.execute(instances.update().where(instances.c.id == self.id, instances.c.boot_id == self.boot_id)
                       .values(config=json.dumps(self.local, ensure_ascii=False)))

    def resource_limits(self, row=None):
        """Only resource controls may override the node's saved configuration.

        Older registry rows can contain strategy fields in this object. Merging
        them blindly makes every successful YAML save read back the old policy.
        """
        values = json.loads((self.row() if row is None else row)['limits'])
        return {key: values[key] for key in RESOURCE_LIMITS if key in values}

    def local_settings(self, db=None):
        row = self.row(db)
        local = json.loads(row['config'])
        local['config'].update(self.resource_limits(row))
        return local

    def policy(self, *, active=False, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.policy(active=active, db=connection)
        if active:
            value = db.execute(select(settings.c.value).where(settings.c.key == 'fleet_active_policy')).scalar_one_or_none()
            return json.loads(value) if value else None
        local = self.local_settings(db)
        return {'version': local.get('policy_version', 1),
                'config': {k: local['config'][k] for k in POLICY_FIELDS if k in local['config']}}

    def save_policy(self, config):
        """Compatibility API: update this instance only; HTTP saves also persist YAML."""
        local = self.local_settings()
        self.save_local({**local['config'], **config}, local['base'], local['clash_directory'])
        return self.policy()

    def execution_policy(self, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.execution_policy(connection)
        return self.policy(db=db) if self.store.workbench_job(db) or self.policy(active=True, db=db) else None

    def activate(self, db, expected_version=None):
        # This shared record grants execution permission, never supplies strategy.
        value = {'version': 1, 'config': {}}
        db.execute(self.store.db.upsert(settings, {'key': 'fleet_active_policy', 'value': json.dumps(value)}, ['key']))

    def execution(self, payload, db):
        local = self.local_settings(db)
        policy = self.execution_policy(db)
        if not policy:
            raise ValueError('尚未从管理后台开始下载。')
        result = {**payload, 'base': local['base'], 'config': dict(local['config']),
                  'policy_version': policy['version']}
        result.pop('clash', None)
        if result['config'].get('proxy_mode') == 'clash':
            result['clash'] = {'directory': local['clash_directory']}
        return result

    def admission(self, db, *, lock=True):
        from webapp.disk_budget import resource_blocked
        if resource_blocked(self.store, db):
            return False
        statement = select(settings.c.value).where(settings.c.key == 'fleet_budget')
        budget = db.execute(statement.with_for_update() if lock else statement).scalar_one()
        # One short database lock serializes slot allocation across all instances.
        row = self.row(db)
        stamp = self.clock(db)
        if (not self.can_download or row['paused'] or row['boot_id'] != self.boot_id
                or row['heartbeat'] < stamp - ONLINE_SECONDS
                or not (self.store.workbench_job(db) or self.policy(active=True, db=db))):
            return False
        active = tasks.c.status == 'running'
        valid = tasks.c.lease_until > stamp
        global_count = db.execute(select(func.count()).select_from(tasks).where(active, valid, tasks.c.action == 'download')).scalar_one()
        local_count = db.execute(select(func.count()).select_from(tasks).where(active, valid, tasks.c.action == 'download', tasks.c.instance_id == self.id)).scalar_one()
        from webapp.throughput import runtime_limits
        config = self.local_settings(db)['config']
        limit = runtime_limits(config, self.store.directory)['workers']
        return (int(budget) == 0 or global_count < int(budget)) and local_count < limit

    def beat(self, error=None, throughput=None):
        with self.store.connect() as db:
            values = {'heartbeat': self.clock(db)}
            if error is not None:
                values['error'] = str(error)[:500]
            if throughput is not None:
                local = json.loads(db.execute(select(instances.c.config).where(
                    instances.c.id == self.id).with_for_update()).scalar_one())
                local['throughput'] = throughput
                values['config'] = json.dumps(local, ensure_ascii=False)
            return db.execute(instances.update().where(instances.c.id == self.id, instances.c.boot_id == self.boot_id)
                              .values(**values)).rowcount > 0

    def should_stop(self):
        row = self.row()
        return row['boot_id'] != self.boot_id or bool(row['stop_requested'])

    def quarantine(self, identity, reason, *, boot_id=None):
        with self.store.connect() as db:
            statement = instances.update().where(instances.c.id == identity)
            if boot_id:
                statement = statement.where(instances.c.boot_id == boot_id)
            db.execute(statement.values(paused=1, error=str(reason)[:500]))

    def list(self):
        with self.store.connect() as db:
            stamp = self.clock(db)
            rows = db.execute(select(instances).order_by(instances.c.name, instances.c.id)).mappings().all()
            running = db.execute(select(tasks.c.instance_id, func.count()).where(tasks.c.status == 'running',
                tasks.c.action == 'download').group_by(tasks.c.instance_id)).all()
            counts = dict(running)
            by_instance = {}
            current_tasks = {}
            for task in db.execute(select(tasks.c.id, tasks.c.instance_id, tasks.c.batch_id,
                    tasks.c.video_id, tasks.c.title, tasks.c.progress).where(
                    tasks.c.action == 'download', tasks.c.status == 'running').order_by(tasks.c.seq)).mappings():
                progress = json.loads(task['progress']) if task['progress'] else {}
                by_instance.setdefault(task['instance_id'], []).append(progress)
                current_tasks.setdefault(task['instance_id'], []).append({
                    key: task[key] for key in ('id', 'batch_id', 'video_id', 'title')} | {'progress': progress})
            totals = {}
            for scope, status, value in db.execute(select(counters).where(counters.c.scope.startswith('instance:'))):
                totals.setdefault(scope.removeprefix('instance:'), {})[status] = int(value)
            result = []
            selections = dict(db.execute(select(settings.c.key, settings.c.value).where(
                settings.c.key.startswith('workbench_job:'))).all())
            jobs = {job['id']: dict(job) for job in db.execute(select(
                download_lists.c.id, download_lists.c.name, download_lists.c.customer).where(
                    download_lists.c.id.in_(list(selections.values())))).mappings()}
            for row in rows:
                local = json.loads(row['config'])
                config = {**local.get('config', {}), **self.resource_limits(row)}
                progress = by_instance.get(row['id'], [])
                result.append({k: row[k] for k in ('id', 'name', 'role', 'host', 'url', 'heartbeat', 'paused', 'stop_requested', 'error', 'boot_id')} | {
                    'online': row['heartbeat'] > stamp - ONLINE_SECONDS, 'running': counts.get(row['id'], 0),
                    'transferring': sum(p.get('stage') == 'downloading' for p in progress),
                    'speed': sum(p.get('speed') or 0 for p in progress if p.get('stage') == 'downloading'),
                    'worker_count': config.get('worker_count', 1), 'yt5s_browser_count': config.get('yt5s_browser_count', 2),
                    'throughput': local.get('throughput'),
                    'directory': local.get('directory', ''),
                    'policy': {k: config[k] for k in POLICY_FIELDS if k in config},
                    'policy_version': local.get('policy_version', 1),
                    'min_free_gb': config.get('min_free_gb', 2), 'statistics': totals.get(row['id'], {})})
                result[-1]['current_tasks'] = current_tasks.get(row['id'], [])
                result[-1]['selected_job'] = jobs.get(selections.get('workbench_job:' + row['id']))
            budget = int(db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget')).scalar_one())
        return {'items': result, 'global_limit': budget, 'role': self.role, 'instance_id': self.id}

    def control(self, identity, data):
        with self.store.connect() as db:
            db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget').with_for_update()).first()
            row = db.execute(select(instances).where(instances.c.id == identity).with_for_update()).mappings().first()
            if not row or row['role'] not in DOWNLOAD_ROLES:
                raise ValueError('下载实例不存在。')
            values = {}
            if 'name' in data:
                if not isinstance(data['name'], str) or not data['name'].strip() or len(data['name']) > 100:
                    raise ValueError('实例名称需要 1 到 100 个字符。')
                values['name'] = data['name'].strip()
            limits = self.resource_limits(row)
            for key, bounds in RESOURCE_LIMITS.items():
                if key in data:
                    value = data[key]
                    if type(value) is not int or not bounds[0] <= value <= bounds[1]:
                        raise ValueError(f'{key} 必须在 {bounds[0]} 到 {bounds[1]} 之间。')
                    limits[key] = value
            if set(data) - {'name', 'action', 'worker_count', 'yt5s_browser_count', 'min_free_gb'}:
                raise ValueError('实例配置包含不支持的字段。')
            if 'action' in data:
                action = data['action']
                if action not in ('resume', 'drain', 'stop'):
                    raise ValueError('不支持的实例操作。')
                values.update(paused=int(action != 'resume'), stop_requested=int(action == 'stop'))
                if action == 'resume':
                    values['error'] = ''
            db.execute(instances.update().where(instances.c.id == identity).values(**values, limits=json.dumps(limits)))

    def resolve_target(self, identity):
        with self.store.connect() as db:
            if not identity:
                rows = db.execute(select(instances.c.id).where(instances.c.role.in_(DOWNLOAD_ROLES),
                    instances.c.heartbeat > self.clock(db) - ONLINE_SECONDS)).scalars().all()
                if len(rows) != 1:
                    raise ValueError('请先选择一个下载实例。')
                identity = rows[0]
            row = db.execute(select(instances).where(instances.c.id == identity, instances.c.role.in_(DOWNLOAD_ROLES))).mappings().first()
            if not row or row['heartbeat'] < self.clock(db) - ONLINE_SECONDS or (row['id'] != self.id and not row['url']):
                raise ValueError('实例离线或未配置管理端可访问的 VIDEO_INSTANCE_URL。')
            if row['id'] != self.id and row['host'] != socket.gethostname():
                import ipaddress
                from urllib.parse import urlsplit
                hostname = (urlsplit(row['url']).hostname or '').lower()
                try:
                    address = ipaddress.ip_address(hostname)
                    local_only = address.is_loopback or address.is_unspecified
                except ValueError:
                    local_only = hostname == 'localhost' or hostname.endswith('.localhost')
                if local_only:
                    raise ValueError(f'下载实例“{row["name"]}”登记了本机回环地址 {row["url"]}，管理端无法用它访问另一台机器。'
                                     '请在该实例的 /setup 中改为管理端可访问的 IP 或域名，再重启该实例。')
            return dict(row)
