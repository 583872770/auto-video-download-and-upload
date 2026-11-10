"""Flask web console. Run `python app.py`, then open http://127.0.0.1:5000."""
if __name__ == '__main__':
    print('正在加载视频工作台…', flush=True)

import argparse
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid

from flask import Flask, g, has_request_context, jsonify, render_template, request, send_file, session
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import HTTPException

from Utils.config import DEFAULT_CONFIG, load_config, resolve_path, video_directory
from Utils.paths import io_path
from Utils.runtime import ROOT, configure_runtime
from Utils.youtube_login import parse_youtube_cookie_file, save_youtube_cookies
from webapp.media import VIDEO_SUFFIXES
from webapp.settings import DEFAULTS, DOWNLOAD_FIELDS, download_methods, save_settings, urls_from, validate_settings, validate_method_urls
from webapp.config_schema import discard_retired_settings
from webapp.tasks import TaskRunner, TaskStore
from webapp.clash import ClashError
from webapp.clash_routes import register_clash
from webapp.youtube_auth import YouTubeAuthRequired, is_youtube
from webapp.yt5s import BrowserEnvironmentRequired, require_browser_environment
from webapp.environment import DownloadEnvironmentRequired, require_fallback_environment, require_downmp3_environment
from webapp.yt1d import require_environment as require_yt1d_environment
from webapp.service_role import normalize_role

ACTIONS = ('download', 'check')


