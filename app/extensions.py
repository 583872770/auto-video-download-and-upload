"""Flask extensions initialization."""
import json
import threading
from pathlib import Path

from Utils.config import DEFAULT_CONFIG, load_config
from webapp.catalog import MediaCatalog
from webapp.clash import ClashManager
from webapp.console_auth import ConsoleAuth
from webapp.youtube_auth import YouTubeAuth
from webapp.startup import DownloadStartup
from webapp.execution import ExecutionControlService
from webapp.settings import DEFAULTS


def init_extensions(app, store, config_path, service_role, instance_id, instance_url, instance_name):
    """Initialize and register all extensions."""
    catalog = MediaCatalog(store)
    app.extensions['media_catalog'] = catalog

    settings_lock = threading.RLock()
    media_lock = threading.RLock()
    app.extensions['settings_lock'] = settings_lock
    app.extensions['media_lock'] = media_lock

    clash = ClashManager(store.directory / 'clash')
    app.extensions['clash'] = clash

    fleet = None
    registered = bool(service_role or instance_id or store.fleet or store.single or store.db.mysql)
    if registered:
        from webapp.fleet import Fleet
        fleet = Fleet(store, service_role, identity=instance_id, url=instance_url, name=instance_name)
        initial, initial_base = load_config(Path(config_path or DEFAULT_CONFIG).resolve())
        fleet.initialize({**DEFAULTS, **initial}, initial_base, clash.directory)
    app.extensions['fleet'] = fleet

    youtube_auth = YouTubeAuth(store)
    app.extensions['youtube_auth'] = youtube_auth

    startup = DownloadStartup()
    app.extensions['download_startup'] = startup

    execution_control = ExecutionControlService(store)
    app.extensions['execution_control'] = execution_control

    console_auth = ConsoleAuth(app, store)
    app.extensions['console_auth'] = console_auth

    return {
        'catalog': catalog,
        'settings_lock': settings_lock,
        'media_lock': media_lock,
        'clash': clash,
        'fleet': fleet,
        'youtube_auth': youtube_auth,
        'startup': startup,
        'execution_control': execution_control,
        'console_auth': console_auth,
    }
