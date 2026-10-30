"""Disk and diagnostics routes."""
from flask import Blueprint, current_app, jsonify

bp = Blueprint('disk', __name__, url_prefix='/api/disk')


@bp.get('/')
def disk_status():
    """Get disk budget status."""
    from webapp.disk_budget import DiskBudget

    store = current_app.extensions['task_store']
    settings = current_app.extensions.get('settings_func')

    if settings:
        config, _ = settings()
    else:
        from webapp.settings import DEFAULTS
        config = DEFAULTS

    return jsonify(DiskBudget(store.directory, config).snapshot())


@bp.get('/logs')
def instance_logs():
    """Get instance runtime logs."""
    from webapp.diagnostics import read_runtime_logs

    store = current_app.extensions['task_store']
    return jsonify(text=read_runtime_logs(store.directory))


@bp.post('/cleanup')
def disk_cleanup():
    """Clean up temporary files and logs."""
    from webapp.disk_budget import DiskBudget

    store = current_app.extensions['task_store']
    settings = current_app.extensions.get('settings_func')

    if settings:
        config, _ = settings()
    else:
        from webapp.settings import DEFAULTS
        config = DEFAULTS

    result = DiskBudget(store.directory, config).cleanup(store)
    return jsonify(
        message='已检查并清理可安全删除的临时文件和过期日志，统计与素材保留。',
        disk=result
    )
