"""Youtube HTTP endpoints."""
from io import BytesIO
from pathlib import Path
from flask import g, jsonify, request, send_file
from Utils.youtube_login import parse_youtube_cookie_file
from webapp.helpers import get_body, json_result


def register_youtube_routes(context, cookies, preflight):
    app = context.app
    install_cookie = cookies.install_cookie
    media_lock = context.media_lock
    require_login_idle = cookies.require_login_idle
    settings = context.settings
    settings_lock = context.settings_lock
    start_browser_login = cookies.start_browser_login
    store = context.store
    verify_youtube = preflight.verify_youtube
    youtube_auth = context.youtube_auth
    youtube_login = context.app.extensions['youtube_login']
    body = get_body

    @app.post('/api/youtube/auth/check')
    def check_youtube_auth():
        with media_lock:
            require_login_idle()
            config, base = settings()
            result = verify_youtube({**config, 'download_methods': ['yt_dlp']}, base, ['https://www.youtube.com/'], force=True)
            youtube_auth.clear_block()
        return jsonify(auth=result, message=result['message'])

    @app.get('/api/youtube/account-login')
    @app.get('/api/youtube/login')
    def youtube_login_status():
        return jsonify(youtube_login.status())

    @app.post('/api/youtube/account-login')
    @app.post('/api/youtube/login')
    def start_youtube_login():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise ValueError('请提交 JSON 对象。')
        identity = data.get('account_id')
        if request.path == '/api/youtube/account-login' and not identity:
            raise ValueError('请选择需要登录的账号。')
        actor = None
        if identity:
            from werkzeug.exceptions import Forbidden
            user = getattr(g, 'console_user', None)
            if not getattr(g, 'fleet_rpc', False) and (not user or user['role'] != 'admin'):
                raise Forbidden('仅管理员可以使用已保存账号登录。')
            actor = (data.get('actor') or 'instance_rpc') if getattr(g, 'fleet_rpc', False) else user['username']
            if not isinstance(actor, str) or len(actor) > 64:
                raise ValueError('登录操作人无效。')
        return jsonify(start_browser_login(identity, data, actor)), 202

    @app.post('/api/youtube/account-login/cancel')
    @app.post('/api/youtube/login/cancel')
    def cancel_youtube_login():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise ValueError('请提交 JSON 对象。')
        if not data.get('account_id'):
            raise ValueError('请选择需要取消的账号登录。')
        from werkzeug.exceptions import Forbidden
        user = getattr(g, 'console_user', None)
        if not getattr(g, 'fleet_rpc', False) and (not user or user['role'] != 'admin'):
            raise Forbidden('仅管理员可以取消账号登录。')
        with youtube_login.lock:
            if data.get('account_id'):
                from werkzeug.exceptions import Conflict
                status = youtube_login.status()
                if (status.get('account') or {}).get('account_id') != data['account_id'] or status.get('id') != data.get('session_id'):
                    raise Conflict('登录窗口已变化，请刷新状态；未取消其他登录。')
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
        return json_result(cookies.list_youtube_cookies())

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
        return json_result(cookies.check_youtube_cookie_profile(body().get('id')))

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
        return json_result(cookies.recover_youtube_cookie_pool())

    @app.post('/api/youtube/cookies')
    def add_youtube_cookie():
        request.max_content_length = 2 * 1024 * 1024
        uploaded = request.files.get('cookie')
        if uploaded is None or not uploaded.filename or Path(uploaded.filename).suffix.lower() != '.txt':
            raise ValueError('请选择 Netscape 格式的 Cookie .txt 文件。')
        data = uploaded.stream.read(1024 * 1024 + 1)
        return json_result(cookies.add_youtube_cookie(data, request.form.get('name'),
            uploaded.filename, activate=request.form.get('activate') != 'false'))

    @app.post('/api/youtube/cookies/select')
    def select_youtube_cookie():
        return json_result(cookies.select_youtube_cookie(body().get('id')))

    @app.post('/api/youtube/cookies/delete')
    def delete_youtube_cookie():
        return json_result(cookies.delete_youtube_cookie(body().get('id')))
