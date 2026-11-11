"""Manager UI endpoints and authenticated streaming access to worker-local resources."""
import re

from flask import Response, current_app, g, jsonify, request, stream_with_context
import requests

from webapp.adapters.fleet import UploadBody, instance_auth_error, rpc
from webapp.database import settings
from webapp.fleet import DOWNLOAD_ROLES, LOCAL_FIELDS, POLICY_FIELDS
from webapp.settings import DEFAULTS, validate_settings, download_methods


def selected_id():
    fleet = current_app.extensions['fleet']
    return (request.headers.get('X-Instance-ID') or request.args.get('instance_id')
            or fleet.id)








def selected_configuration(fleet, config):
    if not selected_id() or selected_id() == fleet.id:
        return config
    local = rpc(fleet, selected_id(), '/api/config')['config']
    from Utils.oss_storage import OSS_DEFAULTS, OSS_SECRETS
    return {**config, **{k: local[k] for k in LOCAL_FIELDS | POLICY_FIELDS if k in local},
            **{k: local.get(k, default) for k, default in OSS_DEFAULTS.items()},
            **{k + '_configured': local.get(k + '_configured', False) for k in OSS_SECRETS}}


def update_configuration(fleet, data):
    from webapp.services.instances import update_configuration as save
    return jsonify(save(fleet, data, target=selected_id(), rpc=rpc))


def preflight_instances(fleet, stage, cancelled):
    from webapp.services.instances import preflight_instances as check
    return check(fleet, stage, cancelled, rpc=rpc)


def register_fleet(app, fleet):
    from webapp.display_cache import display_cache
    views = display_cache(app)
    fleet.check_local = lambda policy: check_instance(app, fleet, policy)

    @app.before_request
    def route_resources():
        if not fleet.can_manage or g.fleet_rpc:
            return None
        path = request.path
        resource = (path.startswith(('/api/media', '/media/', '/api/clash', '/api/youtube', '/api/disk'))
                    or path == '/api/downloads/connection'
                    or path == '/api/status' and selected_id())
        target = selected_id()
        if re.fullmatch(r'/api/tasks/[a-f0-9]{32}/logs', path):
            task = fleet.store.get(path.split('/')[3])
            target = task and task.get('instance_id')
            resource = bool(target and target != fleet.id)
        if path == '/api/tasks' and request.method == 'POST' and (request.get_json(silent=True) or {}).get('action') == 'check':
            resource = True
        if not resource:
            return None
        row = fleet.resolve_target(target)
        if row['id'] == fleet.id:
            return None  # Local resources use this request's CSRF-protected session.
        headers = {key: value for key, value in request.headers if key.lower() in
                   ('content-type', 'content-length', 'range', 'if-range', 'if-none-match', 'if-modified-since')}
        headers['Authorization'] = 'Bearer ' + row['token']
        headers['Accept-Encoding'] = request.headers.get('Accept-Encoding', 'identity')
        session = requests.Session()
        session.trust_env = False
        query = [(key, value) for key, value in request.args.items(multi=True) if key != 'instance_id']
        upload = None
        if request.method not in ('GET', 'HEAD'):
            upload = request.get_data(cache=True) if request.is_json else (
                UploadBody(request.stream, request.content_length) if request.content_length else request.stream)
        try:
            response = session.request(request.method, row['url'] + path, params=query, headers=headers,
                                       data=upload,
                                       stream=True, timeout=(5, 60), allow_redirects=False)
        except requests.RequestException:
            session.close()
            return jsonify(error='下载实例连接失败，请检查地址和网络。'), 503
        if response.status_code in (401, 403):
            response.close()
            session.close()
            return jsonify(error=instance_auth_error(row), error_type='instance_auth', instance_id=row['id']), 502
        def content():
            try:
                # Keep media ranges and Content-Length consistent; do not decompress.
                yield from response.raw.stream(128 * 1024, decode_content=False)
            finally:
                response.close()
                session.close()
        response_headers = {key: value for key, value in response.headers.items() if key.lower() in
                            ('content-type', 'content-length', 'content-range', 'accept-ranges', 'content-disposition',
                             'etag', 'last-modified', 'cache-control', 'content-encoding')}
        forwarded = Response(stream_with_context(content()), status=response.status_code, headers=response_headers)
        forwarded.call_on_close(response.close)
        forwarded.call_on_close(session.close)
        return forwarded

    @app.get('/api/instances')
    def list_instances():
        if not fleet.can_manage:
            return jsonify(error='此接口仅供管理端使用。'), 403
        compact = request.args.get('compact') == '1'
        detail = selected_id() if compact else None
        return jsonify(**views.read(('instances', compact, detail),
                       lambda: fleet.list(compact=compact, detail_id=detail)),
                       startup=app.extensions['download_startup'].status(),
                       execution=app.extensions['execution_control'].status())

    @app.put('/api/instances/<identity>')
    def update_instance(identity):
        if not fleet.can_manage:
            return jsonify(error='此接口仅供管理端使用。'), 403
        data = request.get_json() or {}
        if data.get('action') == 'resume':
            rpc(fleet, identity, '/api/fleet/preflight', method='POST', data={})
        fleet.control(identity, data)
        messages = {'resume': '实例已启动。', 'drain': '实例将在正在处理的视频全部完成后停止。',
                    'stop': '已发送立即停止指令，正在停止当前实例的下载。'}
        return jsonify(message=messages.get(data.get('action'), '实例配置已保存。'))

    @app.put('/api/instances/limits')
    def update_global_limit():
        if not fleet.can_manage:
            return jsonify(error='此接口仅供管理端使用。'), 403
        value = (request.get_json() or {}).get('global_limit')
        if type(value) is not int or not 0 <= value <= 10000:
            raise ValueError('全局并发数必须在 0 到 10000 之间，0 表示仅按实例额度执行。')
        with fleet.store.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'fleet_budget').values(value=str(value)))
        return jsonify(message='全局并发上限已保存，降低上限后等待当前任务完成再领取。')

    @app.post('/api/fleet/preflight')
    def preflight():
        if not fleet.can_download:
            return jsonify(error='此接口仅供下载实例使用。'), 403
        value, status = check_instance(app, fleet, request.get_json() or {})
        return jsonify(value), status


