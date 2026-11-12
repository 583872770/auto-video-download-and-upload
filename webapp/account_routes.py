"""Global, human-authenticated account APIs; execution stays on the owner."""
import hashlib

from flask import g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import Conflict, Forbidden
from sqlalchemy import select

from webapp.account_import import parse_import
from webapp.account_library import AccountLibrary
from webapp.database import cookie_profiles

PREFIX = '/api/account-library/youtube'


def register_accounts(app):
    store = app.extensions['task_store']
    signer = URLSafeTimedSerializer(app.secret_key, salt='youtube-account-import-v1')

    def library():
        config, base = app.extensions['settings_func']()
        return AccountLibrary(store, base, config)

    def admin():
        user = getattr(g, 'console_user', None)
        if getattr(g, 'fleet_rpc', False) or not user or user['role'] != 'admin':
            raise Forbidden('仅已登录的管理员可以修改或读取账号密钥。')
        return user['username']

    def body():
        request.max_content_length = 2 * 1024 * 1024
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('请提交 JSON 对象。')
        return value

    @app.get(PREFIX)
    def youtube_account_list():
        return jsonify(library().list(request.args))

    @app.get(PREFIX + '/cookies')
    def youtube_account_cookie_list():
        return jsonify(library().list(request.args, cookies=True))

    @app.get(PREFIX + '/overview')
    def youtube_account_overview():
        return jsonify(library().overview(request.args))

    @app.get(PREFIX + '/capabilities')
    def youtube_account_capabilities():
        configured, problem = False, ''
        try:
            lib = library()
            with store.connect() as db:
                lib.key(db)
            configured = True
        except ValueError as error:
            problem = str(error)
        return jsonify(encryption_ready=configured, message=problem)

    @app.get(PREFIX + '/choices')
    def youtube_account_choices():
        from webapp.database import youtube_accounts
        with store.connect() as db:
            rows = db.execute(select(youtube_accounts.c.id, youtube_accounts.c.email,
                youtube_accounts.c.instance_id, youtube_accounts.c.version).order_by(youtube_accounts.c.email)).mappings()
            return jsonify(items=[dict(r) for r in rows])

    @app.post(PREFIX)
    def youtube_account_add():
        actor = admin()
        identity = library().create(body(), actor)
        return jsonify(id=identity, message='账号资料已保存；尚未登录或加入账号池。'), 201

    @app.get(PREFIX + '/<identity>')
    def youtube_account_detail(identity):
        return jsonify(item=library().list({}, identity=identity))

    @app.patch(PREFIX + '/<identity>')
    def youtube_account_edit(identity):
        actor = admin()
        library().update(identity, body(), actor)
        return jsonify(message='账号资料已保存；原 Cookie 和冷却状态不变。')

    @app.delete(PREFIX + '/<identity>')
    def youtube_account_delete(identity):
        actor = admin()
        data = body()
        if type(data.get('version')) is not int:
            raise Conflict('缺少资料版本，请重新读取。')
        library().delete(identity, data['version'], actor)
        return jsonify(message='账号资料已删除；Cookie 凭据及实例独占归属保留。')

    @app.post(PREFIX + '/<identity>/secrets')
    def youtube_account_secret(identity):
        actor = admin()
        return jsonify(value=library().secrets(identity, body().get('field'), actor))

    @app.put(PREFIX + '/<identity>/cookies')
    def youtube_account_associate(identity):
        actor = admin()
        library().associate(identity, body(), actor)
        return jsonify(message='Cookie 关联已保存（用户确认），不会自动加入账号池。')

    def login_target(identity, data=None):
        lib = library()
        item = lib.list({}, identity=identity)
        target = item['instance_id'] or (data or {}).get('instance_id')
        with store.connect() as db:
            lib.validate_instance(db, target)
        if not target:
            raise ValueError('请先选择绑定实例。')
        return lib, item, target

    def login_rpc(lib, target, path, *, method='GET', data=None):
        fleet = app.extensions['fleet']
        if not fleet:
            raise ValueError('绑定实例不可用，不能在当前入口登录。')
        from webapp.fleet_routes import rpc
        return rpc(fleet, target, path, method=method, data=data)

    @app.post(PREFIX + '/<identity>/login')
    def youtube_account_login_start(identity):
        actor, data = admin(), body()
        lib, item, target = login_target(identity, data)
        if data.get('version') != item['version']:
            raise Conflict('账号资料已变化，请重新读取后登录。')
        if target == lib.local_id:
            result = app.extensions['start_account_browser_login'](identity, data, actor)
        else:
            result = login_rpc(lib, target, '/api/youtube/account-login', method='POST', data={
                'account_id': identity, 'version': data.get('version'), 'instance_id': target,
                'automatic': data.get('automatic', False), 'actor': actor})
        from webapp.account_login import status_for
        return jsonify(status_for(result, identity)), 202

    @app.get(PREFIX + '/<identity>/login')
    def youtube_account_login_status(identity):
        admin()
        lib, item, target = login_target(identity)
        result = (app.extensions['youtube_login'].status() if target == lib.local_id else
                  login_rpc(lib, target, '/api/youtube/account-login'))
        from webapp.account_login import status_for
        return jsonify(status_for(result, identity))

    @app.post(PREFIX + '/<identity>/login/cancel')
    def youtube_account_login_cancel(identity):
        admin()
        data = body()
        lib, item, target = login_target(identity)
        payload = {'account_id': identity, 'session_id': data.get('session_id')}
        if target == lib.local_id:
            manager = app.extensions['youtube_login']
            with manager.lock:
                status = manager.status()
                if (status.get('account') or {}).get('account_id') != identity or status.get('id') != payload['session_id']:
                    raise Conflict('登录窗口已变化，未取消其他登录。')
                result = manager.cancel()
        else:
            result = login_rpc(lib, target, '/api/youtube/account-login/cancel', method='POST', data=payload)
        from webapp.account_login import status_for
        return jsonify(status_for(result, identity))

    @app.post(PREFIX + '/import/preview')
    def youtube_account_import_preview():
        actor, data = admin(), body()
        mode = data.get('mode', 'skip')
        if mode not in ('skip', 'update'):
            raise ValueError('请选择重复账号处理方式。')
        rows = parse_import(data.get('text'))
        lib = library()
        with store.connect() as db:
            lib.validate_instance(db, data.get('instance_id'))
        preview, versions = lib.import_preview(rows, mode)
        token = signer.dumps({'digest':hashlib.sha256(data['text'].encode()).hexdigest(), 'versions':versions,
            'mode':mode, 'instance':data.get('instance_id'), 'actor':actor})
        return jsonify(items=preview, token=token, errors=sum(bool(r.get('error')) for r in rows))

    @app.post(PREFIX + '/import')
    def youtube_account_import_commit():
        actor, data = admin(), body()
        try:
            preview = signer.loads(data.get('token', ''), max_age=900)
        except (BadSignature, SignatureExpired, TypeError):
            raise Conflict('导入预览已过期或无效，请重新预览。') from None
        raw = data.get('text')
        rows = parse_import(raw)
        if preview['actor'] != actor or preview['digest'] != hashlib.sha256(raw.encode()).hexdigest():
            raise Conflict('导入内容或操作人已变化，请重新预览。')
        result = library().import_commit(rows, preview['versions'], preview['mode'], preview['instance'], actor)
        return jsonify(**result, message='导入完成；未改变已有账号的绑定、Cookie 或登录状态。')

    @app.post(PREFIX + '/<identity>/check')
    def youtube_account_check(identity):
        admin()
        data, lib = body(), library()
        item = lib.list({}, identity=identity)
        profile = next((p for p in item['cookies'] if p['id'] == data.get('profile_id')), None)
        if not profile:
            raise ValueError('请选择该账号已关联的 Cookie。')
        return execute_check(lib, profile['instance_id'], profile['id'])

    def execute_check(lib, target, profile):
        fleet = app.extensions['fleet']
        # Never use the caller's selected executor or fall back to this host.
        if target != lib.local_id:
            if not fleet:
                raise ValueError('绑定实例不在本机，无法代为检测。')
            from webapp.fleet_routes import rpc
            return jsonify(rpc(fleet, target, '/api/youtube/cookies/check', method='POST', data={'id':profile}))
        with app.extensions['media_lock'], app.extensions['settings_lock']:
            config, base = app.extensions['settings_func']()
            if app.extensions['youtube_login'].status().get('active'):
                raise Conflict('浏览器登录正在进行，请稍后检测。')
            options = {}
            if config.get('proxy_mode') == 'clash':
                snapshot = app.extensions['clash'].snapshot()
                options.update(proxy=snapshot['proxy'], node=snapshot['node'])
            value = app.extensions['youtube_auth'].check_profile(profile, config, base, recover=True, **options)
        return jsonify(auth=value, message=value['message'])

    def cookie_owner(target, profile):
        with store.connect() as db:
            row = db.execute(select(cookie_profiles.c.id).where(cookie_profiles.c.scope == target,
                cookie_profiles.c.id == profile, cookie_profiles.c.deleted == 0)).first()
        if not row:
            raise ValueError('该实例的 Cookie 不存在或已删除。')

    @app.post(PREFIX + '/cookie-action')
    def youtube_account_cookie_action():
        admin()
        data, lib = body(), library()
        target, profile = data.get('instance_id'), data.get('profile_id')
        cookie_owner(target, profile)
        if data.get('action') == 'check':
            return execute_check(lib, target, profile)
        raise ValueError('不支持的 Cookie 操作。')

    @app.get(PREFIX + '/cookies/<target>/<profile>/timeline')
    def youtube_account_history(target, profile):
        cookie_owner(target, profile)
        from webapp.youtube_cookie_history import list_events
        events = list_events(store, profile, limit=100)
        if profile == 'legacy':
            events = [e for e in events if e['metadata'].get('instance_id') == target
                or not store.fleet and target == library().local_id and e['metadata'].get('instance_id') is None]
        return jsonify(events=events)

    @app.post(PREFIX + '/<identity>/upload')
    def youtube_account_upload(identity):
        actor, lib = admin(), library()
        request.max_content_length = 2 * 1024 * 1024
        item = lib.list({}, identity=identity)
        if not item['instance_id'] or request.form.get('confirmed') != 'true':
            raise ValueError('请先绑定实例，并确认 Cookie 属于该账号。')
        try:
            version = int(request.form.get('version', ''))
        except ValueError:
            raise Conflict('缺少资料版本，请重新读取。') from None
        if version != item['version']:
            raise Conflict('账号资料已更新，请重新读取后上传。')
        uploaded = request.files.get('cookie')
        if uploaded is None or not uploaded.filename or not uploaded.filename.lower().endswith('.txt'):
            raise ValueError('请选择 Netscape Cookie TXT 文件。')
        raw = uploaded.stream.read(1024 * 1024 + 1)
        from Utils.youtube_login import parse_youtube_cookie_file
        parse_youtube_cookie_file(raw)
        target = item['instance_id']
        if target == lib.local_id:
            from webapp.cookie_profiles import CookieProfiles
            with app.extensions['settings_lock']:
                _, base = app.extensions['settings_func']()
                saved = CookieProfiles(base, store).add(request.form.get('name'), raw, uploaded.filename)
        else:
            fleet = app.extensions['fleet']
            if not fleet:
                raise ValueError('绑定实例不可用，不能在当前入口代存。')
            import requests
            node = fleet.resolve_target(target)
            with requests.Session() as http:
                http.trust_env = False
                try:
                    response = http.post(node['url'] + '/api/youtube/cookies',
                        headers={'Authorization':'Bearer ' + node['token']},
                        data={'name':request.form.get('name',''), 'activate':'false'},
                        files={'cookie':(uploaded.filename, raw, 'text/plain')}, timeout=(5,60), allow_redirects=False)
                    value = response.json()
                except (requests.RequestException, ValueError):
                    raise ValueError('绑定实例未确认保存结果，请刷新 Cookie 列表核实后再操作。') from None
                if response.status_code not in (200,201):
                    raise ValueError(value.get('error','绑定实例拒绝保存 Cookie。'))
                saved = value['item']
        try:
            lib.associate(identity, {'version':version, 'instance_id':target, 'confirmed':True,
                'profile_ids':[p['id'] for p in item['cookies']] + [saved['id']]}, actor)
        except (ValueError, Conflict):
            raise Conflict('Cookie 已保存在绑定实例，但关联未完成（资料可能已被修改）。请刷新，在 Cookie 列表手动关联；不要重复上传。') from None
        return jsonify(message='Cookie 已保存并关联账号；未自动加入账号池。', profile_id=saved['id']), 201
