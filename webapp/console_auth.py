"""People use signed sessions; node credentials only access local executor APIs."""
from datetime import timedelta
import hashlib
import ipaddress
import os
import re
import secrets
import threading
import time

from flask import g, jsonify, redirect, render_template, request, session
from sqlalchemy import func, select
from werkzeug.exceptions import Conflict, Forbidden, Unauthorized, TooManyRequests
from werkzeug.security import check_password_hash, generate_password_hash

from webapp.database import console_users, settings

ROLES = {'admin': '管理员', 'operator': '操作员', 'viewer': '只读'}
_DUMMY_HASH = generate_password_hash('not-a-login-password')


def local_request():
    # Never trust an unconfigured X-Forwarded-For header.
    try:
        return ipaddress.ip_address(request.remote_addr or '').is_loopback
    except ValueError:
        return False


def rpc_allowed():
    path = request.path
    if path == '/api/tasks' and request.method == 'POST':
        data = request.get_json(silent=True)
        return isinstance(data, dict) and data.get('action') == 'check'
    if re.fullmatch(r'/api/tasks/[a-f0-9]{32}/logs', path):
        return request.method in ('GET', 'HEAD')
    if path == '/api/disk/logs':
        return request.method in ('GET', 'HEAD')
    return (path in ('/health', '/api/config', '/api/status', '/api/fleet/preflight', '/api/downloads/connection', '/api/disk', '/api/disk/cleanup')
            or path.startswith(('/api/media', '/media/', '/api/clash', '/api/youtube')))