def check_instance(app, fleet, policy):
    """Check this node directly so its management requests never wait on its own HTTP pool."""
    from webapp.environment import require_downmp3_environment, require_native_environment, require_fallback_environment
    from webapp.yt5s import require_browser_environment
    from webapp.youtube_auth import YouTubeAuth
    from Utils.config import video_directory
    from Utils.download_storage import DownloadWorkspace
    local = fleet.local_settings()
    config = dict(local['config'])
    validate_settings(config)
    base = local['base']
    methods = download_methods(config)
    if 'yt1d' in methods:
        from webapp.yt1d import require_environment
        require_environment(config, base)
    if 'bergung' in methods:
        from webapp.bergung import require_environment
        require_environment(config, base)
    if 'downmp3' in methods:
        require_downmp3_environment()
    if 'yt5s' in methods:
        (require_fallback_environment if 'yt_dlp' in methods else require_browser_environment)(config, base)
    elif 'yt_dlp' in methods:
        require_native_environment(config)
    snapshot = app.extensions['clash'].snapshot() if config.get('proxy_mode') == 'clash' else None
    if 'yt_dlp' in methods:
        auth = YouTubeAuth(fleet.store)
        result = auth.check_startup(config, base, force=True, **({'proxy': snapshot['proxy'], 'node': snapshot['node']} if snapshot else {}))
        if result['state'] not in ('valid', 'waiting'):
            return {'error': result['message']}, 409
        if result['state'] == 'valid':
            auth.clear_block()
    import shutil
    from pathlib import Path
    workspace = DownloadWorkspace(config, fleet.store.directory)
    root = video_directory(workspace.config, Path(base))
    # Check the parent of a temporary workspace without leaving a test directory.
    if workspace.test_mode:
        root = fleet.store.directory
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < int(config.get('min_free_gb', 2)) * 1024 ** 3:
        raise ValueError('实例磁盘空间不足。')
    latest = fleet.local_settings()['config']
    if any(config.get(k) != latest.get(k) for k in POLICY_FIELDS):
        raise ValueError('检查期间实例下载策略发生变化，请重新开始。')
    return {'ready': True, 'instance_id': fleet.id}, 200
