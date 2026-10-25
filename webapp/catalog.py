"""A media index updated on completion; listing never traverses the filesystem."""
import base64
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from sqlalchemy import and_, func, or_, select

from webapp.database import media, roots
from webapp.media import FORMAT_TRACK, IMAGE_SUFFIXES, VIDEO_SUFFIXES
from webapp.store import now


class MediaCatalog:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def root_key(self, root):
        path = str(Path(root).resolve())
        identity = self.store.fleet.id if self.store.fleet else (self.store.single or {}).get('instance_id')
        return f'@{identity}|{path}' if identity else path

    @staticmethod
    def physical_root(key):
        return key.split('|', 1)[1] if key.startswith('@') and '|' in key else key

    def describe(self, path, root):
        path, root = Path(path).resolve(), Path(root).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            return None
        match = FORMAT_TRACK.fullmatch(path.stem)
        stem = match['stem'] if match else path.stem
        track = bool(match and (stem.endswith(']') or (path.parent / (stem + '.info.json')).is_file()
                               or any((path.parent / (stem + ext)).is_file() for ext in IMAGE_SUFFIXES)))
        if not track:
            stem = path.stem
        if track:
            for extension in ('.mp4', '.mkv', '.webm', '.mov'):
                final = path.parent / (stem + extension)
                if final.is_file():
                    path, track = final, False
                    break
        info_path = path.parent / (stem + '.info.json')
        info = {}
        if info_path.is_file():
            try:
                info = json.loads(info_path.read_text(encoding='utf-8'))
                if not isinstance(info, dict):
                    info = {}
            except (OSError, ValueError):
                pass
        cover = next((path.parent / (stem + ext) for ext in IMAGE_SUFFIXES if (path.parent / (stem + ext)).is_file()), None)
        stat = path.stat()
        logical = (path.parent.relative_to(root) / stem).as_posix()
        root_key = self.root_key(root)
        identity = hashlib.sha256((root_key + '\x00' + logical).encode()).hexdigest()
        title = str(info.get('title') or stem)
        return dict(id=identity, root=root_key, name=path.relative_to(root).as_posix(), logical_key=logical,
                    title=title, search_title=title.casefold()[:512], video_id=str(info.get('id') or '')[:256] or None,
                    size=stat.st_size, modified=stat.st_mtime, cover=cover.relative_to(root).as_posix() if cover else None,
                    playable=int(not track), status='incomplete' if track else 'ready',
                    prepared=int((path.parent / (path.name + '.parts')).is_dir()), scan_id=None, indexed_at=time.time())

    def upsert(self, items, scan_id=None):
        if not items:
            return
        if self.db.mysql and any(len(item['root']) > 512 for item in items):
            raise ValueError('MySQL 素材根目录不能超过 512 个字符，请缩短目录路径。')
        combined = {}
        for item in items:
            old = combined.get(item['id'])
            if old is None or item['playable'] > old['playable'] or item['name'].endswith('.mp4'):
                combined[item['id']] = {**item, 'scan_id': scan_id}
        with self.store.connect() as db:
            for root in sorted({item['root'] for item in combined.values()}):
                self.db.ensure(db, roots, {'root': root, 'count': 0, 'bytes': 0, 'scanned': 0})
                db.execute(select(roots.c.root).where(roots.c.root == root).with_for_update())
            previous = {row['id']: row for row in db.execute(select(media).where(media.c.id.in_(combined))).mappings()}
            for key, value in list(combined.items()):
                old = previous.get(key)
                if old and (Path(self.physical_root(old['root'])) / old['name']).is_file() and (old['playable'] > value['playable'] or
                            (not value['playable'] and old['name'].endswith('.mp4') and not value['name'].endswith('.mp4'))):
                    combined[key] = {**old, 'scan_id': scan_id}
            values = list(combined.values())
            db.execute(self.db.upsert(media, values, ['id']))
            for root in {item['root'] for item in values}:
                rows = [item for item in values if item['root'] == root]
                self.db.ensure(db, roots, {'root': root, 'count': 0, 'bytes': 0, 'scanned': 0})
                count_delta = sum(item['id'] not in previous for item in rows)
                bytes_delta = sum(item['size'] - (previous[item['id']]['size'] if item['id'] in previous else 0) for item in rows)
                db.execute(roots.update().where(roots.c.root == root).values(count=roots.c.count + count_delta,
                                                                           bytes=roots.c.bytes + bytes_delta))

    def index_file(self, path, root):
        item = self.describe(path, root)
        if item:
            self.upsert([item])

    def root_status(self, root):
        with self.store.connect() as db:
            row = db.execute(select(roots).where(roots.c.root == self.root_key(root))).mappings().first()
            return dict(row) if row else {'count': 0, 'bytes': 0, 'last_indexed': None, 'scanned': 0}

    def page(self, root, *, limit=24, cursor=None, query=''):
        if not 1 <= limit <= 100:
            raise ValueError('每页素材条数必须在 1 到 100 之间。')
        statement = select(media).where(media.c.root == self.root_key(root))
        if cursor:
            try:
                modified, identity = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                modified = float(modified)
                if not isinstance(identity, str) or len(identity) != 64:
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                raise ValueError('素材分页游标无效。') from None
            statement = statement.where(or_(media.c.modified < modified, and_(media.c.modified == modified, media.c.id < identity)))
        if query:
            escaped = query.casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            statement = statement.where(or_(media.c.search_title.startswith(escaped, escape='\\'), media.c.video_id == query))
        with self.store.connect() as db:
            rows = db.execute(statement.order_by(media.c.modified.desc(), media.c.id.desc()).limit(limit + 1)).mappings().all()
            items = [{**row, 'playable': bool(row['playable']), 'prepared': bool(row['prepared']),
                      'instance_id': self.store.fleet.id if self.store.fleet else None} for row in rows[:limit]]
        cursor = base64.urlsafe_b64encode(json.dumps([items[-1]['modified'], items[-1]['id']]).encode()).decode() if len(rows) > limit else None
        return {'items': items, 'next_cursor': cursor, 'total': self.root_status(root)['count']}

    def reindex(self, root, report=None):
        root = Path(root).resolve()
        root_key = self.root_key(root)
        scan_id = uuid.uuid4().hex
        started = time.time()
        with self.store.connect() as db:
            self.db.ensure(db, roots, {'root': root_key, 'count': 0, 'bytes': 0, 'scanned': 0})
            db.execute(roots.update().where(roots.c.root == root_key).values(scan_id=scan_id, scanned=0))
        pending, scanned = [], 0

        def walk(folder):
            if not folder.is_dir():
                return
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_symlink() or Path(entry.path).is_junction():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not entry.name.endswith('.parts') and not entry.name.startswith('.'):
                            yield from walk(Path(entry.path))
                    elif Path(entry.name).suffix.lower() in VIDEO_SUFFIXES:
                        yield Path(entry.path)
        for path in walk(root):
            item = self.describe(path, root)
            if item:
                pending.append(item)
            scanned += 1
            if len(pending) >= 100:
                self.upsert(pending, scan_id)
                pending.clear()
                if report:
                    report({'stage': 'indexing', 'scanned': scanned, 'title': '同步素材索引'})
        self.upsert(pending, scan_id)
        with self.store.connect() as db:
            stale = and_(media.c.root == root_key, media.c.indexed_at < started,
                         or_(media.c.scan_id != scan_id, media.c.scan_id.is_(None)))
            removed, size = db.execute(select(func.count(), func.coalesce(func.sum(media.c.size), 0)).where(stale)).one()
            db.execute(media.delete().where(stale))
            db.execute(roots.update().where(roots.c.root == root_key).values(count=roots.c.count - removed,
                bytes=roots.c.bytes - size, scanned=scanned, last_indexed=now(), scan_id=None))
        return {'message': f'素材索引同步完成，扫描 {scanned} 个文件。', 'scanned': scanned}
