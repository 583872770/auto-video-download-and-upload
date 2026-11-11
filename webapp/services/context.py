"""Explicit dependencies shared by one application's services and routes."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class WebDependencies:
    save_settings: Callable
    require_browser_environment: Callable
    require_fallback_environment: Callable
    require_downmp3_environment: Callable
    require_yt1d_environment: Callable
    selected_id: Callable
    rpc: Callable
    preflight_instances: Callable


class WebContext:
    def __init__(self, runtime, dependencies):
        self.runtime = runtime
        self.dependencies = dependencies
        self.app = runtime.app
        self.config_path = runtime.config_path
        self.settings = runtime.settings
        self.sync_runtime = runtime.sync_runtime
        self.service_role = self.app.config['SERVICE_ROLE']
        self.store = self.app.extensions['task_store']
        self.catalog = self.app.extensions['media_catalog']
        self.startup = self.app.extensions['download_startup']
        self.settings_lock = self.app.extensions['settings_lock']
        self.media_lock = self.app.extensions['media_lock']
        self.clash = self.app.extensions['clash']
        self.fleet = self.app.extensions['fleet']
        self.youtube_auth = self.app.extensions['youtube_auth']
        self.console_auth = self.app.extensions['console_auth']