def create_app(config_path=None, state_dir=None, *, start_worker=True, env_file=None,
               role=None, instance_id=None, instance_url='', instance_name=None, database_url=None,
               startup_report=None):
    configure_runtime()
    if not database_url and (state_dir is None or env_file is not None):
        from webapp.bootstrap import configure_database_environment
        configure_database_environment(env_file)
    service_role = normalize_role(role or os.environ.get('VIDEO_SERVICE_ROLE'))
    app = Flask(__name__)
    from webapp.compression import register_compression
    register_compression(app)
    from webapp.disk_uploads import BudgetRequest
    app.request_class = BudgetRequest
    app.json.ensure_ascii = False
    # Development and local deployments should see edited templates and
    # static JavaScript/CSS immediately after restarting the service.
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
    config_path = Path(config_path or DEFAULT_CONFIG).resolve()
    store = TaskStore(state_dir or ROOT / '.cache' / 'web', database_url or
                      os.environ.get('VIDEO_DATABASE_URL'), startup_report=startup_report)
    # Production (gateway, MySQL, explicit instance configuration) always uses
    # the unified registry. Preserve the embedded SQLite application used by
    # local tools without inventing a second public service role.
    registered = bool(role or os.environ.get('VIDEO_SERVICE_ROLE') or database_url
                      or os.environ.get('VIDEO_DATABASE_URL') or store.fleet or store.single
                      or store.db.mysql or state_dir is None)
    if startup_report:
        startup_report('数据库已就绪，正在初始化素材库与实例配置…')
    secret_file = store.directory / 'session.key'
    try:
        with secret_file.open('xb') as output:
            output.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    app.config.update(
        SERVICE_ROLE=service_role,
        SECRET_KEY=secret_file.read_bytes(), MAX_CONTENT_LENGTH=8 * 1024 ** 3,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        TRUSTED_HOSTS=os.environ.get('VIDEO_WEB_HOSTS', f'localhost,127.0.0.1,[::1],{socket.gethostname()}').split(','),
    )
    app.extensions['task_store'] = store

    # Initialize extensions
    from webapp.extensions import init_extensions
    exts = init_extensions(app, store, config_path, service_role, instance_id, instance_url, instance_name,
                           registered=registered)
    catalog = exts['catalog']
    startup = exts['startup']
    settings_lock = exts['settings_lock']
    media_lock = exts['media_lock']
    clash = exts['clash']
    fleet = exts['fleet']
    youtube_auth = exts['youtube_auth']
    console_auth = exts['console_auth']

    @app.context_processor
    def instance_context():
        return {'fleet_enabled': fleet is not None}
    if startup_report:
        startup_report('实例配置已就绪，正在加载接口与任务调度…')

    def settings():
        with settings_lock:
            config, base = load_config(config_path)
            if fleet:
                if fleet.can_download:
                    config.update(fleet.resource_limits())
            return config, base

    # Store settings getter and helpers in extensions
    app.extensions['settings_func'] = settings
    app.extensions['disk_config'] = lambda: {**DEFAULTS, **settings()[0]}

    # Register helper functions
    from webapp.helpers import get_body as body, sync_runtime as sync_request_runtime

    def sync_runtime(config, base):
        with app.app_context():
            sync_request_runtime(config, base)

    # Register blueprints
    from webapp.routes import register_blueprints
    register_blueprints(app)

    from webapp.disk_budget import DiskPressure

    @app.errorhandler(DiskPressure)
    def disk_pressure(error):
        return jsonify(error=str(error), error_type='disk_wait'), 507

    if not fleet:
        sync_runtime(*settings())

    @app.before_request
    def csrf_protection():
        from webapp.console_auth import rpc_allowed
        g.fleet_rpc = False
        if fleet and fleet.can_download:
            if request.path == '/health' and request.method == 'GET':
                return None
            token = request.headers.get('Authorization', '').removeprefix('Bearer ')
            if token and secrets.compare_digest(token, fleet.token):
                if not rpc_allowed():
                    return jsonify(error='节点凭据不能用于管理客户任务、账单或账户。'), 403
                g.fleet_rpc = True
                return None
            if request.headers.get('Authorization'):
                return jsonify(error='实例认证凭据无效。'), 403
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.headers.get('X-CSRF-Token', '')
            if not secrets.compare_digest(token, session['csrf']):
                return jsonify(error='页面会话已失效，请刷新后重试。'), 403
        return console_auth.guard()

    if fleet:
        from webapp.fleet_routes import register_fleet
        register_fleet(app, fleet)

    @app.after_request
    def response_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
            if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and 200 <= response.status_code < 300:
                runner = app.extensions.get('task_runner')
                if runner:
                    runner.wake()
        elif request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(OSError)
    def filesystem_error(error):
        from webapp.disk_budget import is_disk_full
        if is_disk_full(error):
            return jsonify(error='磁盘空间或配额不足，请等待自动清理或释放空间。', error_type='disk_wait'), 507
        app.logger.exception('文件操作失败')
        return jsonify(error=f'文件操作失败：{error}'), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    from sqlalchemy.exc import SQLAlchemyError

    @app.errorhandler(SQLAlchemyError)
    def database_error(error):
        app.logger.exception('数据库操作失败')
        return jsonify(error='数据库暂时不可用，请检查数据库服务。'), 503

    @app.get('/')
    def index():
        from webapp.cobalt_catalog import catalog
        from webapp.risk_diagnostics import GUIDE
        return render_template('index.html', csrf=session['csrf'], service_role=service_role, cobalt_catalog=catalog(), risk_guide=GUIDE)

    @app.get('/api/config')
    def read_config():
        if fleet and fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            from webapp.fleet_routes import selected_id, rpc
            if selected_id() != fleet.id:
                # Paths and the complete configuration belong to the selected
                # node. Resolving a peer's relative paths against this entry
                # node's base silently describes a different directory.
                return jsonify(rpc(fleet, selected_id(), '/api/config'))
        config, base = settings()
        editable = {key: config.get(key, default) for key, default in DEFAULTS.items()}
        editable['download_methods'] = download_methods(config)
        from Utils.oss_storage import public_oss_config, OSS_SECRETS
        editable = public_oss_config(editable)
        for key in OSS_SECRETS:
            editable[key + '_configured'] = bool(config.get(key) or config.get(key + '_configured'))
        return jsonify(config=editable, config_path=str(config_path), media_directory=str(video_directory(config, base)),
                       instance_id=fleet.id if fleet else None)

    @app.put('/api/config')
    def update_config():
        data = body()
        # A cached older frontend may still submit this retired field.
        discard_retired_settings(data)
        if fleet and fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            from webapp.fleet_routes import selected_id, update_configuration
            if selected_id() != fleet.id:
                return update_configuration(fleet, data)
        if any(key not in DEFAULTS for key in data):
            raise ValueError('包含不支持在页面修改的配置项。')
        with media_lock, settings_lock:
            config, _ = settings()
            if any(key in data and data[key] != config.get(key, DEFAULTS[key])
                   for key in ('path', 'dir', 'storage_mode')) and store.media_clear_busy():
                return jsonify(error='素材库正在清空，完成后再修改保存目录或保存方式。'), 409
            if any(key in data for key in ('proxy', 'proxy_mode', 'youtube_cookie', 'youtube_source_address')):
                require_login_idle()
            if any(key in data and data[key] != config.get(key, DEFAULTS[key]) for key in ('proxy', 'proxy_mode', 'youtube_source_address')):
                if store.download_connection_busy():
                    return jsonify(error='请先停止全部下载并等待当前任务结束，再修改代理连接方式。'), 409
            from Utils.oss_storage import OSS_SECRETS
            # Blank secret fields mean keep the saved credential. Never send
            # credentials back to the browser to populate password controls.
            config.update({k: v for k, v in data.items() if k not in OSS_SECRETS or v})
            if 'download_methods' in data:
                config['download_method'] = download_methods(config)[0]
            elif 'download_method' in data:
                config['download_methods'] = None  # Compatibility with old clients/configuration.
            validate_settings(config, download_only=set(data) <= DOWNLOAD_FIELDS)
            if 'youtube_cookie_pool' in data or 'youtube_cookie' in data:
                from webapp.cookie_profiles import CookieProfiles
                profiles = CookieProfiles(config_path.parent, store)
                for identity in config.get('youtube_cookie_pool', []):
                    profiles.require(identity)
                if 'youtube_cookie' in data and config.get('youtube_cookie'):
                    target = resolve_path(config_path.parent, config['youtube_cookie'])
                    if target.is_file():
                        profiles.restore_selected(config['youtube_cookie'])
            if fleet and fleet.can_download:
                from webapp.fleet import RESOURCE_LIMITS
                limits = {key: config[key] for key in RESOURCE_LIMITS if key in data}
                if limits:
                    fleet.control(fleet.id, limits)
            save_settings(config_path, config)
            sync_runtime(config, config_path.parent)
            if any(key in data for key in ('proxy', 'proxy_mode', 'youtube_cookie', 'youtube_source_address')):
                youtube_auth.invalidate()
        return jsonify(message='配置已保存；下载器、画质等策略从后续领取的视频生效，无需暂停或重启。'
                       '配置仅作用于当前实例，正在执行的视频保持本次配置。')

    @app.get('/api/status')
    def status():
        config, base = settings()
        tools = {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe', 'node', 'deno')}
        tools['playwright'] = importlib.util.find_spec('playwright') is not None
        from webapp.yt5s import browser_executable, display_available
        try:
            tools['yt5s_browser'] = bool(browser_executable(
                str(resolve_path(base, config['browser_executable'])) if config.get('browser_executable') else ''))
        except RuntimeError:
            tools['yt5s_browser'] = False
        tools['yt5s_display'] = display_available()
        youtube_file = resolve_path(base, config['youtube_cookie']) if config.get('youtube_cookie') else None
        youtube = {'saved': bool(youtube_file and youtube_file.is_file() and youtube_file.stat().st_size),
                   'path': str(youtube_file) if youtube_file else None, 'login': youtube_login.status(),
                   'auth': youtube_auth.status(), 'auth_block': youtube_auth.block_reason()}
        queue = store.download_queue()
        from webapp.throughput import runtime_limits
        from webapp.concurrency_diagnostics import diagnose, read_context
        from sqlalchemy import func, select
        from webapp.database import tasks as diagnostic_tasks, settings as diagnostic_settings
        rate = runtime_limits(config, store.directory)
        with store.connect() as db:
            identity = fleet.id if fleet else 'local'
            stamp = fleet.clock(db) if fleet else time.time()
            statement = select(diagnostic_tasks.c.progress).where(
                diagnostic_tasks.c.status == 'running', diagnostic_tasks.c.action == 'download')
            if fleet:
                statement = statement.where(diagnostic_tasks.c.instance_id == identity)
            progress = [json.loads(value or '{}') for value in db.execute(statement).scalars()]
            context = read_context(db, [(identity, config)], stamp)
            budget = db.execute(select(diagnostic_settings.c.value).where(diagnostic_settings.c.key == 'fleet_budget')).scalar_one_or_none()
            global_running = db.execute(select(func.count()).select_from(diagnostic_tasks).where(
                diagnostic_tasks.c.status == 'running', diagnostic_tasks.c.action == 'download')).scalar_one() if int(budget or 0) else 0
            concurrency = diagnose(config, identity, progress, rate, context, stamp,
                global_limit=int(budget or 0), global_running=global_running,
                paused=bool(fleet.row(db)['paused']) if fleet else queue.get('paused', False))
            from webapp.subtitles import summary as subtitle_summary
            concurrency['subtitles'] = subtitle_summary(db, stamp, identity)
            from webapp.risk_diagnostics import observed, local_health
            concurrency['local_health'] = local_health(store.directory, config, exts['clash'].directory)
            concurrency['local_health']['progress_cache'] = store.progress_cache.health()
            concurrency['risks'] = observed(config, concurrency, youtube['auth'], stamp,
                error=fleet.row(db)['error'] if fleet else '')
            concurrency['auth_checked_at'] = youtube['auth'].get('checked_at')
        return jsonify(tools=tools, accounts={}, startup=startup.status(), python=sys.version.split()[0], database=store.db.engine.dialect.name,
                       throughput=rate, concurrency=concurrency,
                       workers=store.workers(), worker_count=config.get('worker_count', 1),
                       yt5s_browser_count=config.get('yt5s_browser_count', 2), download_queue=queue,
                       execution=app.extensions['execution_control'].status(queue),
                       connection_busy=store.download_connection_busy(),
                       youtube=youtube, storage_mode=config.get('storage_mode', 'local'),
                       download_method=config.get('download_method', 'yt_dlp'),
                       download_methods=download_methods(config),
                       media_directory=str(video_directory(config, base)), timezone='服务器本地时间')

    from webapp.youtube_login import YouTubeLogin, LoginBusy, browser_cookie_name

    @app.errorhandler(LoginBusy)
    def login_busy(error):
        return jsonify(error=str(error)), 409

    from webapp.throughput import PipelineBusy

    @app.errorhandler(PipelineBusy)
    def pipeline_busy(error):
        return jsonify(error=str(error), retry_after=getattr(error, 'retry_after', 10)), 409

    @app.errorhandler(YouTubeAuthRequired)
    def youtube_auth_required(error):
        from webapp.download_policy import failure_evidence, recovery_policy
        failure = failure_evidence(error)
        policy = recovery_policy(failure, settings()[0])
        if policy.targets == ('session',):
            return jsonify(error=str(error), auth=error.result, failure=failure.public(),
                           message='仅该账号需要人工处理登录或验证，其他健康账号继续。' if policy.manual else
                                   '仅该账号请求冷却，其他健康账号继续，实例未暂停。'), 409
        youtube_auth.block(error.result)
        return jsonify(error=str(error), auth=error.result, download_queue=store.download_queue()), 409

    @app.errorhandler(BrowserEnvironmentRequired)
    @app.errorhandler(DownloadEnvironmentRequired)
    def browser_environment_required(error):
        return jsonify(error=str(error), download_queue=store.download_queue()), 409

    def verify_youtube(config, base, urls, snapshot=None, *, force=False):
        methods = download_methods(config)
        if 'yt1d' in methods:
            require_yt1d_environment(config, base)
        if 'bergung' in methods:
            from webapp.bergung import require_environment
            require_environment(config, base)
        if 'downmp3' in methods:
            require_downmp3_environment()
        if 'yt5s' in methods and 'yt_dlp' in methods:
            require_fallback_environment(config, base)
        elif 'yt5s' in methods:
            require_browser_environment(config, base)
        if 'yt_dlp' not in methods:
            return None
        if not is_youtube(urls):
            return None
        if config.get('youtube_cookie_pool') and not force:
            # Each admitted account is checked by its worker. The selected
            # legacy/default Cookie may be cooling while other accounts work.
            return None
        # In on-demand mode public videos start anonymously.  The worker will
        # retry once with the selected Cookie only when yt-dlp reports that
        # authentication is required.  Explicit checks from the settings page
        # still call this function with force=True and therefore remain useful.
        if (config.get('youtube_cookie_usage', 'auto') in ('auto', 'never')
                and not config.get('youtube_cookie_pool') and not force):
            return None
        options = {'force': force or len(methods) > 1}
        if config.get('proxy_mode') == 'clash':
            snapshot = snapshot or clash.snapshot()
            options.update(proxy=snapshot['proxy'], node=snapshot['node'])
        return youtube_auth.require(config, base, **options)

    @app.post('/api/youtube/auth/check')
    def check_youtube_auth():
        with media_lock:
            require_login_idle()
            config, base = settings()
            result = verify_youtube({**config, 'download_methods': ['yt_dlp']}, base, ['https://www.youtube.com/'], force=True)
            youtube_auth.clear_block()
        return jsonify(auth=result, message=result['message'])

    def cookie_update_busy():
        return store.download_connection_busy()

    def install_cookie(data):
        cookies = parse_youtube_cookie_file(data)
        with media_lock, settings_lock:
            if cookie_update_busy():
                raise LoginBusy('请先停止全部下载并等待当前任务结束，再更新 Cookie。')
            config, base = settings()
            target = base / 'Cookies' / 'youtube.txt'
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not target.is_file():
                raise ValueError('Cookie 保存位置不是文件，请检查 Cookies/youtube.txt。')
            with tempfile.TemporaryDirectory(prefix='.youtube-import-', dir=target.parent) as staging:
                backup = Path(staging) / 'previous.txt'
                config_backup = Path(staging) / 'config.yaml'
                shutil.copy2(config_path, config_backup)
                if target.is_file():
                    shutil.copy2(target, backup)
                save_youtube_cookies(cookies, target)
                try:
                    config['youtube_cookie'] = 'Cookies/youtube.txt'
                    save_settings(config_path, config)
                    sync_runtime(config, base)
                    from webapp.cookie_profiles import CookieProfiles
                    CookieProfiles(base, store).save_legacy(target.read_bytes())
                    youtube_auth.invalidate()
                except Exception:
                    config_backup.replace(config_path)
                    if backup.is_file():
                        backup.replace(target)
                    else:
                        target.unlink(missing_ok=True)
                    raise

    def install_browser_cookie(data, *, account_name=None):
        with media_lock, settings_lock:
            from webapp.cookie_profiles import CookieProfiles
            config, base = settings()
            profiles = CookieProfiles(base, store)
            item = profiles.add(browser_cookie_name(account_name), data)
            name = item['name']
            if cookie_update_busy():
                return f'YouTube 登录成功，已新增 Cookie「{name}」。当前下载继续使用原 Cookie；停止当前实例后可在列表中选择新 Cookie。'
            original_config = config_path.read_bytes()
            try:
                config['youtube_cookie'] = profiles.require(item['id']).relative_to(base).as_posix()
                save_settings(config_path, config)
                sync_runtime(config, base)
                youtube_auth.invalidate()
            except Exception:
                config_path.write_bytes(original_config)
                profiles.delete(item['id'])
                raise
        return f'YouTube 登录成功，已新增并启用 Cookie「{name}」，后续下载将自动使用。'

    youtube_login = YouTubeLogin(store.directory, install_browser_cookie)
    app.extensions['youtube_login'] = youtube_login

    def require_login_idle():
        if youtube_login.status()['active']:
            raise LoginBusy('请先完成或取消 YouTube 登录，再执行此操作。')

    @app.get('/api/youtube/login')
    def youtube_login_status():
        return jsonify(youtube_login.status())

    @app.post('/api/youtube/login')
    def start_youtube_login():
        with media_lock:
            require_login_idle()
            config, base = settings()
            mode = config.get('proxy_mode', 'manual')
            connection = '直连'
            proxy = ''
            if mode == 'clash':
                snapshot = clash.snapshot()
                proxy = snapshot['proxy']
                connection = 'Clash · ' + snapshot['node']
            elif mode == 'manual' and config.get('proxy'):
                proxy = config['proxy']
                connection = '手动代理'
            executable = str(resolve_path(base, config['browser_executable'])) if config.get('browser_executable') else ''
            status = youtube_login.start({'proxy': proxy, 'browser_executable': executable}, connection)
        return jsonify(status), 202

    @app.post('/api/youtube/login/cancel')
    def cancel_youtube_login():
        return jsonify(youtube_login.cancel())

    @app.post('/api/youtube/cookie')
    def upload_youtube_cookie():
        request.max_content_length = 2 * 1024 * 1024
        uploaded = request.files.get('cookie')
        if uploaded is None or not uploaded.filename or Path(uploaded.filename).suffix.lower() != '.txt':
            raise ValueError('请选择 Netscape 格式的 Cookie .txt 文件。')
        uploaded_data = uploaded.stream.read(1024 * 1024 + 1)
        parse_youtube_cookie_file(uploaded_data)
        with media_lock:
            require_login_idle()
            install_cookie(uploaded_data)
        return jsonify(message='YouTube Cookie 已保存，后续下载将自动使用。', cookie_path='Cookies/youtube.txt'), 201

    @app.get('/api/youtube/cookies')
    def list_youtube_cookies():
        from webapp.cookie_profiles import CookieProfiles
        from webapp.youtube_cookie_history import list_events
        with settings_lock:
            config, base = settings()
            profiles = CookieProfiles(base, store)
            # Repair installations created before first-upload auto-activation:
            # a saved profile with no selected path is safe to make active.
            if not config.get('youtube_cookie'):
                saved = profiles.items()
                from webapp.cookie_binding import CookieBindingConflict
                for item in saved:
                    try:
                        target = profiles.require(item['id'])
                    except CookieBindingConflict:
                        continue  # Keep legacy conflicts visible for removal/export.
                    config['youtube_cookie'] = target.relative_to(base).as_posix()
                    config['youtube_cookie_pool'] = []
                    save_settings(config_path, config)
                    sync_runtime(config, base)
                    youtube_auth.invalidate()
                    break
            items = profiles.items(config.get('youtube_cookie', ''))
            for item in items:
                item['auth'] = youtube_auth.profile_status(item['id'], base)
                item['pooled'] = item['id'] in config.get('youtube_cookie_pool', [])
            pool_status = None
            if config.get('youtube_cookie_pool'):
                from webapp.cookie_pool import CookiePool
                pool_status = CookiePool(store, config, base, None, strict=False).status()
            sessions = {row['id']: row for row in (pool_status or {}).get('session_states', [])}
            for item in items:
                item['session'] = sessions.get(item['id'])
                recent = list_events(store, item['id'], limit=1)
                item['latest_event'] = recent[0] if recent else None
        return jsonify(items=items, pool=pool_status, cookie_path=config.get('youtube_cookie', ''),
                       custom_active=bool(config.get('youtube_cookie') and not any(item['active'] for item in items)))

    @app.post('/api/youtube/cookies/export')
    def export_youtube_cookie():
        from webapp.cookie_profiles import CookieProfiles
        identity = body().get('id')
        with settings_lock:
            _, base = settings()
            filename, data = CookieProfiles(base, store).export(identity)
        return send_file(BytesIO(data), mimetype='text/plain', as_attachment=True,
                         download_name=filename, conditional=False, etag=False)

    @app.post('/api/youtube/cookies/check')
    def check_youtube_cookie_profile():
        identity = body().get('id')
        with media_lock, settings_lock:
            require_login_idle()
            config, base = settings()
            options = {}
            if config.get('proxy_mode') == 'clash':
                snapshot = clash.snapshot()
                options.update(proxy=snapshot['proxy'], node=snapshot['node'])
            result = youtube_auth.check_profile(identity, config, base, recover=True, **options)
        return jsonify(auth=result, message=result['message'])

    @app.get('/api/youtube/cookies/<identity>/timeline')
    def youtube_cookie_timeline(identity):
        """Return bounded, secret-free history for one saved Cookie profile."""
        from webapp.cookie_profiles import CookieProfiles
        from webapp.youtube_cookie_history import list_events
        with settings_lock:
            config, base = settings()
            profile = next((item for item in CookieProfiles(base, store).items() if item['id'] == identity), None)
            if profile is None:
                raise ValueError('Cookie 配置不存在。')
            def number(name):
                value = request.args.get(name)
                try:
                    return float(value) if value not in (None, '') else None
                except ValueError:
                    return None
            events = list_events(store, identity, since=number('from'), until=number('to'),
                kind=request.args.get('kind'), phase=request.args.get('phase'),
                event_type=request.args.get('event_type'),
                only_interval_changes=request.args.get('only_interval_changes') in ('1', 'true', 'yes'),
                limit=request.args.get('limit', 100))
        return jsonify(profile=profile, events=events)

    @app.post('/api/youtube/cookies/pool/recover')
    def recover_youtube_cookie_pool():
        from webapp.cookie_pool import CookiePool
        with media_lock, settings_lock:
            require_login_idle()
            if cookie_update_busy() or not store.claims_paused():
                raise LoginBusy('请先停止当前实例，并等待全部下载结束，再检查账号池。')
            config, base = settings()
            if not config.get('youtube_cookie_pool'):
                raise ValueError('当前未启用 Cookie 账号池。')
            pool = CookiePool(store, config, base, None)
            if pool.status().get('cooldown_until', 0) > time.time():
                raise ValueError('账号池仍在冷却，请等待冷却结束后再检测和解除拦截。')
            options = {}
            if config.get('proxy_mode') == 'clash':
                snapshot = clash.snapshot()
                options.update(proxy=snapshot['proxy'], node=snapshot['node'])
            for identity in config['youtube_cookie_pool']:
                result = youtube_auth.check_profile(identity, config, base, force=True, **options)
                if result['state'] != 'valid':
                    return jsonify(error='账号池登录检查未全部通过，拦截保持有效。', auth=result), 409
            pool.clear_halt_after_checks()
        return jsonify(message='账号池已通过检查并解除拦截，会话重新从 1 个任务预热，池独立调度。请手动恢复下载；源站冷却保持有效。')

    @app.post('/api/youtube/cookies')
    def add_youtube_cookie():
        from webapp.cookie_profiles import CookieProfiles
        request.max_content_length = 2 * 1024 * 1024
        uploaded = request.files.get('cookie')
        if uploaded is None or not uploaded.filename or Path(uploaded.filename).suffix.lower() != '.txt':
            raise ValueError('请选择 Netscape 格式的 Cookie .txt 文件。')
        data = uploaded.stream.read(1024 * 1024 + 1)
        with media_lock, settings_lock:
            config, base = settings()
            item = CookieProfiles(base, store).add(request.form.get('name'), data, uploaded.filename)
            # A first upload must be immediately usable. Keep an explicitly
            # selected Cookie untouched when adding a second profile.
            activated = not bool(config.get('youtube_cookie'))
            if activated:
                config['youtube_cookie'] = f"Cookies/profiles/{item['id']}.txt"
                config['youtube_cookie_pool'] = []
                save_settings(config_path, config)
                sync_runtime(config, base)
                youtube_auth.invalidate()
                item['active'] = True
        message = ('Cookie 已保存并设为当前使用；请点击“检测登录”确认当前出口可用。'
                   if activated else 'Cookie 已保存；请在列表中点击“使用此 Cookie”后生效。')
        return jsonify(item=item, activated=activated, message=message), 201

    @app.post('/api/youtube/cookies/select')
    def select_youtube_cookie():
        from webapp.cookie_profiles import CookieProfiles
        identity = body().get('id')
        with media_lock, settings_lock:
            require_login_idle()
            if cookie_update_busy():
                raise LoginBusy('请先停止当前实例并等待全部下载结束，再切换 Cookie。')
            config, base = settings()
            target = CookieProfiles(base, store).require(identity)
            parse_youtube_cookie_file(target.read_bytes())
            previous = dict(config)
            original = config_path.read_bytes()
            config['youtube_cookie'] = target.relative_to(base).as_posix()
            config['youtube_cookie_pool'] = []
            try:
                save_settings(config_path, config)
                sync_runtime(config, base)
            except Exception:
                config_path.write_bytes(original)
                sync_runtime(previous, base)
                raise
            youtube_auth.invalidate(preserve_block=True)
        return jsonify(cookie_path=config['youtube_cookie'], message='已切换为单 Cookie，账号池已关闭。请检查登录状态后恢复下载；已有的限流冷却保持有效。')

    @app.post('/api/youtube/cookies/delete')
    def delete_youtube_cookie():
        from webapp.cookie_profiles import CookieProfiles
        identity = body().get('id')
        with media_lock, settings_lock:
            config, base = settings()
            profiles = CookieProfiles(base, store)
            items = profiles.items(config.get('youtube_cookie', ''))
            item = next((item for item in items if item['id'] == identity), None)
            if item is None:
                raise ValueError('Cookie 配置不存在。')
            pooled = identity in config.get('youtube_cookie_pool', [])
            changed = item['active'] or pooled
            replacement = None
            previous = dict(config)
            original = config_path.read_bytes()
            if changed:
                require_login_idle()
                if cookie_update_busy():
                    raise LoginBusy('请先停止当前实例并等待全部下载结束，再删除正在使用的 Cookie。')
                if item['active']:
                    replacement = next((value for value in items if value['id'] != identity), None)
                    config['youtube_cookie'] = (profiles.require(replacement['id']).relative_to(base).as_posix()
                                                if replacement else '')
                if pooled:
                    remaining_pool = [value for value in config['youtube_cookie_pool'] if value != identity]
                    config['youtube_cookie_pool'] = remaining_pool if len(remaining_pool) >= 2 else []
            try:
                if changed:
                    save_settings(config_path, config)
                    sync_runtime(config, base)
                    youtube_auth.invalidate(preserve_block=True)
                profiles.delete(identity)
            except Exception:
                if changed:
                    config_path.write_bytes(original)
                    profiles.restore_selected(previous.get('youtube_cookie', ''))
                    sync_runtime(previous, base)
                raise
        message = ('Cookie 已删除，已自动选择其他 Cookie 作为当前使用项。' if replacement else
                   'Cookie 已删除，当前未使用 Cookie。' if item['active'] else 'Cookie 已删除。')
        return jsonify(message=message, cookie_path=config.get('youtube_cookie', ''),
                       cookie_pool=config.get('youtube_cookie_pool', []),
                       selection_changed=bool(item['active']))

    @app.get('/api/media')
    def media():
        config, base = settings()
        root = Path(io_path(video_directory(config, base))).resolve()
        return jsonify(**catalog.page(root, limit=int(request.args.get('limit', 24)),
                       cursor=request.args.get('cursor'), query=request.args.get('q', '')[:200]), directory=str(root),
                       indexing=catalog.root_status(root))

    @app.post('/api/media/reindex')
    def reindex_media():
        if store.media_clear_busy():
            return jsonify(error='素材库正在清空，完成后再同步本地文件。'), 409
        config, base = settings()
        task = store.create('index', {'config': config, 'base': str(base), 'scope': 'download'})
        return jsonify(task), 202

    @app.post('/api/media/clear')
    def clear_media_library():
        from webapp.media_cleanup import clear_media, validate_clear_root
        data = body()
        if data.get('confirmation') != '清空':
            raise ValueError('请输入“清空”确认删除素材文件。')
        with media_lock, settings_lock:
            config, base = settings()
            protected = [ROOT, config_path, store.directory]
            if config.get('youtube_cookie'):
                protected.append(resolve_path(base, config['youtube_cookie']))
            root = validate_clear_root(video_directory(config, base), protected)
            if data.get('directory') != str(root):
                raise ValueError('素材目录已变化，请刷新页面后重新确认。')
            try:
                task = store.start_media_clear({'base': str(base), 'directory': str(root)})
            except ValueError as error:
                return jsonify(error=str(error)), 409

        def run_clear():
            stopped = threading.Event()
            def heartbeat():
                while not stopped.wait(10):
                    try:
                        if not store.heartbeat(task['id'], store.owner):
                            return
                    except Exception:
                        return
            keeper = threading.Thread(target=heartbeat, name='media-clear-heartbeat', daemon=True)
            keeper.start()
            try:
                def report(value):
                    if not store.update_progress(task['id'], value, store.owner):
                        raise RuntimeError('清空任务租约已失效，清理已停止。')
                validate_clear_root(root, protected)
                result = clear_media(catalog, root, report)
                store.finish(task['id'], result=result, worker_id=store.owner)
            except Exception as error:
                store.finish(task['id'], error=str(error), worker_id=store.owner)
            finally:
                stopped.set()
                keeper.join(timeout=2)
        threading.Thread(target=run_clear, name='media-clear', daemon=True).start()
        return jsonify(task), 202

    @app.get('/api/overview')
    def overview():
        config, base = settings()
        root = video_directory(config, base)
        accounting = store.statistics.dashboard()
        if fleet and fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            from sqlalchemy import func, select
            from webapp.database import media as catalog_media, roots as catalog_roots, instances as fleet_instances
            with store.connect() as db:
                current_roots = []
                for identity, value in db.execute(select(fleet_instances.c.id, fleet_instances.c.config)
                                                  .where(fleet_instances.c.role.in_(('worker', 'hybrid')))):
                    directory = json.loads(value).get('directory')
                    if directory:
                        current_roots.append('@' + identity + '|' + directory)
                total = db.execute(select(func.coalesce(func.sum(catalog_roots.c.count), 0)).where(catalog_roots.c.root.in_(current_roots))).scalar_one()
                recent = [dict(row) for row in db.execute(select(catalog_media).where(catalog_media.c.root.in_(current_roots))
                          .order_by(catalog_media.c.modified.desc()).limit(4)).mappings()]
            for item in recent:
                item['instance_id'] = item['root'][1:].split('|', 1)[0]
            return jsonify(counts=store.counts(actions=('download', 'subtitles', 'check', 'import', 'index', 'clear_media')),
                download_counts=store.counts(actions=('download',)), accounting=accounting,
                media={'count': int(total)}, recent_media=recent,
                recent_tasks=store.page(limit=5, actions=('download', 'subtitles', 'check', 'import', 'index', 'clear_media'), finished_only=True)['items'])
        return jsonify(counts=store.counts(actions=('download', 'subtitles', 'check', 'import', 'index', 'clear_media')),
                       download_counts=store.counts(actions=('download',)), accounting=accounting,
                       media=catalog.root_status(root), recent_media=catalog.page(root, limit=4)['items'],
                       recent_tasks=store.page(limit=5, actions=('download', 'subtitles', 'check', 'import', 'index', 'clear_media'), finished_only=True)['items'])

    @app.get('/media/videos/<path:name>')
    def video(name):
        # Block path traversal attempts
        if '..' in name.split('/') or name.startswith('/') or '\\' in name:
            return jsonify(error='视频不存在'), 404
        config, base = settings()
        root = Path(io_path(video_directory(config, base))).resolve()
        target = (root / name).resolve()
        if (not target.is_relative_to(root) or target.suffix.lower() not in VIDEO_SUFFIXES
                or not target.is_file()):
            return jsonify(error='视频不存在'), 404
        mimetype = {'.mp4': 'video/mp4', '.webm': 'video/webm',
                    '.mov': 'video/quicktime', '.mkv': 'video/x-matroska'}[target.suffix.lower()]
        # Werkzeug handles byte ranges so the native player can seek large files.
        return send_file(str(target), mimetype=mimetype, conditional=True)

    @app.get('/media/covers/<path:name>')
    def cover(name):
        # Block path traversal attempts
        if '..' in name.split('/') or name.startswith('/') or '\\' in name:
            return jsonify(error='封面不存在'), 404
        config, base = settings()
        root = Path(io_path(video_directory(config, base))).resolve()
        target = (root / name).resolve()
        if not target.is_relative_to(root) or target.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp') or not target.is_file():
            return jsonify(error='封面不存在'), 404
        return send_file(target, conditional=True)

    @app.post('/api/media')
    def import_media():
        if store.pending():
            return jsonify(error='请等待当前任务结束后再导入素材。'), 409
        video, image = request.files.get('video'), request.files.get('cover')
        if video is None or image is None or not video.filename or not image.filename:
            raise ValueError('请选择一个视频和对应的封面图片。')
        suffix = Path(video.filename).suffix.lower()
        cover_suffix = Path(image.filename).suffix.lower()
        if suffix not in VIDEO_SUFFIXES or cover_suffix not in ('.jpg', '.jpeg', '.png', '.webp'):
            raise ValueError('视频支持 MP4 / MKV / WebM / MOV；封面支持 JPG / PNG / WebP。')
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(video.filename).stem).strip(' .')[:100]
        if not stem or stem.upper() in ('CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))):
            raise ValueError('视频文件名无效，请重命名后重试。')
        config, base = settings()
        root = Path(io_path(video_directory(config, base)))
        root.mkdir(parents=True, exist_ok=True)
        target, cover_target = root / (stem + suffix), root / (stem + cover_suffix)
        with media_lock:
            if store.pending():
                return jsonify(error='请等待当前任务结束后再导入素材。'), 409
            if target.exists() or any((root / (stem + ext)).exists() for ext in ('.jpg', '.jpeg', '.webp', '.png')):
                return jsonify(error='存在同名素材，请先重命名文件。'), 409
            with tempfile.TemporaryDirectory(prefix='.import-', dir=root) as staging:
                temporary_video = Path(staging) / ('video' + suffix)
                temporary_cover = Path(staging) / ('cover' + cover_suffix)
                video.save(temporary_video)
                image.save(temporary_cover)
                if not temporary_video.stat().st_size:
                    raise ValueError('视频文件为空。')
                try:
                    with Image.open(temporary_cover) as opened:
                        opened.verify()
                except (UnidentifiedImageError, OSError, ValueError) as error:
                    raise ValueError('封面不是有效图片。') from error
                temporary_cover.replace(cover_target)
                try:
                    temporary_video.replace(target)
                except OSError:
                    cover_target.unlink(missing_ok=True)
                    raise
        catalog.index_file(target, root)
        return jsonify(message='素材已导入，封面已自动匹配视频名称。', name=target.name), 201

    @app.get('/api/tasks')
    def tasks():
        actions = ('download', 'subtitles', 'check', 'import', 'index', 'clear_media') if request.args.get('scope') == 'download' else None
        active = request.args.get('view') == 'active'
        if active:
            actions = ('download',)
        batch_id = request.args.get('batch_id')
        return jsonify(**store.page(actions=actions, limit=int(request.args.get('limit', 50)),
                       cursor=request.args.get('cursor'), status='active' if active else request.args.get('status'),
                       finished_only=request.args.get('view') == 'history',
                       query=request.args.get('q', '')[:200], batch_id=batch_id, order=request.args.get('order', 'desc')),
                       counts=store.counts(actions=actions, batch_id=batch_id))

    @app.get('/api/batches')
    def download_batches():
        return jsonify(store.batch_page(cursor=request.args.get('cursor')))

    @app.post('/api/downloads/stop')
    def stop_downloads():
        from webapp.control import stop_downloads as stop_workers
        startup.cancel()
        with media_lock:
            store.set_downloads_paused(True)
            stop_workers(store)
            queue = store.download_queue()
        message = '正在暂停当前下载，排队任务已保留。' if queue['running'] else '下载已暂停，待处理清单和部分文件已保留。'
        return jsonify(message=message, download_queue=queue), 202 if queue['running'] else 200

    @app.post('/api/downloads/resume')
    def resume_downloads():
        with media_lock:
            return jsonify(app.extensions['execution_control'].resume_system())

    @app.get('/api/downloads/startup')
    def startup_status():
        return jsonify(startup.status())

    def activate_downloads(stage, cancelled):
        stage('checking_database')
        if store.media_clear_busy():
            raise ValueError('素材库正在清空，请完成后再开始下载。')
        queue = store.download_queue()
        if queue['paused'] and queue['running']:
            raise ValueError('下载进程正在停止，请稍后再开始。')
        if not queue['queued'] and not queue['importing']:
            raise ValueError('没有待处理的视频，请先添加清单。')
        if fleet and fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            from webapp.fleet_routes import preflight_instances
            stage('checking_instances')
            policy_version = preflight_instances(fleet, stage, cancelled)
            if cancelled.is_set():
                raise InterruptedError('启动已取消。')
            stage('releasing_queue')
            store.activate_download_lists(expected_policy_version=policy_version)
            return
        config, base = settings()
        stage('reading_queue')
        upcoming = store.page(limit=1, actions=('download',), status='queued', order='asc')['items']
        urls = [upcoming[0]['url']] if upcoming else ['https://www.youtube.com/']
        if upcoming:
            validate_method_urls(config, urls)
        stage('preparing_proxy')
        snapshot = clash.snapshot() if config.get('proxy_mode') == 'clash' else None
        stage('checking_environment')
        methods = download_methods(config)
        if 'yt1d' in methods:
            require_yt1d_environment(config, base)
        if 'bergung' in methods:
            from webapp.bergung import require_environment
            require_environment(config, base)
        if 'downmp3' in methods:
            require_downmp3_environment()
        if 'yt5s' in methods:
            (require_fallback_environment if 'yt_dlp' in methods else require_browser_environment)(config, base)
        elif 'yt_dlp' in methods:
            from webapp.environment import require_native_environment
            require_native_environment(config)
        if ('yt_dlp' in methods and is_youtube(urls) and not config.get('youtube_cookie_pool')
                and config.get('youtube_cookie_usage', 'auto') == 'always'):
            stage('checking_login')
            youtube_auth.require(config, base, force=True, **(
                {'proxy': snapshot['proxy'], 'node': snapshot['node']} if snapshot else {}))
        with media_lock:
            if cancelled.is_set():
                raise InterruptedError('启动已取消，清单仍在待处理队列。')
            if settings()[0] != config:
                raise ValueError('启动检查期间配置发生变化，请按已保存的新配置重新开始。')
            stage('releasing_queue')
            youtube_auth.clear_block()
            store.activate_download_lists()

    @app.post('/api/downloads/start')
    def start_downloads():
        return jsonify(startup.start(activate_downloads)), 202

    @app.post('/api/downloads/import')
    def import_downloads():
        config, _ = settings()
        request.max_content_length = int(config.get('disk_upload_limit_mb', 256)) * 1024 ** 2 + 1024 ** 2
        source = request.files.get('file')
        if not source or not source.filename or Path(source.filename).suffix.lower() not in ('.txt', '.csv'):
            raise ValueError('请选择 UTF-8 编码的 TXT 或 CSV 文件。')
        _, base = settings()
        payload = {'base': str(base), 'scope': 'download'}
        identity = uuid.uuid4().hex
        directory = store.directory / 'imports'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (identity + Path(source.filename).suffix.lower())
        try:
            from webapp.disk_uploads import save_import
            save_import(source, path, store, config)
            with media_lock:
                from webapp.bulk import create_import
                task = create_import(store, identity, path, Path(source.filename).name, payload, list_data=request.form)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return jsonify(task), 202

    @app.get('/api/tasks/<task_id>')
    def task_detail(task_id):
        task = store.get(task_id)
        if task:
            from webapp.risk_diagnostics import task_risks
            task['risk'] = task_risks(task)
        return (jsonify(task), 200) if task else (jsonify(error='任务不存在'), 404)

    @app.get('/api/tasks/<task_id>/logs')
    def task_logs(task_id):
        task = store.get(task_id)
        if not task:
            return jsonify(error='任务不存在'), 404
        try:
            offset = int(request.args.get('offset', 0))
        except ValueError:
            raise ValueError('日志偏移量必须为整数。')
        result = store.logs(task_id, offset)
        if result.get('state') == 'missing':
            result['state'] = ('pruned' if task.get('logs_pruned') else
                               'pending' if task['status'] in ('queued', 'running') else 'missing')
            result['message'] = {'pruned': '日志已按容量或保留期限自动清理，任务结果和统计仍可查看。',
                                 'pending': '尚未产生日志，请等待任务开始执行。',
                                 'missing': '本实例未找到任务日志，请查看实例运行日志及磁盘写入告警。'}[result['state']]
        return jsonify(result)

    @app.post('/api/tasks')
    @app.post('/api/downloads')
    def create_task():
        data = body()
        batch = request.path == '/api/downloads'
        action = 'download' if batch else data.get('action')
        if action not in ACTIONS:
            raise ValueError('不支持的任务类型。')
        config, base = settings()
        # Check the current environment; pending downloads bind settings at claim.
        config = {**DEFAULTS, **config}
        validate_settings(config, download_only=action == 'download' or (action == 'check' and data.get('scope') == 'download'))
        payload = {'config': config, 'base': str(base)}
        if data.get('scope') == 'download':
            payload['scope'] = 'download'
        if action == 'download':
            payload['urls'] = urls_from(data.get('urls') or config.get('url'))
            validate_method_urls(config, payload['urls'])
        with media_lock:
            local_download = action == 'download' and not (fleet and fleet.can_manage and not (has_request_context() and g.fleet_rpc))
            if local_download and config.get('proxy_mode') == 'clash':
                payload['clash'] = clash.snapshot()
            if action == 'download':
                if local_download:
                    verify_youtube(config, base, payload['urls'], payload.get('clash'))
                payload.pop('config', None)
                payload.pop('clash', None)
            if batch:
                batch_id = uuid.uuid4().hex
                payloads = [{**payload, 'urls': [url], 'batch_id': batch_id} for url in payload['urls']]
                return jsonify(batch_id=batch_id, items=store.create_batch(payloads)), 202
            task = store.create(action, payload)
        return jsonify(task), 202

    @app.post('/api/downloads/queue')
    def enqueue_downloads():
        data = body()
        urls = urls_from(data.get('urls'))
        identity = uuid.uuid4().hex
        _, base = settings()
        with store.connect() as db:
            store.lists.create(db, identity, data)
            ids = store.insert_downloads(db, [{'base': str(base), 'scope': 'download', 'batch_id': identity,
                'urls': [url]} for url in urls], deduplicate=True)
        return jsonify(batch_id=identity, added=len(ids), duplicates=len(urls) - len(ids),
                       message='已添加到待处理清单，点击开始下载后执行。'), 202

    register_clash(app, clash, store, media_lock, settings_lock, settings, config_path, sync_runtime)
    from webapp.statistics_routes import register_statistics
    register_statistics(app, store)

    def job_preflight(config, base):
        if fleet and fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            from webapp.fleet_routes import rpc
            nodes = [node for node in fleet.list()['items'] if node['role'] in ('worker', 'hybrid') and node['online'] and not node['paused']]
            warnings = []
            if not nodes:
                return ['任务已开始，等待在线且启用的下载实例。']
            for node in nodes:
                try:
                    rpc(fleet, node['id'], '/api/fleet/preflight', method='POST', data={})
                except (ValueError, YouTubeAuthRequired, BrowserEnvironmentRequired, DownloadEnvironmentRequired) as error:
                    fleet.quarantine(node['id'], str(error), boot_id=node['boot_id'])
                    warnings.append(f'{node["name"]} 已暂停领取：{error}')
            return warnings
        methods = download_methods(config)
        if 'yt1d' in methods:
            require_yt1d_environment(config, base)
        if 'bergung' in methods:
            from webapp.bergung import require_environment
            require_environment(config, base)
        if 'downmp3' in methods:
            require_downmp3_environment()
        if 'yt5s' in methods:
            (require_fallback_environment if 'yt_dlp' in methods else require_browser_environment)(config, base)
        elif 'yt_dlp' in methods:
            from webapp.environment import require_native_environment
            require_native_environment(config)
        if ('yt_dlp' in methods and not config.get('youtube_cookie_pool') and
                config.get('youtube_cookie_usage', 'auto') == 'always'):
            youtube_auth.require(config, base, force=True)

    from webapp.jobs import register_jobs
    register_jobs(app, store, settings, job_preflight)
    from webapp.workbench import register_workbench
    register_workbench(app, store, settings, job_preflight)
    from webapp.managed_deployment import register_instance_deployment
    register_instance_deployment(app)

    def prepare_system_resume():
        if fleet:
            return  # Nodes are checked on enable/start and isolate their own failures.
        from sqlalchemy import select
        from webapp.database import tasks as task_table
        with store.connect() as db:
            upcoming = db.execute(select(task_table.c.batch_id, task_table.c.url).where(task_table.c.action == 'download',
                task_table.c.status == 'queued', store.released_condition()).order_by(task_table.c.seq).limit(1)).first()
        if upcoming:
            config, base = settings()
            verify_youtube(config, base,
                [upcoming.url] if upcoming.url else ['https://www.youtube.com/'],
                force=not config.get('youtube_cookie_pool') and config.get('youtube_cookie_usage', 'auto') == 'always')
        youtube_auth.clear_block()

    app.extensions['execution_control'].prepare_resume = prepare_system_resume

    if start_worker:
        def restore_clash():
            config, _ = settings()
            if config.get('proxy_mode') == 'clash':
                try:
                    clash.start()
                except ClashError as error:
                    clash.last_error = str(error)
        threading.Thread(target=restore_clash, name='restore-clash', daemon=True).start()
        runner = TaskRunner(store, lambda: settings()[0], base=config_path.parent)
        runner.start()
        app.extensions['task_runner'] = runner
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='视频工作台 Flask 服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=5000, type=int)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--env-file', type=Path, help='数据库环境文件，默认读取项目目录中的 .env')
    parser.add_argument('--role', type=normalize_role, metavar='hybrid', help='固定为管理与下载一体；兼容读取旧角色名称')
    parser.add_argument('--state', type=Path, help='实例独立状态目录')
    parser.add_argument('--instance-id', help='固定实例 ID，不同实例必须不同')
    parser.add_argument('--instance-url', default='', help='管理端能访问的下载实例地址')
    parser.add_argument('--instance-name', help='页面展示名称')
    args = parser.parse_args()
    from waitress import create_server
    from webapp.deployment import DeploymentGateway
    service = DeploymentGateway(args.config, args.state or ROOT / '.cache' / 'web', env_file=args.env_file,
                                role=args.role, instance_id=args.instance_id, instance_url=args.instance_url,
                                instance_name=args.instance_name)
    server = create_server(service, host=args.host, port=args.port, threads=8,
                           max_request_body_size=8 * 1024 ** 3, channel_timeout=300)
    print(f'视频工作台：http://{args.host}:{args.port}', flush=True)
    print(f'部署设置：http://{args.host}:{args.port}/setup', flush=True)
    print(f'部署设置访问码：{service.access_code}', flush=True)
    service.start()
    try:
        server.run()
    finally:
        server.close()

