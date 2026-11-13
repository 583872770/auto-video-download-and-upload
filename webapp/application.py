"""Application assembly and background startup, separate from HTTP routes."""
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import socket
import threading
from typing import Callable

from flask import Flask

from Utils.config import DEFAULT_CONFIG, load_config
from Utils.runtime import ROOT, configure_runtime
from webapp.clash import ClashError
from webapp.service_role import normalize_role
from webapp.settings import DEFAULTS
from webapp.runtime.supervisor import TaskRunner
from webapp.store import TaskStore


@dataclass(frozen=True)
class ApplicationRuntime:
    app: Flask
    config_path: Path
    settings: Callable
    sync_runtime: Callable


def initialize_application(import_name, config_path=None, state_dir=None, *, env_file=None,
                           role=None, instance_id=None, instance_url='', instance_name=None,
                           database_url=None, startup_report=None):
    """Build local state and extensions without starting background services."""
    configure_runtime()
    if not database_url and (state_dir is None or env_file is not None):
        from webapp.bootstrap import configure_database_environment
        configure_database_environment(env_file)
    service_role = normalize_role(role or os.environ.get('VIDEO_SERVICE_ROLE'))
    app = Flask(import_name, root_path=str(ROOT))
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
    settings_lock = exts['settings_lock']
    fleet = exts['fleet']

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

    app.extensions['settings_func'] = settings
    app.extensions['disk_config'] = lambda: {**DEFAULTS, **settings()[0]}

    from webapp.helpers import sync_runtime as sync_request_runtime

    def sync_runtime(config, base):
        with app.app_context():
            sync_request_runtime(config, base)

    return ApplicationRuntime(app, config_path, settings, sync_runtime)


def start_background_services(runtime):
    """Start services only after all routes and execution callbacks are ready."""
    app = runtime.app
    settings = runtime.settings
    clash = app.extensions['clash']
    store = app.extensions['task_store']
    config_path = runtime.config_path

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


def build_application(import_name, config_path=None, state_dir=None, *, start_worker=True,
                      env_file=None, role=None, instance_id=None, instance_url='',
                      instance_name=None, database_url=None, startup_report=None, dependencies):
    """Assemble services and HTTP routes before starting background work."""
    from webapp.services.context import WebContext
    from webapp.services.configuration import ConfigurationService
    from webapp.services.cookies import CookieService
    from webapp.services.dashboard import DashboardService
    from webapp.services.media_library import MediaLibraryService
    from webapp.services.download_queue import DownloadQueueService
    from webapp.services.preflight import DownloadPreflight
    from webapp.youtube_login import YouTubeLogin
    from webapp.middleware import register_middleware
    from webapp.routes import register_blueprints
    from webapp.routes.configuration import register_configuration_routes
    from webapp.routes.dashboard import register_dashboard_routes
    from webapp.routes.media import register_media_routes
    from webapp.routes.pages import register_pages_routes
    from webapp.routes.tasks import register_tasks_routes
    from webapp.routes.youtube import register_youtube_routes

    runtime = initialize_application(import_name, config_path, state_dir, env_file=env_file,
                                     role=role, instance_id=instance_id, instance_url=instance_url,
                                     instance_name=instance_name, database_url=database_url,
                                     startup_report=startup_report)
    context = WebContext(runtime, dependencies)
    app = context.app
    cookies = CookieService(context)
    preflight = DownloadPreflight(context)
    configuration = ConfigurationService(context, cookies)
    dashboard = DashboardService(context)
    media_library = MediaLibraryService(context)
    queue = DownloadQueueService(context, preflight)
    app.extensions['youtube_login'] = YouTubeLogin(context.store.directory, cookies.install_browser_cookie)
    app.extensions['start_account_browser_login'] = cookies.start_browser_login
    app.extensions['execution_control'].prepare_resume = preflight.prepare_system_resume

    register_blueprints(app)
    register_middleware(context)
    if not context.fleet:
        context.sync_runtime(*context.settings())
    if context.fleet:
        from webapp.fleet_routes import register_fleet
        register_fleet(app, context.fleet)
    from webapp.account_routes import register_accounts
    register_accounts(app)
    register_pages_routes(context)
    register_configuration_routes(context, configuration)
    register_dashboard_routes(context, dashboard)
    register_youtube_routes(context, cookies, preflight)
    register_media_routes(context, media_library)
    register_tasks_routes(context, queue, preflight)

    from webapp.clash_routes import register_clash
    register_clash(app, context.clash, context.store, context.media_lock, context.settings_lock,
                   context.settings, context.config_path, context.sync_runtime)
    from webapp.statistics_routes import register_statistics
    register_statistics(app, context.store)
    from webapp.jobs import register_jobs
    from webapp.workbench import register_workbench
    register_jobs(app, context.store, context.settings, preflight.job_preflight)
    from webapp.delivery.routes import register_delivery
    register_delivery(app, context.store, context.settings)
    register_workbench(app, context.store, context.settings, preflight.job_preflight)
    from webapp.managed_deployment import register_instance_deployment
    register_instance_deployment(app)
    if start_worker:
        start_background_services(runtime)
    return app
