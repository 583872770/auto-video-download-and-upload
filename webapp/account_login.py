"""Account-bound browser sessions; credentials stay in the executor's memory."""
import time

from werkzeug.exceptions import Conflict

from webapp.account_library import AccountLibrary
from webapp.database import youtube_accounts


def prepare(store, base, config, identity, data, actor):
    lib = AccountLibrary(store, base, config)
    automatic = data.get('automatic', False)
    if type(automatic) is not bool or type(data.get('version')) is not int:
        raise ValueError('请重新读取账号，并选择登录方式。')
    with lib.transaction() as db:
        row = lib.get_row(db, identity, data['version'])
        target = row['instance_id'] or lib.validate_instance(db, data.get('instance_id'))
        if target != lib.local_id:
            raise Conflict('只能在账号绑定的下载实例登录；请先选择绑定实例。')
        lib.owner(db, row['normalized_email'], target)
        credentials = None
        if automatic:
            saved = lib.key(db).decrypt(row)
            password = saved.get('password')
            if not password:
                raise ValueError('此账号尚未保存密码，请使用手动登录或先保存密码。')
            credentials = {'email': row['email'], 'password': password, 'code_url': saved.get('code_url', '')}
        if not row['instance_id']:
            row['version'] += 1
            db.execute(youtube_accounts.update().where(youtube_accounts.c.id == identity).values(
                instance_id=target, version=row['version'], updated_at=time.time()))
            lib.record(db, identity, actor, 'bind_for_login')
        lib.record(db, identity, actor, 'browser_login_auto' if automatic else 'browser_login_manual')
        return {'account_id': identity, 'email': row['email'], 'version': row['version'],
                'instance_id': target, 'actor': actor, 'automatic': automatic}, credentials


def install(store, base, config, data, account, account_name):
    """Commit the new Cookie and association together, preserving pool/config."""
    from Utils.youtube_login import parse_youtube_cookie_file

    parse_youtube_cookie_file(data)
    if not isinstance(account_name, str) or account_name.strip().lower() != account['email'].lower():
        raise ValueError('登录邮箱与所选账号不一致或无法确认邮箱，未保存或关联 Cookie，请重新登录。')
    lib = AccountLibrary(store, base, config)
    if account['instance_id'] != lib.local_id:
        raise Conflict('只能在账号绑定的实例保存登录结果。')
    lib.save_cookie(account['account_id'], account['version'], data, account['actor'],
                    target=lib.local_id, expected_email=account['email'], action='browser_login_saved')
    return 'YouTube 登录成功，已保存此账号唯一的 Cookie；重新登录会替换旧凭据，账号池选择和冷却保留。'


def status_for(status, identity):
    if (status.get('account') or {}).get('account_id') == identity:
        return {**status, 'can_cancel': status['active'] and status['state'] not in ('committing', 'cancelling')}
    return {'state': 'busy' if status['active'] else 'idle', 'active': status['active'],
            'can_cancel': False, 'message': '该实例已有其他登录窗口，请完成或取消后再试。' if status['active'] else '尚未启动此账号的浏览器登录。'}
