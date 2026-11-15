"""Task-owned storage rules, validated before an execution can pin them."""
from pathlib import PurePosixPath, PureWindowsPath

from webapp.config.schema import SETTINGS

FIELDS = frozenset(key for key, spec in SETTINGS.items() if spec.scope == 'task')
SECRETS = frozenset(('oss_access_key_secret', 'oss_security_token'))


def defaults():
    return {key: SETTINGS[key].default_value() for key in FIELDS}


def normalize(value=None, previous=None):
    value = dict(value) if isinstance(value, dict) else {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError('请提交有效的任务保存配置。')
    for key in SECRETS:
        value.pop(key + '_configured', None)
    if set(value) - FIELDS:
        raise ValueError('任务保存配置包含不支持的字段。')
    result = {**defaults(), **(previous or {}), **value}
    for key in SECRETS:
        if not result[key] and previous:
            result[key] = previous.get(key, '')
    for key in FIELDS:
        spec, raw = SETTINGS[key], result[key]
        if spec.limits:
            low, high = spec.limits
            if type(raw) is not int or not low <= raw <= high:
                raise ValueError(f'{key} 必须为 {low}～{high} 的整数。')
        elif type(raw) is not spec.value_type or isinstance(raw, str) and (len(raw) > 4096 or any(ord(c) < 32 for c in raw)):
            raise ValueError(f'{key} 格式不正确。')
    if result['storage_mode'] not in ('local', 'oss', 'test'):
        raise ValueError('请选择本地保存、OSS 上传或测试处理。')
    result['path'] = result['path'].strip()
    if not result['path']:
        raise ValueError('请填写本任务的素材根目录。')
    directory = result['dir'].strip().replace('\\', '/')
    if (PurePosixPath(directory).is_absolute() or PureWindowsPath(directory).drive
            or any(part in ('.', '..') for part in directory.split('/') if part)):
        raise ValueError('任务子目录必须为根目录下的相对路径，不能包含 . 或 ..。')
    result['dir'] = directory.strip('/')
    if result['storage_mode'] == 'oss':
        from Utils.oss_storage import validate_oss
        checked = validate_oss({**result, '_task_storage': True})
        result.update({key: checked[key] for key in FIELDS})
    return result


def public(value):
    result = dict(value)
    for key in SECRETS:
        result[key + '_configured'] = bool(result.get(key))
        result[key] = ''
    return result


def public_spec(spec):
    return {**spec, 'storage': public(spec['storage'])}


def require_edit(value, previous=None):
    from flask import g, has_request_context
    from werkzeug.exceptions import Forbidden
    user = getattr(g, 'console_user', None) if has_request_context() else None
    if user and user['role'] != 'admin' and value != (previous if previous is not None else defaults()):
        raise Forbidden('文件保存配置需要管理员修改；你仍可编辑本任务的交付要求。')
