"""Common helper functions for routes."""
from flask import current_app, jsonify, request

from webapp.settings import DEFAULTS


def json_result(value):
    """Serialize service data while preserving existing status/Response results."""
    if isinstance(value, tuple):
        payload, *options = value
        return (json_result(payload), *options)
    return jsonify(value) if isinstance(value, (dict, list)) else value


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
