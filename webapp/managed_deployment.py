"""Admin-only, selected-executor deployment management; never expose secrets."""
import hashlib
import json
from urllib.parse import urlsplit

from flask import g, jsonify, request
from werkzeug.exceptions import Conflict, Forbidden

from webapp.deployment import validate_profile, test_mysql, write_profile


def validate_managed(data, previous):
    if not isinstance(data, dict):
        raise ValueError('请提交有效的部署配置。')
    data = dict(data)
    data.pop('revision', None)
    enabled = data.pop('redis_enabled', bool(previous.get('redis_url')))
    if type(enabled) is not bool:
        raise ValueError('Redis 开关必须为布尔值。')
    redis_url = data.pop('redis_url', '')
    if not isinstance(redis_url, str) or len(redis_url) > 4096:
        raise ValueError('Redis 连接地址必须为有效文本。')
    redis_url = (redis_url.strip() or previous.get('redis_url', '')) if enabled else ''
    if enabled:
        try:
            parsed = urlsplit(redis_url)
            if (parsed.scheme not in ('redis', 'rediss') or not parsed.hostname or parsed.fragment
                    or parsed.query or any(char in redis_url for char in '\0\r\n')
                    or parsed.path not in ('', '/') and not parsed.path.lstrip('/').isdigit()
                    or parsed.port is not None and not 1 <= parsed.port <= 65535):
                raise ValueError
        except ValueError:
            raise ValueError('Redis 地址请填写 redis:// 或 rediss:// 连接地址，数据库编号为非负整数。') from None
    profile = validate_profile(data, previous)
    profile['redis_url'] = redis_url
    return profile


def test_connections(profile):
    test_mysql(profile)
    if profile.get('redis_url'):
        client = None
        try:
            import redis
            client = redis.Redis.from_url(profile['redis_url'], socket_connect_timeout=3, socket_timeout=3,
                                         retry_on_timeout=False)
            client.ping()
        except Exception:
            raise ValueError('Redis 连接失败，请检查地址、账号密码、数据库编号与网络。') from None
        finally:
            if client is not None:
                client.close()


def revision(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def public_profile(profile):
    config = {key: value for key, value in profile.items() if key not in ('mysql_password', 'redis_url')}
    config.update(mysql_password='', redis_url='', redis_enabled=bool(profile.get('redis_url')))
    address = ''
    try:
        redis = urlsplit(profile.get('redis_url') or '')
        if redis.hostname:
            host = f'[{redis.hostname}]' if ':' in redis.hostname else redis.hostname
            address = f'{redis.scheme}://{host}:{redis.port or 6379}{redis.path}'
    except ValueError:
        address = '已配置，请测试连接确认地址格式'
    return dict(config=config, mysql_password_configured=bool(profile.get('mysql_password')),
                redis_address=address,
                revision=revision(profile))


def configuration(gateway, identity):
    with gateway.lock:
        return dict(**public_profile(gateway.profile), instance_id=identity,
                    pending_restart=gateway.pending_restart, running=gateway.active is not None)


def update(gateway, data):
    if not gateway.lock.acquire(blocking=False):
        raise Conflict('部署配置正在保存或服务正在初始化，请稍后再试。')
    try:
        if not isinstance(data, dict):
            raise ValueError('请提交有效的部署配置。')
        if data.get('revision') != revision(gateway.profile):
            raise Conflict('部署配置已被其他页面修改，请重新读取后再保存。')
        value = validate_managed(data, gateway.profile)
        gateway.resolve_identity(value)
        test_connections(value)
        try:
            write_profile(gateway.managed_path, dict(version=1, profile=value))
        except OSError:
            raise ValueError('部署配置无法保存，请检查目标实例状态目录的写入权限。') from None
        gateway.profile = value
        gateway.pending_restart = value != gateway.running_profile
        return dict(message='所选实例部署配置已保存；重启该实例服务后生效，当前下载继续使用原连接。',
                    pending_restart=gateway.pending_restart)
    finally:
        gateway.lock.release()


def register_instance_deployment(app):
    def dispatch(test=False):
        request.max_content_length = 16384
        if not g.fleet_rpc and g.console_user and g.console_user['role'] != 'admin':
            raise Forbidden('部署设置需要管理员权限。')
        fleet = app.extensions.get('fleet')
        if fleet and not g.fleet_rpc:
            from webapp.fleet_routes import selected_id, rpc
            if selected_id() != fleet.id:
                return jsonify(rpc(fleet, selected_id(), request.path, method='GET' if request.method == 'HEAD' else request.method,
                                   data=request.get_json(silent=True) if request.method not in ('GET', 'HEAD') else None))
        gateway = app.extensions.get('deployment_gateway')
        if gateway is None:
            raise Conflict('此实例未通过部署网关启动或版本不支持，请升级并按标准启动方式运行该实例。')
        identity = fleet.id if fleet else None
        if request.method in ('GET', 'HEAD'):
            return jsonify(configuration(gateway, identity))
        data = request.get_json(silent=True)
        if test:
            with gateway.lock:
                value = validate_managed(data, gateway.profile)
            test_connections(value)
            return jsonify(message='所选实例 MySQL 连接成功；Redis 已通过检查或处于关闭状态。', instance_id=identity)
        return jsonify(**update(gateway, data), instance_id=identity)

    app.add_url_rule('/api/instance-deployment', 'instance_deployment', dispatch, methods=['GET', 'PUT'])
    app.add_url_rule('/api/instance-deployment/test', 'instance_deployment_test',
                     lambda: dispatch(test=True), methods=['POST'])
