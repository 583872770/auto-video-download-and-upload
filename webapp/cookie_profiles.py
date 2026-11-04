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
from webapp.database import cookie_profiles as table


class CookieProfiles:
    def __init__(self, base, store):
        self.base, self.store = Path(base), store
        self.directory = self.base / 'Cookies' / 'profiles'
        self.scope = store.fleet.id if store.fleet else hashlib.sha256(str(self.base.resolve()).encode()).hexdigest()
        self._migrate()

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
        for identity, target, metadata in candidates:
            if not target.is_file():
                continue
            with self.store.connect() as db:
                if db.execute(select(table.c.id).where(self._where(identity))).first():
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

    def items(self, active=''):
        current = resolve_path(self.base, active).resolve() if active else None
        with self.store.connect() as db:
            rows = db.execute(select(table.c.id, table.c.name, table.c.created_at).where(
                self._where() & (table.c.deleted == 0)).order_by(table.c.created_at, table.c.id)).mappings().all()
        return [{**row, 'active': self.path(row['id']).resolve() == current} for row in rows]

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
            db.execute(table.insert().values(scope=self.scope, **item, content=content, deleted=0))
        return {**item, 'active': False}

    def save_legacy(self, data):
        parse_youtube_cookie_file(data)
        with self.store.connect() as db:
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

    def restore_selected(self, value):
        if not value:
            return
        target = resolve_path(self.base, value).resolve()
        if target == self.path('legacy').resolve():
            identity = 'legacy'
        elif target.parent == self.directory.resolve() and re.fullmatch(r'[a-f0-9]{32}', target.stem):
            identity = target.stem
        else:
            return  # User-managed custom paths retain their existing behavior.
        self.require(identity)

    def delete(self, identity):
        target = self.require(identity)
        # Remove caches before committing the tombstone. If cleanup fails, the
        # database still owns the credentials and can restore the selected file.
        target.unlink(missing_ok=True)
        if identity != 'legacy':
            target.with_suffix('.json').unlink(missing_ok=True)
        with self.store.connect() as db:
            # Tombstone prevents any leftover cache being re-imported after a crash.
            db.execute(table.update().where(self._where(identity)).values(deleted=1, content=''))
