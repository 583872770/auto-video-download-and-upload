"""Disk and diagnostics routes."""
from flask import Blueprint, current_app, jsonify, request

bp = Blueprint('disk', __name__, url_prefix='/api/disk')


@bp.get('')
def disk_status():
    """Get disk budget status."""
    from webapp.disk_budget import DiskBudget

    store = current_app.extensions['task_store']
    settings = current_app.extensions.get('settings_func')

    if settings:
        config, _ = settings()
    else:
        from webapp.config.settings import DEFAULTS
        config = DEFAULTS

    return jsonify(DiskBudget(store.directory, config).snapshot())


@bp.get('/logs')
def instance_logs():
    """Get instance runtime logs."""
    from webapp.diagnostics import read_runtime_logs

    store = current_app.extensions['task_store']
    import psutil
    since = psutil.Process().create_time() if request.args.get('scope') == 'current' else None
    return jsonify(text=read_runtime_logs(store.directory, since=since), since=since)


@bp.get('/runtime-summary')
def runtime_summary():
    import json
    from webapp.runtime_summary import read
    try:
        days = int(request.args.get('days', 1))
    except (TypeError, ValueError):
        return jsonify(error='days 必须是 1～30 的整数。'), 400
    if not 1 <= days <= 30:
        return jsonify(error='days 必须是 1～30 的整数。'), 400
    store = current_app.extensions['task_store']
    result = read(store, days)
    try:
        with (store.directory / 'log-retention.json').open(encoding='utf-8') as source:
            result['log_retention'] = json.loads(source.read(4096))
    except (OSError, ValueError):
        result['log_retention'] = None
    return jsonify(result)


@bp.post('/cleanup')
def disk_cleanup():
    """Clean up temporary files and logs."""
    from webapp.disk_budget import DiskBudget

    store = current_app.extensions['task_store']
    settings = current_app.extensions.get('settings_func')

    if settings:
        config, _ = settings()
    else:
        from webapp.config.settings import DEFAULTS
        config = DEFAULTS

    result = DiskBudget(store.directory, config).cleanup(store)
    return jsonify(
        message='已检查并清理可安全删除的临时文件和过期日志，统计与素材保留。',
        disk=result
    )
