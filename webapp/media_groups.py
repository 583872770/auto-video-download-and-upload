"""Page the library by source video, with complete summaries and lazy file lists."""
import base64
from datetime import datetime
import json

from sqlalchemy import Float, String, and_, case, func, literal, or_, select, union_all

from webapp.database import delivery_artifacts as artifacts, media
from webapp.delivery.listing import PUBLIC_FIELDS


KINDS = ('av', 'video_only', 'audio_only', 'subtitle', 'source_video')


def _cursor(value, length):
    try:
        if len(value) > 8192:
            raise ValueError()
        result = json.loads(base64.b64decode(value.encode(), altchars=b'-_', validate=True))
        if not isinstance(result, list) or len(result) != length or not all(isinstance(v, str) for v in result):
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('素材分页游标无效。') from None


def _encode(values):
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def _scope(catalog, root, query=''):
    """Union only catalog metadata; host paths and object keys never leave SQL."""
    instance = catalog.store.fleet.id if catalog.store.fleet else 'local'
    local = catalog.selection(root, exclude_delivery=True).subquery()
    delivery = select(*PUBLIC_FIELDS).where(artifacts.c.instance_id == instance,
        artifacts.c.available == 1, artifacts.c.test_mode == 0).subquery()

    def projection(table, source):
        video_id = func.nullif(table.c.video_id, '')
        key = case((video_id.is_not(None), literal('video:') + video_id),
                   else_=literal(source + ':') + table.c.id)
        title = func.coalesce(func.nullif(table.c.title, ''), video_id,
                              table.c.name if source == 'media' else table.c.id)
        matched = or_(title.contains(query, autoescape=True),
                      table.c.video_id.contains(query, autoescape=True))
        if source == 'media':
            matched = or_(matched, table.c.name.contains(query, autoescape=True))
        return select(key.label('group_key'), (literal(source + ':') + table.c.id).label('file_key'),
            table.c.id, func.substr(title, 1, 512).label('title'), video_id.label('video_id'),
            (literal('source_video') if source == 'media' else table.c.kind).label('kind'),
            table.c.size,
            (table.c.downloaded_at if source == 'media' else literal(None, Float)).label('local_at'),
            (literal(None, String) if source == 'media' else table.c.created_at).label('artifact_at'),
            case((matched, 1), else_=0).label('matched'))

    return union_all(projection(local, 'media'), projection(delivery, 'artifact')).subquery()


def page(catalog, root, *, query='', kind='all', cursor=None, limit=24):
    if kind not in ('all', *KINDS):
        raise ValueError('请选择有效素材类型。')
    if not 1 <= limit <= 100:
        raise ValueError('每页标题组数必须在 1 到 100 之间。')
    files = _scope(catalog, root, query[:200])
    groups = select(files.c.group_key, func.min(files.c.title).label('title'),
        func.lower(func.min(files.c.title)).label('sort_title'),
        func.max(files.c.video_id).label('video_id'), func.count().label('file_count'),
        func.sum(files.c.size).label('size'), func.max(files.c.local_at).label('local_at'),
        func.max(files.c.artifact_at).label('artifact_at'),
        *(func.sum(case((files.c.kind == name, 1), else_=0)).label(name) for name in KINDS)
    ).group_by(files.c.group_key).having(func.max(files.c.matched) == 1).subquery()
    statement = select(groups)
    if kind != 'all':
        statement = statement.where(groups.c[kind] > 0)
    with catalog.store.connect() as db:
        totals = db.execute(select(func.count().label('all'),
            func.coalesce(func.sum(groups.c.file_count), 0).label('file_count'),
            func.coalesce(func.sum(groups.c.size), 0).label('size'),
            func.coalesce(func.sum(groups.c.subtitle), 0).label('subtitle_files'),
            *(func.coalesce(func.sum(case((groups.c[name] > 0, 1), else_=0)), 0).label(name) for name in KINDS)
        ).select_from(groups)).mappings().one()
        total = int(totals[kind])
        if cursor:
            title, key = _cursor(cursor, 2)
            statement = statement.where(or_(groups.c.sort_title > title,
                and_(groups.c.sort_title == title, groups.c.group_key > key)))
        results = [dict(row) for row in db.execute(statement.order_by(
            groups.c.sort_title, groups.c.group_key).limit(limit + 1)).mappings()]
    items = results[:limit]
    next_cursor = _encode([items[-1]['sort_title'], items[-1]['group_key']]) if len(results) > limit else None
    for item in items:
        item['size'] = int(item['size'])
        kind_counts = {name: int(item.pop(name)) for name in KINDS}
        item['kind_counts'] = {name: count for name, count in kind_counts.items() if count}
        item['downloaded_at'] = max(float(item.pop('local_at') or 0),
            datetime.fromisoformat(item['artifact_at']).timestamp() if item['artifact_at'] else 0) or None
        item.pop('artifact_at')
        item.pop('sort_title')
    return dict(items=items, total=total, next_cursor=next_cursor,
                counts={name: int(totals[name]) for name in ('all', *KINDS)},
                file_count=int(totals['file_count']), size=int(totals['size']),
                subtitle_files=int(totals['subtitle_files']))


def group_files(catalog, root, *, key, cursor=None, limit=50):
    if not key.startswith(('video:', 'media:', 'artifact:')) or len(key) > 320:
        raise ValueError('素材分组无效。')
    if not 1 <= limit <= 100:
        raise ValueError('每页文件条数必须在 1 到 100 之间。')
    files = _scope(catalog, root)
    statement = select(files.c.file_key, files.c.id).where(files.c.group_key == key)
    with catalog.store.connect() as db:
        total = db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
        if cursor:
            statement = statement.where(files.c.file_key > _cursor(cursor, 1)[0])
        rows = list(db.execute(statement.order_by(files.c.file_key).limit(limit + 1)).mappings())
        selected = rows[:limit]
        local_ids = [row['id'] for row in selected if row['file_key'].startswith('media:')]
        artifact_ids = [row['id'] for row in selected if row['file_key'].startswith('artifact:')]
        local_fields = (media.c.id, media.c.name, media.c.title, media.c.size,
                        media.c.downloaded_at, media.c.video_id, media.c.cover, media.c.playable)
        local = {row['id']: dict(row) for row in db.execute(select(*local_fields).where(media.c.id.in_(local_ids))).mappings()} if local_ids else {}
        outputs = {row['id']: dict(row) for row in db.execute(select(*PUBLIC_FIELDS).where(artifacts.c.id.in_(artifact_ids))).mappings()} if artifact_ids else {}
    instance = catalog.store.fleet.id if catalog.store.fleet else None
    items = []
    for row in selected:
        if row['file_key'].startswith('media:'):
            item = local[row['id']]
            item.update(playable=bool(item['playable']), instance_id=instance)
            items.append(dict(id=row['file_key'], kind='source_video', size=item['size'],
                              downloaded_at=item['downloaded_at'], local=item))
        else:
            item = outputs[row['id']]
            item['content_url'] = '/api/delivery/files/' + item['id']
            items.append(dict(id=row['file_key'], kind=item['kind'], size=item['size'],
                              downloaded_at=item['created_at'], artifact=item))
    return dict(items=items, total=total,
                next_cursor=_encode([selected[-1]['file_key']]) if len(rows) > limit else None)
