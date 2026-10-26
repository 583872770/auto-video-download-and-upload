"""A database-independent setup UI in front of the configured application."""
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template, request, session
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool
from werkzeug.exceptions import HTTPException

from Utils.runtime import ROOT
from webapp.deployment_errors import ClusterRoleConflict, explain_startup_error
from webapp.fleet import InstanceIdentityConflict
from webapp.modes import check_conversion, single_state

ROLES = {'hybrid', 'manager', 'worker', 'all'}


def mysql_url(profile):
    return URL.create('mysql+pymysql', host=profile['mysql_host'], port=profile['mysql_port'],
                      username=profile['mysql_user'], password=profile['mysql_password'],
                      database=profile['mysql_database'], query={'charset': 'utf8mb4'}).render_as_string(hide_password=False)


def validate_profile(data, previous):
    if not isinstance(data, dict):
        raise ValueError('请提交有效的部署配置。')
    allowed = {'role', 'instance_id', 'instance_name', 'instance_url', 'mysql_host', 'mysql_port',
               'mysql_database', 'mysql_user', 'mysql_password', 'web_hosts'}
    if set(data) - allowed:
        raise ValueError('包含不支持的部署配置项。')
    value = {**previous, **data}
    for key in allowed - {'mysql_port'}:
        if not isinstance(value.get(key, ''), str) or len(value.get(key, '')) > 4096 or '\0' in value.get(key, ''):
            raise ValueError('配置项必须为有效文本。')
        if key != 'mysql_password':
            value[key] = value.get(key, '').strip()
    if not data.get('mysql_password'):
        value['mysql_password'] = previous.get('mysql_password', '')
    if value.get('role') not in ROLES:
        raise ValueError('请选择管理与下载一体、纯管理端、纯下载实例或单机模式。')
    port = value.get('mysql_port', 3306)
    if isinstance(port, str) and port.isascii() and port.isdigit():
        port = int(port)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('MySQL 端口必须在 1 到 65535 之间。')
    value['mysql_port'] = port
    for key in ('mysql_host', 'mysql_database', 'mysql_user', 'mysql_password'):
        if not value.get(key):
            raise ValueError('请填写完整的 MySQL 地址、库名、用户名和密码。')
    if any(char in value['mysql_host'] for char in '/?#@ \t\r\n'):
        raise ValueError('MySQL 地址只填写域名或 IP，不带协议和端口。')
    if value.get('instance_id') and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['instance_id']):
        raise ValueError('实例 ID 只能包含 1 到 64 位字母、数字、下划线或短横线。')
    value['instance_name'] = value.get('instance_name', '')[:100] or socket.gethostname()
    address = value.get('instance_url', '').rstrip('/')
    if address:
        try:
            parsed = urlsplit(address)
            valid = (parsed.scheme in ('http', 'https') and parsed.hostname not in (None, '', '0.0.0.0', '::') and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment
                     and parsed.path in ('', '/') and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('实例地址请填写完整 HTTP(S) 地址，例如 http://10.0.0.12:5000。')
    if value['role'] in ('worker', 'hybrid') and not address:
        raise ValueError('请填写各管理页面能够访问的本机实例地址。')
    value['instance_url'] = address
    if not value.get('web_hosts'):
        raise ValueError('请填写页面访问域名或 IP，多个用逗号分隔。')
    for host in value['web_hosts'].split(','):
        if not host.strip() or any(char in host for char in '/?#@ \t\r\n') or len(host) > 253:
            raise ValueError('允许访问的主机只填写域名或 IP，多个用逗号分隔，不带协议或端口。')
    return {key: value.get(key, '') for key in allowed}


def test_mysql(profile):
    engine = None
    try:
        engine = create_engine(mysql_url(profile), poolclass=NullPool, hide_parameters=True,
                               connect_args={'connect_timeout': 5, 'read_timeout': 5, 'write_timeout': 5})
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
            supported = not engine.dialect.is_mariadb and engine.dialect.server_version_info >= (5, 7, 9)
    except Exception:
        raise ValueError('MySQL 连接失败，请检查地址、端口、库名、账号密码和数据库访问权限。') from None
    finally:
        if engine is not None:
            engine.dispose()
    if not supported:
        raise ValueError('请使用 MySQL 5.7.9 或更新版本，推荐 MySQL 8.4。')


def write_profile(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.deployment-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            json.dump(value, output, ensure_ascii=False)
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


class DeploymentGateway:
    def __init__(self, config_path, state_dir, *, env_file=None, role=None, instance_id=None,
                 instance_url='', instance_name=None, load_environment=True, app_factory=None):
        self.config_path, self.directory = Path(config_path).resolve(), Path(state_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'deployment.json'
        self.lock = threading.Lock()
        self.active = None
        self.running_role = None
        self.pending_restart = False
        self.startup_error = ''
        self.failure = None
        self.starting = False
        self.startup_stage = ''
        self.stage_started = time.monotonic()
        self.initialization = None
        self.auto_start = False
        self.desktop_started = False
        self.app_factory = app_factory
        key = self.directory / 'deployment.key'
        try:
            with key.open('x', encoding='utf-8') as output:
                os.chmod(key, 0o600)
                output.write(secrets.token_urlsafe(18))
        except FileExistsError:
            pass
        self.access_code = key.read_text(encoding='utf-8').strip()
        if load_environment:
            from webapp.bootstrap import configure_database_environment
            try:
                configure_database_environment(env_file)
            except ValueError:
                if env_file is not None or os.environ.get('MYSQL_HOST') or os.environ.get('MYSQL_PASSWORD'):
                    self.startup_error = '原启动配置不完整，请在页面补齐后保存。'
        self.profile = self.environment_profile()
        invalid_saved = False
        if self.path.is_file():
            try:
                self.profile = validate_profile(json.loads(self.path.read_text(encoding='utf-8')), self.profile)
            except (ValueError, OSError):
                self.startup_error = '已保存的部署配置无法读取，请重新配置。'
                invalid_saved = True
        for key, value in {'role': role, 'instance_id': instance_id, 'instance_url': instance_url,
                           'instance_name': instance_name}.items():
            if value:
                self.profile[key] = value
        # Older deployments have a fleet identity but no deployment.json yet.
        # Preserve that role instead of applying the new wizard's default.
        if not self.path.exists() and not role and not os.environ.get('VIDEO_SERVICE_ROLE'):
            try:
                state = self.cluster_state()
                if state:
                    self.profile['role'] = state['role']
            except (OSError, ValueError):
                self.startup_error = '本机集群身份记录无法读取，请保留状态目录并检查 fleet-runtime.json。'
                invalid_saved = True
        if not invalid_saved and all(self.profile.get(key) for key in ('mysql_host', 'mysql_database', 'mysql_user', 'mysql_password')):
            try:
                if (self.path.is_file() and not role and not instance_id and self.profile['role'] == 'all'
                        and not single_state(self.directory)):
                    self.recover_cluster_profile()
                self.profile = validate_profile(self.profile, self.profile)
                self.resolve_identity(self.profile)
                self.auto_start = True
            except ClusterRoleConflict as error:
                self.startup_stage = '正在检查本机实例配置…'
                self.record_failure(error)
            except OSError as error:
                self.startup_stage = '正在保存恢复后的部署配置…'
                self.record_failure(error)
            except Exception:
                # Do not expose database credentials from exception messages.
                self.startup_error = '服务启动未完成，请检查部署配置、数据库连接和实例 ID 是否重复。'
        self.setup = self.create_setup_app()

    def report_startup(self, message):
        self.startup_stage = message
        self.stage_started = time.monotonic()
        print(f'[服务启动] {message}', flush=True)

    def record_failure(self, error, prefix=''):
        self.failure = explain_startup_error(error, self.startup_stage, self.profile)
        self.startup_error = prefix + self.failure['summary'] + ' ' + self.failure['remedy']
        self.report_startup(self.startup_error)
        print('[服务启动详情] ' + json.dumps(self.failure, ensure_ascii=False), flush=True)
        try:
            write_profile(self.directory / 'deployment-error.json', self.failure)
        except OSError:
            pass

    def start(self):
        """Start only after the HTTP listener is bound; never block the setup UI."""
        if not self.auto_start or self.active is not None or not self.lock.acquire(blocking=False):
            return
        self.begin_initialization()

    def begin_initialization(self, prefix=''):
        """The caller transfers its held configuration lock to this thread."""
        self.auto_start = False
        self.starting = True
        self.startup_error = ''
        self.failure = None

        def initialize():
            try:
                profile = dict(self.profile)
                try:
                    self.activate(profile)
                except InstanceIdentityConflict as error:
                    self.wait_for_identity_lease(error)
                    self.activate(profile)
                self.startup_error = ''
                self.failure = None
            except Exception as error:
                self.record_failure(error, prefix)
            finally:
                self.starting = False
                self.lock.release()

        self.initialization = threading.Thread(target=initialize, name='deployment-startup', daemon=True)
        try:
            self.initialization.start()
        except Exception:
            self.starting = False
            self.lock.release()
            raise

    def wait_for_identity_lease(self, error):
        # Container PID/hostname reuse cannot prove that the previous owner is alive.
        # Wait outside the transaction, then let Fleet check the latest heartbeat.
        delay = error.retry_after + 1
        self.report_startup(f'实例 ID {error.identity} 的旧心跳尚未过期，正在等待约 {delay:.0f} 秒后自动重试；'
                            f'登记主机：{error.host}。若旧服务仍在线，将保持冲突保护。')
        time.sleep(delay)

    @staticmethod
    def environment_profile():
        value = {'role': os.environ.get('VIDEO_SERVICE_ROLE') or 'hybrid',
                 'instance_id': os.environ.get('VIDEO_INSTANCE_ID', ''),
                 'instance_name': os.environ.get('VIDEO_INSTANCE_NAME') or socket.gethostname(),
                 'instance_url': os.environ.get('VIDEO_INSTANCE_URL', ''),
                 'mysql_host': os.environ.get('MYSQL_HOST', ''), 'mysql_port': os.environ.get('MYSQL_PORT') or 3306,
                 'mysql_database': os.environ.get('MYSQL_DATABASE') or 'video_downloads',
                 'mysql_user': os.environ.get('MYSQL_USER') or 'video', 'mysql_password': os.environ.get('MYSQL_PASSWORD', ''),
                 'web_hosts': os.environ.get('VIDEO_WEB_HOSTS') or f'localhost,127.0.0.1,[::1],{socket.gethostname()}'}
        raw = os.environ.get('VIDEO_DATABASE_URL')
        if raw:
            try:
                url = make_url(raw)
                if url.drivername in ('mysql', 'mysql+pymysql'):
                    value.update(mysql_host=url.host or '', mysql_port=url.port or 3306,
                                 mysql_database=url.database or '', mysql_user=url.username or '', mysql_password=url.password or '')
            except Exception:
                pass
        return value

    def cluster_state(self):
        try:
            saved = json.loads((self.directory / 'fleet-runtime.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError):
            raise ValueError('本机集群身份记录无效，请保留状态目录并检查 fleet-runtime.json。') from None
        if (not isinstance(saved, dict) or saved.get('role') not in ('manager', 'worker', 'hybrid')
                or not isinstance(saved.get('id'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', saved['id'])):
            raise ValueError('本机集群身份记录无效，请保留状态目录并检查 fleet-runtime.json。')
        return {'role': saved['role'], 'instance_id': saved['id']}

    def resolve_identity(self, profile, *, allow_transition=False):
        state = self.cluster_state()
        single = single_state(self.directory)
        permitted = allow_transition or (single and state and single['instance_id'] == state['instance_id'])
        if state and profile['role'] == 'all' and not permitted:
            raise ClusterRoleConflict(state['role'], state['instance_id'])
        if profile['role'] != 'all' and not profile['instance_id']:
            if state:
                profile['instance_id'] = state['instance_id']
                return
            try:
                saved = json.loads((self.directory / 'instance.json').read_text(encoding='utf-8'))
                identity = saved['id']
                if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identity):
                    raise ValueError('Invalid saved identity')
            except FileNotFoundError:
                identity = uuid.uuid4().hex
            profile['instance_id'] = identity

    def recover_cluster_profile(self):
        state = self.cluster_state()
        if not state:
            return
        candidate = {**self.profile, 'role': state['role'], 'instance_id': state['instance_id']}
        try:
            candidate = validate_profile(candidate, self.profile)
        except ValueError:
            # A worker without a reachable address still needs the setup form.
            raise ClusterRoleConflict(state['role'], state['instance_id']) from None
        write_profile(self.path, candidate)
        self.profile = candidate
        self.startup_error = ''
        label = {'manager': '管理后台', 'worker': '下载实例', 'hybrid': '管理与下载一体'}[state['role']]
        self.report_startup(f'已自动恢复原集群配置：{label}，实例 ID {state["instance_id"]}，正在继续启动…')

    def activate(self, profile):
        # Reject incompatible local identities before starting any processes or DDL.
        self.resolve_identity(profile)
        if os.environ.get('YT5S_BROWSER_ENABLED') == '1' and profile['role'] != 'manager' and not self.desktop_started:
            self.report_startup('正在启动浏览器桌面环境…')
            spec = importlib.util.spec_from_file_location('frameflow_desktop', ROOT / 'docker/browser_desktop.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.start_desktop()
            self.desktop_started = True
        if self.app_factory is None:
            from app import create_app
            factory = create_app
        else:
            factory = self.app_factory
        self.report_startup('正在连接 MySQL 并检查表结构；首次升级可能需要较长时间…')
        app = factory(self.config_path, self.directory, role=profile['role'], instance_id=profile['instance_id'] or None,
                      instance_url=profile['instance_url'], instance_name=profile['instance_name'], database_url=mysql_url(profile),
                      startup_report=self.report_startup)
        app.config['TRUSTED_HOSTS'] = None if profile['role'] == 'worker' else [item.strip() for item in profile['web_hosts'].split(',')]
        app.config['DEPLOYMENT_SETUP_AVAILABLE'] = True
        self.active = app
        self.running_role = profile['role']
        self.report_startup('初始化完成，服务已就绪。')

    def startup_status(self):
        return {'starting': self.starting, 'stage': self.startup_stage,
                'stage_elapsed': round(max(0, time.monotonic() - self.stage_started), 1) if self.starting else 0}

    def create_setup_app(self):
        app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'),
                    static_url_path='/setup-static')
        app.json.ensure_ascii = False
        app.config.update(SECRET_KEY=self.access_code, SESSION_COOKIE_NAME='deployment_session',
                          SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict', MAX_CONTENT_LENGTH=16384)
        attempts = {}

        @app.before_request
        def guard():
            if 'csrf' not in session:
                session['csrf'] = secrets.token_urlsafe(32)
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                if not secrets.compare_digest(request.headers.get('X-CSRF-Token', '').encode('utf-8'), session['csrf'].encode('utf-8')):
                    return jsonify(error='页面会话已失效，请刷新后重试。'), 403
            if request.path.startswith('/api/deployment') and request.path != '/api/deployment/unlock' and not session.get('deployment_admin'):
                return jsonify(error='请先输入本机部署设置访问码。'), 403

        @app.after_request
        def headers(response):
            response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY',
                'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
            return response

        @app.errorhandler(ValueError)
        def invalid(error):
            return jsonify(error=str(error)), 400

        @app.errorhandler(HTTPException)
        def http_error(error):
            return jsonify(error=error.description), error.code

        @app.get('/setup')
        def page():
            return render_template('deployment.html', csrf=session['csrf'], unlocked=bool(session.get('deployment_admin')))

        @app.post('/api/deployment/unlock')
        def unlock():
            address = request.remote_addr or ''
            stamp = time.monotonic()
            recent = [value for value in attempts.get(address, []) if stamp - value < 60]
            if len(recent) >= 8:
                return jsonify(error='访问码尝试次数过多，请一分钟后重试。'), 429
            data = request.get_json(silent=True)
            code = data.get('code', '') if isinstance(data, dict) else ''
            if not isinstance(code, str) or not secrets.compare_digest(code.encode('utf-8'), self.access_code.encode('utf-8')):
                attempts[address] = [*recent, stamp]
                return jsonify(error='访问码不正确，请查看本机启动日志中的“部署设置访问码”。'), 403
            attempts.pop(address, None)
            session['deployment_admin'] = True
            return jsonify(message='已解锁部署设置。')

        @app.get('/api/deployment')
        def configuration():
            public = {key: value for key, value in self.profile.items() if key != 'mysql_password'}
            return jsonify(config=public, password_saved=bool(self.profile.get('mysql_password')),
                           cluster_state=self.cluster_state(),
                           single_conversion=(single_state(self.directory) or {}).get('status'),
                           running=self.active is not None, running_role=self.running_role,
                           pending_restart=self.pending_restart, error=self.startup_error,
                           diagnostic=self.failure, **self.startup_status())

        @app.post('/api/deployment/test')
        def test():
            if self.starting:
                return jsonify(error='服务正在初始化，请稍后再测试连接。'), 409
            data = request.get_json(silent=True)
            if isinstance(data, dict):
                data.pop('single_transition', None)
            value = validate_profile(data, self.profile)
            test_mysql(value)
            return jsonify(message='MySQL 连接成功。')

        @app.post('/api/deployment/save')
        def save():
            if not self.lock.acquire(blocking=False):
                return jsonify(error='服务正在初始化或配置正在保存，请稍后再试。'), 409
            transferred = False
            request_created = False
            profile_saved = False
            try:
                data = request.get_json(silent=True)
                if not isinstance(data, dict):
                    raise ValueError('请提交有效的部署配置。')
                transition = data.pop('single_transition', False)
                if type(transition) is not bool:
                    raise ValueError('请确认是否切换单机模式。')
                value = validate_profile(data, self.profile)
                state = self.cluster_state()
                converting = bool(state and value['role'] == 'all')
                self.resolve_identity(value, allow_transition=transition and converting)
                if converting:
                    if self.profile.get('mysql_host') and any(value[key] != self.profile[key] for key in ('mysql_host', 'mysql_port', 'mysql_database')):
                        raise ValueError('切换单机模式时请保留原 MySQL 地址、端口和库名。')
                    if self.active and self.active.extensions.get('task_store'):
                        check_conversion(self.active.extensions['task_store'], state['instance_id'])
                    value['instance_id'] = state['instance_id']
                test_mysql(value)
                was_running = self.active is not None
                if converting and not single_state(self.directory):
                    write_profile(self.directory / 'single-mode.json', {
                        'instance_id': state['instance_id'], 'owner': uuid.uuid4().hex, 'status': 'requested'})
                    request_created = True
                    if self.active and self.active.extensions.get('task_store'):
                        check_conversion(self.active.extensions['task_store'], state['instance_id'])
                # Persist before starting dispatch threads: a write failure must
                # never leave a service running with unsaved credentials.
                write_profile(self.path, value)
                profile_saved = True
                self.profile = value
                if value['role'] != 'all' and (single_state(self.directory) or {}).get('status') == 'requested':
                    (self.directory / 'single-mode.json').unlink(missing_ok=True)
                if not was_running:
                    transferred = True
                    self.begin_initialization('配置已保存，连接测试已通过。')
                    return jsonify(message='配置已保存，服务正在后台初始化。',
                                   pending_restart=False, starting=True, role=value['role']), 202
                self.pending_restart = was_running
                self.startup_error = ''
                self.failure = None
                return jsonify(message=('单机转换配置已保存，重启本机服务后完成转换；转换后下载保持暂停。' if converting
                               else '配置已保存，重启本机服务后生效。') if was_running else '配置已保存，服务已启动。',
                               pending_restart=was_running, role=value['role'])
            except OSError:
                return jsonify(error='部署配置无法保存，请检查本机状态目录的写入权限。'), 400
            finally:
                try:
                    if request_created and not profile_saved:
                        (self.directory / 'single-mode.json').unlink(missing_ok=True)
                finally:
                    if not transferred:
                        self.lock.release()

        @app.get('/health')
        def health():
            return jsonify(status='starting' if self.starting else 'setup',
                           message=self.startup_stage if self.starting else '等待页面配置')

        @app.route('/', defaults={'path': ''})
        @app.route('/<path:path>')
        def fallback(path):
            return redirect('/setup')

        return app

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '/')
        setup_path = (path == '/setup' or path == '/api/deployment' or
                      path.startswith(('/api/deployment/', '/setup-static/')))
        return (self.setup if setup_path or self.active is None else self.active)(environ, start_response)
