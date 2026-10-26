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

from webapp.database import instances, settings, tasks, counters
from webapp.disk_budget import DISK_FIELDS

POLICY_FIELDS = {'download_methods', 'download_method', 'download_format', 'download_attempts',
                 'yt5s_quality', 'downmp3_quality', 'downmp3_timeout', 'fragment_concurrency', 'storage_mode'}
LOCAL_FIELDS = {'path', 'dir', 'url', 'proxy_mode', 'proxy', 'youtube_cookie', 'browser_executable',
                'js_runtimes', 'worker_count', 'yt5s_browser_count', 'min_free_gb',
                'max_log_mb', 'log_retention_days', 'shard_downloads'}
LOCAL_FIELDS |= DISK_FIELDS
ONLINE_SECONDS = 45
DOWNLOAD_ROLES = ('worker', 'hybrid')
MANAGEMENT_ROLES = ('manager', 'hybrid')


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
        if role not in ('manager', 'worker', 'hybrid'):
            raise ValueError('集群角色必须是 manager、worker 或 hybrid。')
        self.store, self.role = store, role
        path = store.directory / 'instance.json'
        try:
            with path.open('x', encoding='utf-8') as output:
                json.dump({'id': uuid.uuid4().hex, 'token': secrets.token_urlsafe(40)}, output)
        except FileExistsError:
            pass
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.id = identity or os.environ.get('VIDEO_INSTANCE_ID') or saved['id']
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
        return self.role in MANAGEMENT_ROLES

    @property
    def can_download(self):
        return self.role in DOWNLOAD_ROLES

    def untargeted_actions(self):
        return (('download',) if self.can_download else ()) + (('import',) if self.can_manage else ())

    def clock(self, db):
        return database_time(db, self.store.db.mysql)

    def initialize(self, config, base, clash_directory):
        with self.store.connect() as db:
            self.store.db.ensure(db, settings, {'key': 'fleet_budget', 'value': '32'})
            db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget').with_for_update()).scalar_one()
            mode = db.execute(select(settings.c.value).where(settings.c.key == 'fleet_enabled')).scalar_one_or_none()
            if mode and mode.startswith('standalone:'):
                raise ValueError('此数据库已切为单机模式，不能再启动集群实例连接此任务队列。')
            self.store.db.ensure(db, settings, {'key': 'fleet_enabled', 'value': 'true'})
            initial = {'version': 1, 'config': {k: config[k] for k in POLICY_FIELDS if k in config}}
            if self.can_manage:
                self.store.db.ensure(db, settings, {'key': 'fleet_policy', 'value': json.dumps(initial)})
            previous = db.execute(select(instances).where(instances.c.id == self.id)).mappings().first()
            stamp = self.clock(db)
            if previous and previous['heartbeat'] > stamp - ONLINE_SECONDS:
                import psutil
                if previous['host'] != socket.gethostname() or psutil.pid_exists(previous['pid']):
                    raise InstanceIdentityConflict(self.id, previous['host'], previous['heartbeat'] + ONLINE_SECONDS - stamp)
            values = {'id': self.id, 'name': self.name[:100], 'role': self.role, 'host': socket.gethostname(),
                      'pid': os.getpid(), 'boot_id': self.boot_id, 'heartbeat': self.clock(db),
                      'url': self.url, 'token': self.token, 'config': '{}', 'limits': '{}',
                      'paused': int(self.can_download), 'stop_requested': 0, 'error': ''}
            updates = {key: values[key] for key in values if key not in ('id', 'limits', 'paused', 'stop_requested', 'name')}
            if previous and previous['role'] not in DOWNLOAD_ROLES and self.can_download:
                updates.update(paused=1, stop_requested=0)
            db.execute(self.store.db.upsert(instances, values, ['id'], updates))
        self.save_local(config, base, clash_directory)
        runtime = self.store.directory / 'fleet-runtime.json'
        temporary = runtime.with_suffix('.tmp')
        temporary.write_text(json.dumps({'role': self.role, 'id': self.id, 'boot_id': self.boot_id}), encoding='utf-8')
        temporary.replace(runtime)

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
            db.execute(instances.update().where(instances.c.id == self.id, instances.c.boot_id == self.boot_id)
                       .values(config=json.dumps(self.local, ensure_ascii=False)))

    def local_settings(self, db=None):
        row = self.row(db)
        local = json.loads(row['config'])
        local['config'].update(json.loads(row['limits']))
        return local

    def policy(self, *, active=False, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.policy(active=active, db=connection)
        value = db.execute(select(settings.c.value).where(settings.c.key == ('fleet_active_policy' if active else 'fleet_policy'))).scalar_one_or_none()
        return json.loads(value) if value else None

    def save_policy(self, config):
        with self.store.connect() as db:
            old = db.execute(select(settings.c.value).where(settings.c.key == 'fleet_policy').with_for_update()).scalar_one()
            previous = json.loads(old)
            value = {'version': previous['version'] + 1,
                     'config': {key: config[key] for key in POLICY_FIELDS if key in config}}
            db.execute(settings.update().where(settings.c.key == 'fleet_policy').values(value=json.dumps(value)))
        return value

    def activate(self, db, expected_version=None):
        raw = db.execute(select(settings.c.value).where(settings.c.key == 'fleet_policy').with_for_update()).scalar_one()
        value = json.loads(raw)
        if expected_version is not None and value['version'] != expected_version:
            raise ValueError('检查期间下载策略发生变化，请重新开始。')
        db.execute(self.store.db.upsert(settings, {'key': 'fleet_active_policy', 'value': json.dumps(value)}, ['key']))

    def execution(self, payload, db):
        local = self.local_settings(db)
        policy = self.policy(active=True, db=db)
        if not policy:
            raise ValueError('尚未从管理后台开始下载。')
        result = {**payload, 'base': local['base'], 'config': {**local['config'], **policy['config']},
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
                or row['heartbeat'] < stamp - ONLINE_SECONDS or not self.policy(active=True, db=db)):
            return False
        active = tasks.c.status == 'running'
        valid = tasks.c.lease_until > stamp
        global_count = db.execute(select(func.count()).select_from(tasks).where(active, valid, tasks.c.action == 'download')).scalar_one()
        local_count = db.execute(select(func.count()).select_from(tasks).where(active, valid, tasks.c.action == 'download', tasks.c.instance_id == self.id)).scalar_one()
        limit = int(self.local_settings(db)['config'].get('worker_count', 1))
        return global_count < int(budget) and local_count < limit

    def beat(self, error=None):
        with self.store.connect() as db:
            values = {'heartbeat': self.clock(db)}
            if error is not None:
                values['error'] = str(error)[:500]
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
            for identity, value in db.execute(select(tasks.c.instance_id, tasks.c.progress).where(
                    tasks.c.action == 'download', tasks.c.status == 'running')):
                if value:
                    by_instance.setdefault(identity, []).append(json.loads(value))
            totals = {}
            for scope, status, value in db.execute(select(counters).where(counters.c.scope.startswith('instance:'))):
                totals.setdefault(scope.removeprefix('instance:'), {})[status] = int(value)
            result = []
            for row in rows:
                local = json.loads(row['config'])
                config = {**local.get('config', {}), **json.loads(row['limits'])}
                progress = by_instance.get(row['id'], [])
                result.append({k: row[k] for k in ('id', 'name', 'role', 'host', 'url', 'heartbeat', 'paused', 'stop_requested', 'error', 'boot_id')} | {
                    'online': row['heartbeat'] > stamp - ONLINE_SECONDS, 'running': counts.get(row['id'], 0),
                    'transferring': sum(p.get('stage') == 'downloading' for p in progress),
                    'speed': sum(p.get('speed') or 0 for p in progress if p.get('stage') == 'downloading'),
                    'worker_count': config.get('worker_count', 1), 'yt5s_browser_count': config.get('yt5s_browser_count', 2),
                    'directory': local.get('directory', ''),
                    'min_free_gb': config.get('min_free_gb', 2), 'statistics': totals.get(row['id'], {})})
            budget = int(db.execute(select(settings.c.value).where(settings.c.key == 'fleet_budget')).scalar_one())
        return {'items': result, 'global_limit': budget, 'role': self.role, 'instance_id': self.id}

    def control(self, identity, data):
        with self.store.connect() as db:
            row = db.execute(select(instances).where(instances.c.id == identity).with_for_update()).mappings().first()
            if not row or row['role'] not in DOWNLOAD_ROLES:
                raise ValueError('下载实例不存在。')
            values = {}
            if 'name' in data:
                if not isinstance(data['name'], str) or not data['name'].strip() or len(data['name']) > 100:
                    raise ValueError('实例名称需要 1 到 100 个字符。')
                values['name'] = data['name'].strip()
            limits = json.loads(row['limits'])
            for key, bounds in {'worker_count': (1, 16), 'yt5s_browser_count': (1, 16), 'min_free_gb': (0, 10000)}.items():
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
            if not row or row['heartbeat'] < self.clock(db) - ONLINE_SECONDS or not row['url']:
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
