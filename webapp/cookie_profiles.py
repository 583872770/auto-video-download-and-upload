"""Database-backed Cookie library; files are disposable yt-dlp caches."""
import datetime
import hashlib
import json
from pathlib import Path
import re
import tempfile
import uuid

from sqlalchemy import select
from Utils.config import resolve_path
from Utils.youtube_login import parse_youtube_cookie_file, save_youtube_cookies
from webapp.database import cookie_profiles as table, cookie_bindings, youtube_account_cookies, youtube_accounts
from webapp.cookie_binding import claim, credential_keys, CookieBindingConflict


class CookieProfiles:
    def __init__(self, base, store):
        self.base, self.store = Path(base), store
        self.directory = self.base / 'Cookies' / 'profiles'
        self.scope = self.scope_for(base, store)
        self._migrate()

    @staticmethod
    def scope_for(base, store):
        return store.fleet.id if store.fleet else hashlib.sha256(str(Path(base).resolve()).encode()).hexdigest()

    def path(self, identity):
        if identity == 'legacy':
            return self.base / 'Cookies' / 'youtube.txt'
        if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
            raise ValueError('Cookie 配置不存在。')
        return self.directory / (identity + '.txt')

    def _where(self, identity=None):
        condition = table.c.scope == self.scope
        return condition & (table.c.id == identity) if identity is not None else condition

    def _migrate(self):
        # Insert only: stale caches must never overwrite authoritative DB data.
        candidates = [('legacy', self.path('legacy'), None)]
        candidates += [(p.stem, p.with_suffix('.txt'), p) for p in self.directory.glob('*.json')
                       if re.fullmatch(r'[a-f0-9]{32}', p.stem)]
        candidates = [(identity, target, metadata) for identity, target, metadata in candidates if target.is_file()]
        if not candidates:
            return
        with self.store.connect() as db:
            existing = set(db.execute(select(table.c.id).where(
                self._where() & table.c.id.in_([identity for identity, _, _ in candidates]))).scalars())
        for identity, target, metadata in candidates:
            if identity in existing:
                continue
            item = json.loads(metadata.read_text(encoding='utf-8')) if metadata else {
                'name': '原有 Cookie（上传 / 浏览器登录）', 'created_at': None}
            # Preserve even expired historical data; validation remains at use time.
            content = target.read_text(encoding='utf-8-sig')
            if len(content.encode('utf-8')) > 1024 * 1024:
                raise ValueError('已有 Cookie 文件超过 1 MB，无法迁移。')
            with self.store.connect() as db:
                db.execute(self.store.db.ignore(table, dict(scope=self.scope, id=identity,
                    name=item['name'], content=content, created_at=item.get('created_at') or
                    datetime.datetime.now(datetime.timezone.utc).isoformat(), deleted=0)))
            try:
                self.bind(content.encode('utf-8'))
            except ValueError:
                pass  # Historical conflicts/invalid files remain manageable.

    def items(self, active=''):
        current = resolve_path(self.base, active).resolve() if active else None
        with self.store.connect() as db:
            source = table.outerjoin(youtube_account_cookies,
                (youtube_account_cookies.c.instance_id == table.c.scope) & (youtube_account_cookies.c.profile_id == table.c.id))
            source = source.outerjoin(youtube_accounts, youtube_accounts.c.id == youtube_account_cookies.c.account_id)
            rows = db.execute(select(table.c.id, table.c.name, table.c.created_at,
                youtube_accounts.c.id.label('account_id'), youtube_accounts.c.email).select_from(source).where(
                self._where() & (table.c.deleted == 0)).order_by(table.c.created_at, table.c.id)).mappings().all()
        return [{**row, 'instance_id': self.scope, 'active': self.path(row['id']).resolve() == current} for row in rows]

    def add(self, name, data, filename='cookies.txt'):
        name = str(name or '').strip() or str(filename or 'cookies.txt').replace('\\', '/').rsplit('/', 1)[-1]
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError('Cookie 名称需为 1～80 字，不支持控制字符；文件名过长时请填写名称。')
        cookies = parse_youtube_cookie_file(data)
        if len(self.items()) >= 50:
            raise ValueError('最多保存 50 份 Cookie，请先删除不再使用的配置。')
        with tempfile.TemporaryDirectory() as staging:
            normalized = Path(staging) / 'cookie.txt'
            save_youtube_cookies(cookies, normalized)
            content = normalized.read_text(encoding='utf-8')
        identity = uuid.uuid4().hex
        item = dict(id=identity, name=name, created_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        with self.store.connect() as db:
            claim(db, self.scope, credential_keys(content.encode()))
            db.execute(table.insert().values(scope=self.scope, **item, content=content, deleted=0))
        return {**item, 'active': False}

    def save_legacy(self, data):
        parse_youtube_cookie_file(data)
        with self.store.connect() as db:
            claim(db, self.scope, credential_keys(data))
            db.execute(self.store.db.upsert(table, dict(scope=self.scope, id='legacy',
                name='原有 Cookie（上传 / 浏览器登录）', content=data.decode('utf-8-sig'),
                created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), deleted=0), ['scope', 'id']))

    def export(self, identity):
        """Read the saved credential without touching its disposable cache."""
        self.path(identity)  # Validate the profile ID, never accept a file path.
        with self.store.connect() as db:
            row = db.execute(select(table.c.name, table.c.content).where(
                self._where(identity) & (table.c.deleted == 0))).mappings().first()
        if row is None:
            raise ValueError('Cookie 配置不存在。')
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', row['name']).strip(' .')[:80]
        if name.lower().endswith('.txt'):
            name = name[:-4]
        filename = f'{name or "youtube-cookie"}-{identity[:8]}.txt'
        return filename, row['content'].encode('utf-8')

    def require(self, identity):
        target = self.path(identity)
        with self.store.connect() as db:
            content = db.execute(select(table.c.content).where(
                self._where(identity) & (table.c.deleted == 0))).scalar_one_or_none()
        if content is None:
            raise ValueError('Cookie 配置不存在。')
        try:
            self.bind(content.encode('utf-8'))
        except CookieBindingConflict:
            raise
        except ValueError:
            pass  # Preserve historical/expired caches for management; execution validates them.
        return self._cache(identity, content)

    def _cache(self, identity, content):
        target = self.path(identity)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = content.encode('utf-8')
        if not target.is_file() or target.read_bytes() != data:
            # Atomic private cache, including when restoring after redeployment.
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.cookie-', delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
            try:
                temporary.chmod(0o600)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        target.chmod(0o600)
        return target

    def pool_members(self, identities, *, strict=True):
        """Read live membership and immutable ownership in two bulk queries.

        Never cache execution authorization. Only historical, previously unbound
        credentials need a claim/write; normal status polling remains read-only.
        """
        identities = list(dict.fromkeys(identities))
        members, unavailable = [], []
        with self.store.connect() as db:
            rows = {row['id']: dict(row) for row in db.execute(select(
                table.c.id, table.c.name, table.c.content).where(
                    self._where() & (table.c.deleted == 0) & table.c.id.in_(identities))).mappings()}
            keys_by_id = {}
            for identity in identities:
                try:
                    self.path(identity)
                    if identity not in rows:
                        raise ValueError('Cookie 配置不存在。')
                    keys_by_id[identity] = credential_keys(rows[identity]['content'].encode('utf-8'))
                except ValueError as error:
                    if strict:
                        raise
                    unavailable.append({'id': identity, 'name': rows.get(identity, {}).get('name', identity),
                                        'unavailable': True})
            keys = sorted({key for values in keys_by_id.values() for key in values})
            owners = dict(db.execute(select(cookie_bindings.c.credential, cookie_bindings.c.scope).where(
                cookie_bindings.c.credential.in_(keys))).all()) if keys else {}
            for identity, values in keys_by_id.items():
                try:
                    if any(owners.get(key) != self.scope for key in values):
                        # claim() handles the concurrent first-use race and gives
                        # the usual, actionable binding-conflict explanation.
                        with db.begin_nested():
                            claim(db, self.scope, values)
                    members.append(rows[identity])
                except CookieBindingConflict:
                    if strict:
                        raise
                    unavailable.append({'id': identity, 'name': rows[identity]['name'], 'unavailable': True})
        ready = []
        for row in members:
            try:
                ready.append({**row, 'path': self._cache(row['id'], row['content'])})
            except OSError:
                if strict:
                    raise
                unavailable.append({'id': row['id'], 'name': row['name'], 'unavailable': True})
        return ready, unavailable

    def restore_selected(self, value):
        if not value:
            return
        target = resolve_path(self.base, value).resolve()
        if target == self.path('legacy').resolve():
            identity = 'legacy'
        elif target.parent == self.directory.resolve() and re.fullmatch(r'[a-f0-9]{32}', target.stem):
            identity = target.stem
        else:
            with target.open('rb') as source:
                self.bind(source.read(1024 * 1024 + 1))
            return
        self.require(identity)

    def bind(self, data):
        keys = credential_keys(data)
        with self.store.connect() as db:
            claim(db, self.scope, keys)

    def delete(self, identity):
        # Conflicting historical profiles must still be removable/exportable.
        target = self.path(identity)
        self.export(identity)  # Validate a live scoped record, without using it.
        # Remove caches before committing the tombstone. If cleanup fails, the
        # database still owns the credentials and can restore the selected file.
        target.unlink(missing_ok=True)
        if identity != 'legacy':
            target.with_suffix('.json').unlink(missing_ok=True)
        with self.store.connect() as db:
            # Tombstone prevents any leftover cache being re-imported after a crash.
            db.execute(table.update().where(self._where(identity)).values(deleted=1, content=''))