class ConsoleAuth:
    def __init__(self, app, store):
        self.store, self.failures, self.lock = store, {}, threading.Lock()
        self.key_file = store.directory / 'console-bootstrap.key'
        try:
            with self.key_file.open('x', encoding='utf-8') as output:
                os.chmod(self.key_file, 0o600)
                output.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
        self.bootstrap_key = self.key_file.read_text(encoding='utf-8').strip()
        with store.connect() as db:
            store.db.ensure(db, settings, dict(key='console_auth_lock', value=''))
        app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
        # Cookies ignore TCP ports. Distinct nodes on one host must not replace
        # one another's signed sessions when both management pages are open.
        app.config['SESSION_COOKIE_NAME'] = 'frameflow_' + hashlib.sha256(
            str(store.directory.resolve()).encode('utf-8')).hexdigest()[:16]
        app.config['SESSION_COOKIE_SECURE'] = os.environ.get('VIDEO_WEB_HTTPS') == '1'
        self.register(app)

    def identity(self):
        with self.store.connect() as db:
            configured = bool(db.execute(select(console_users.c.username).limit(1)).first())
            row = db.execute(select(console_users).where(console_users.c.username == session.get('console_username', ''),
                console_users.c.enabled == 1)).mappings().first() if session.get('console_username') else None
        user = dict(username=row['username'], role=row['role']) if row and row['revision'] == session.get('console_revision') else None
        return configured, user

    def guard(self):
        g.console_user = None
        g.console_local_access = False
        if request.path.startswith('/static/') or request.path == '/health':
            return None
        configured, user = self.identity()
        g.console_user = user
        g.console_local_access = not configured and local_request()
        if request.path == '/login' or request.path in ('/api/auth/status', '/api/auth/login', '/api/auth/setup'):
            return None
        if not user and not g.console_local_access:
            if request.path.startswith('/api/') or request.path.startswith('/media/'):
                return jsonify(error='请先登录管理账户。', login_url='/login'), 401
            return redirect('/login')
        if request.path.startswith('/api/auth/users') and (not user or user['role'] != 'admin'):
            raise Forbidden('仅管理员可以管理账户。')
        if user and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.path == '/api/auth/logout':
                return None
            if user['role'] == 'viewer':
                raise Forbidden('当前账户为只读，不能执行修改或下载操作。')
            if user['role'] == 'operator' and not (
                    request.path.startswith('/api/jobs') or request.path in ('/api/downloads/stop', '/api/downloads/resume')
                    or request.path.startswith('/api/statistics/sessions/')):
                raise Forbidden('此设置需要管理员权限。')
        return None

    @staticmethod
    def credentials(data):
        name, password = data.get('username'), data.get('password')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', name):
            raise ValueError('用户名使用 3 到 64 位字母、数字、点、短横线或下划线。')
        if not isinstance(password, str) or not 10 <= len(password) <= 256:
            raise ValueError('密码需要 10 到 256 个字符。')
        return name, password

    def throttle(self):
        key = request.remote_addr or 'unknown'
        now = time.monotonic()
        with self.lock:
            self.failures = {k: v for k, v in self.failures.items() if now-v[0] < 60}
            since, count = self.failures.get(key, (now, 0))
            if count >= 8 or key not in self.failures and len(self.failures) >= 2048:
                raise TooManyRequests('尝试过于频繁，请稍后重试。')
            self.failures[key] = (since, count+1)

    def login(self, row):
        session.clear()
        session.update(console_username=row['username'], console_revision=row['revision'], csrf=secrets.token_urlsafe(32))
        session.permanent = True

    def register(self, app):
        def body():
            request.max_content_length = 16384
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise ValueError('请提交 JSON 对象。')
            return data

        def lock_accounts(db):
            if self.store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            db.execute(select(settings.c.key).where(settings.c.key == 'console_auth_lock').with_for_update()).first()

        @app.context_processor
        def identity_context():
            return dict(console_user=getattr(g, 'console_user', None), console_roles=ROLES,
                console_local_access=getattr(g, 'console_local_access', False))

        @app.get('/login')
        def console_login_page():
            configured, user = self.identity()
            return render_template('console_login.html', csrf=session['csrf'], configured=configured,
                current_user=user, bootstrap_path=str(self.key_file))

        @app.get('/api/auth/status')
        def console_status():
            configured, user = self.identity()
            return jsonify(configured=configured, user=user, local_access=not configured and local_request())

        @app.post('/api/auth/setup')
        def console_setup():
            self.throttle()
            data = body()
            if not secrets.compare_digest(str(data.get('bootstrap_key', '')), self.bootstrap_key):
                raise Forbidden('初始设置码不正确，请从运行管理端的状态目录读取。')
            name, password = self.credentials(data)
            password_hash = generate_password_hash(password)
            with self.store.connect() as db:
                lock_accounts(db)
                if db.execute(select(console_users.c.username).limit(1)).first():
                    raise Conflict('管理账户已初始化，请直接登录。')
                db.execute(console_users.insert().values(username=name, password_hash=password_hash,
                    role='admin', revision=1, enabled=1))
            self.login(dict(username=name, revision=1))
            return jsonify(message='管理员已创建。', redirect='/jobs'), 201

        @app.post('/api/auth/login')
        def console_login():
            self.throttle()
            data = body()
            with self.store.connect() as db:
                row = db.execute(select(console_users).where(console_users.c.username == str(data.get('username', ''))[:64])).mappings().first()
            password = data.get('password')
            if not isinstance(password, str) or len(password) > 256:
                raise Unauthorized('用户名或密码不正确。')
            valid = check_password_hash(row['password_hash'] if row else _DUMMY_HASH, password)
            if not valid or not row or not row['enabled']:
                raise Unauthorized('用户名或密码不正确。')
            self.login(row)
            with self.lock:
                self.failures.pop(request.remote_addr or 'unknown', None)
            return jsonify(message='登录成功。', redirect='/jobs')

        @app.post('/api/auth/logout')
        def console_logout():
            session.clear()
            return jsonify(redirect='/login')

        @app.get('/api/auth/users')
        def console_accounts():
            with self.store.connect() as db:
                return jsonify(items=[dict(row) for row in db.execute(select(console_users.c.username,
                    console_users.c.role, console_users.c.enabled).order_by(console_users.c.username)).mappings()])

        @app.post('/api/auth/users')
        def console_add_account():
            data = body()
            name, password = self.credentials(data)
            role = data.get('role', 'viewer')
            if not isinstance(role, str) or role not in ROLES:
                raise ValueError('请选择有效账户角色。')
            password_hash = generate_password_hash(password)
            with self.store.connect() as db:
                lock_accounts(db)
                if db.execute(select(console_users.c.username).where(console_users.c.username == name)).first():
                    raise Conflict('用户名已存在。')
                db.execute(console_users.insert().values(username=name, password_hash=password_hash, role=role, enabled=1, revision=1))
            return jsonify(message='账户已创建。'), 201

        @app.put('/api/auth/users/<username>')
        def console_change_account(username):
            data = body()
            values = {}
            if 'password' in data:
                _, password = self.credentials(dict(username=username, password=data['password']))
                values['password_hash'] = generate_password_hash(password)
            if 'role' in data:
                if not isinstance(data['role'], str) or data['role'] not in ROLES:
                    raise ValueError('请选择有效账户角色。')
                values['role'] = data['role']
            if 'enabled' in data:
                if type(data['enabled']) is not bool:
                    raise ValueError('启用标记必须为布尔值。')
                values['enabled'] = int(data['enabled'])
            with self.store.connect() as db:
                lock_accounts(db)
                row = db.execute(select(console_users).where(console_users.c.username == username)).mappings().first()
                if not row:
                    raise ValueError('账户不存在。')
                if row['role'] == 'admin' and row['enabled'] and (values.get('role', row['role']) != 'admin' or not values.get('enabled', row['enabled'])):
                    admins = db.execute(select(func.count()).select_from(console_users).where(console_users.c.role == 'admin', console_users.c.enabled == 1)).scalar_one()
                    if admins <= 1:
                        raise Conflict('必须保留至少一个启用的管理员。')
                db.execute(console_users.update().where(console_users.c.username == username).values(**values, revision=row['revision']+1))
            return jsonify(message='账户已更新，该账户的已有登录会话已失效。')
