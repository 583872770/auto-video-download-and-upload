"""Common helper functions for routes."""
from flask import current_app, request, jsonify
from pathlib import Path

from Utils.config import load_config
from webapp.settings import DEFAULTS


def get_settings():
    """Get current merged settings (cached via closure in app.py)."""
    settings_func = current_app.extensions.get('settings_func')
    if settings_func:
        return settings_func()

    # Fallback for routes that don't have settings closure
    config_path = current_app.config.get('CONFIG_PATH')
    if config_path:
        config, base = load_config(Path(config_path))
    else:
        config, base = {}, None

    fleet = current_app.extensions.get('fleet')
    if fleet and fleet.can_download:
        import json
        config.update(json.loads(fleet.row()['limits']))

    return config, base


def get_body():
    """Get and validate JSON request body."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('请提交 JSON 对象。')
    return data


def sync_runtime(config, base):
    """Sync runtime configuration to store/fleet."""
    fleet = current_app.extensions.get('fleet')
    clash = current_app.extensions['clash']
    store = current_app.extensions['task_store']

    if fleet:
        fleet.save_local({**DEFAULTS, **config}, base, clash.directory)
        return

    from Utils.config import resolve_path
    cookie = str(resolve_path(base, config['youtube_cookie'])) if config.get('youtube_cookie') else ''
    store.set_runtime({**DEFAULTS, **config, 'youtube_cookie': cookie})
    store.set_download_settings({**DEFAULTS, **config}, base, clash.directory)
