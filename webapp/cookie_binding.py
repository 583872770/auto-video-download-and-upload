"""Durable instance ownership of Cookie credentials, including custom files.

Bindings survive deletion/offline nodes: an old task may still hold a snapshot.
Only hashes are stored; independently issued sessions cannot prove account identity.
"""
import hashlib
import json

from sqlalchemy import select
from Utils.youtube_login import parse_youtube_cookie_file
from webapp.database import cookie_bindings, instances


class CookieBindingConflict(ValueError):
    pass


def credential_keys(data):
    cookies = parse_youtube_cookie_file(data)
    # Check every stable login token, rather than only the first available SID.
    names = {'SID', '__Secure-1PSID', '__Secure-3PSID', 'SAPISID',
             '__Secure-1PAPISID', '__Secure-3PAPISID'}
    tokens = {(c['name'], c['value']) for c in cookies if c['name'] in names and c['value']}
    if not tokens:
        tokens = {('file', json.dumps(sorted((c['domain'], c['name'], c['value']) for c in cookies)))}
    return sorted({hashlib.sha256(json.dumps(token).encode()).hexdigest() for token in tokens})


def claim(db, scope, keys):
    from sqlalchemy.dialects.mysql import insert as mysql_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    mysql = db.dialect.name == 'mysql'
    known = dict(db.execute(select(cookie_bindings.c.credential, cookie_bindings.c.scope).where(
        cookie_bindings.c.credential.in_(keys))).all())
    for key in sorted(keys):
        owner = known.get(key)
        if owner is None:
            statement = (mysql_insert if mysql else sqlite_insert)(cookie_bindings).values(credential=key, scope=scope)
            statement = (statement.on_duplicate_key_update(credential=cookie_bindings.c.credential) if mysql
                         else statement.on_conflict_do_nothing())
            db.execute(statement)
            owner = db.execute(select(cookie_bindings.c.scope).where(
                cookie_bindings.c.credential == key).with_for_update()).scalar_one()
        if owner != scope:
            name = db.execute(select(instances.c.name).where(instances.c.id == owner)).scalar_one_or_none()
            raise CookieBindingConflict(f'此 Cookie 已绑定实例「{name or owner}」，不能在当前实例重复使用。'
                                        '请为当前实例上传独立的 Cookie；删除 Cookie 或实例离线不会解除绑定。')
