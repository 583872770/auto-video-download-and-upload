"""Database-backed Cookie library; files are disposable yt-dlp caches."""
import hashlib
from pathlib import Path
import re
import tempfile

from sqlalchemy import select
from Utils.config import resolve_path
from webapp.database import (cookie_profiles as table, cookie_bindings, youtube_account_cookies,
    youtube_accounts, youtube_profile_auth)
from webapp.cookie_binding import claim, credential_keys, CookieBindingConflict


class CookieProfiles:
    def __init__(self, base, store):
        self.base, self.store = Path(base), store
        self.directory = self.base / 'Cookies' / 'profiles'
        self.scope = self.scope_for(base, store)

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

    def _account_link(self):
        return select(youtube_account_cookies.c.account_id).join(youtube_accounts,
            youtube_accounts.c.id == youtube_account_cookies.c.account_id).where(
                youtube_account_cookies.c.instance_id == table.c.scope,
                youtube_account_cookies.c.profile_id == table.c.id,
                youtube_accounts.c.instance_id == table.c.scope).exists()

    def items(self, active=''):
        current = resolve_path(self.base, active).resolve() if active else None
        with self.store.connect() as db:
            source = table.outerjoin(youtube_account_cookies,
                (youtube_account_cookies.c.instance_id == table.c.scope) & (youtube_account_cookies.c.profile_id == table.c.id))
            source = source.outerjoin(youtube_accounts, youtube_accounts.c.id == youtube_account_cookies.c.account_id)
            rows = db.execute(select(table.c.id, table.c.name, table.c.created_at,
                youtube_accounts.c.id.label('account_id'), youtube_accounts.c.email).select_from(source).where(
                self._where() & (table.c.deleted == 0) & self._account_link()).order_by(table.c.created_at, table.c.id)).mappings().all()
        return [{**row, 'instance_id': self.scope, 'active': self.path(row['id']).resolve() == current} for row in rows]

    def add(self, name, data, filename='cookies.txt'):
        raise ValueError('请先新增或导入账号，再从该账号登录或更新 Cookie。')

    def save_legacy(self, data):
        raise ValueError('不再支持无账号 Cookie，请从账号管理更新凭据。')

    def export(self, identity):
        """Read the saved credential without touching its disposable cache."""
        self.path(identity)  # Validate the profile ID, never accept a file path.
        with self.store.connect() as db:
            row = db.execute(select(table.c.name, table.c.content).where(
                self._where(identity) & (table.c.deleted == 0) & self._account_link())).mappings().first()
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
                self._where(identity) & (table.c.deleted == 0) & self._account_link())).scalar_one_or_none()
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
                    self._where() & (table.c.deleted == 0) & self._account_link() & table.c.id.in_(identities))).mappings()}
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
            raise ValueError('请使用账号管理中已登录账号的 Cookie，不支持独立 Cookie 路径。')
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
        from webapp.account_library import AccountLibrary
        with AccountLibrary(self.store, self.base).transaction() as db:
            # Tombstone prevents any leftover cache being re-imported after a crash.
            db.execute(table.update().where(self._where(identity)).values(deleted=1, content=''))
            account_ids = select(youtube_account_cookies.c.account_id).where(
                youtube_account_cookies.c.instance_id == self.scope, youtube_account_cookies.c.profile_id == identity)
            db.execute(youtube_accounts.update().where(youtube_accounts.c.id.in_(account_ids)).values(
                version=youtube_accounts.c.version + 1))
            db.execute(youtube_profile_auth.delete().where(youtube_profile_auth.c.instance_id == self.scope,
                youtube_profile_auth.c.profile_id == identity))
