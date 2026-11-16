"""Page the library by source video, with complete summaries and lazy file lists."""
import base64
import json
import math

from sqlalchemy import Float, and_, case, func, literal, literal_column, or_, select, union_all

from webapp.database import delivery_artifacts as artifacts, media
from webapp.delivery.listing import PUBLIC_FIELDS


KINDS = ('av', 'video_only', 'audio_only', 'subtitle', 'thumbnail', 'metadata', 'source_video')


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


def _position(value):
    stamp, key = _cursor(value, 2)
    try:
        stamp = float(stamp)
        if not math.isfinite(stamp):
            raise ValueError()
    except ValueError:
        raise ValueError('素材分页游标无效。') from None
    return stamp, key


def _artifact_time(column, sqlite):
    # Compare imported epoch timestamps and delivery ISO dates on the same UTC
    # timeline. Numeric offsets work without MySQL timezone tables.
    if sqlite:
        return func.strftime('%s', column).cast(Float)
    utc = func.convert_tz(func.replace(func.substr(column, 1, 19), 'T', ' '),
                          func.right(column, 6), '+00:00')
    return func.timestampdiff(literal_column('SECOND'), literal('1970-01-01 00:00:00'), utc)


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
            func.coalesce(table.c.downloaded_at if source == 'media' else
                _artifact_time(table.c.created_at, catalog.store.db.sqlite), 0).label('downloaded_at'),
            case((matched, 1), else_=0).label('matched'))

    return union_all(projection(local, 'media'), projection(delivery, 'artifact')).subquery()


def page(catalog, root, *, query='', kind='all', cursor=None, limit=24):
    if kind not in ('all', *KINDS):
        raise ValueError('请选择有效素材类型。')
    if not 1 <= limit <= 100:
        raise ValueError('每页标题组数必须在 1 到 100 之间。')
    files = _scope(catalog, root, query[:200])
    groups = select(files.c.group_key, func.min(files.c.title).label('title'),
        func.max(files.c.video_id).label('video_id'), func.count().label('file_count'),
        func.sum(files.c.size).label('size'), func.max(files.c.downloaded_at).label('downloaded_at'),
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
            stamp, key = _position(cursor)
            statement = statement.where(or_(groups.c.downloaded_at < stamp,
                and_(groups.c.downloaded_at == stamp, groups.c.group_key > key)))
        results = [dict(row) for row in db.execute(statement.order_by(
            groups.c.downloaded_at.desc(), groups.c.group_key).limit(limit + 1)).mappings()]
    items = results[:limit]
    next_cursor = _encode([str(items[-1]['downloaded_at']), items[-1]['group_key']]) if len(results) > limit else None
    for item in items:
        item['size'] = int(item['size'])
        kind_counts = {name: int(item.pop(name)) for name in KINDS}
        item['kind_counts'] = {name: count for name, count in kind_counts.items() if count}
        item['downloaded_at'] = float(item['downloaded_at']) or None
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
    statement = select(files.c.file_key, files.c.id, files.c.downloaded_at).where(files.c.group_key == key)
    with catalog.store.connect() as db:
        total = db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
        if cursor:
            stamp, file_key = _position(cursor)
            statement = statement.where(or_(files.c.downloaded_at < stamp,
                and_(files.c.downloaded_at == stamp, files.c.file_key > file_key)))
        rows = list(db.execute(statement.order_by(files.c.downloaded_at.desc(), files.c.file_key).limit(limit + 1)).mappings())
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
                next_cursor=_encode([str(selected[-1]['downloaded_at']), selected[-1]['file_key']]) if len(rows) > limit else None)
