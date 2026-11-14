"""Small, public projections for the delivery artifact catalog."""
from sqlalchemy import func, or_, select

from webapp.database import delivery_artifacts as artifacts


PUBLIC_FIELDS = (
    artifacts.c.id,
    artifacts.c.seq,
    artifacts.c.job_id,
    artifacts.c.task_id,
    artifacts.c.video_id,
    artifacts.c.title,
    artifacts.c.kind,
    artifacts.c.language,
    artifacts.c.variant,
    artifacts.c.size,
    artifacts.c.instance_id,
    artifacts.c.storage,
    artifacts.c.available,
    artifacts.c.created_at,
)


def page(store, *, job=None, kind='', kinds=None, language='', instance_id='', query='', cursor=None,
         limit=50, exhausted=False):
    """Return only fields needed by catalog UIs and previews.

    Host paths and object-store keys stay out of the result set, so the
    database driver never transfers private storage metadata for a listing.
    """
    if not 1 <= limit <= 100:
        raise ValueError('交付文件每页条数必须在 1 到 100 之间。')
    statement = select(*PUBLIC_FIELDS).where(artifacts.c.test_mode == 0, artifacts.c.available == 1)
    if job:
        statement = statement.where(artifacts.c.job_id == job)
    if language:
        statement = statement.where(artifacts.c.language == language)
    if instance_id:
        statement = statement.where(artifacts.c.instance_id == instance_id)
    if query:
        statement = statement.where(or_(artifacts.c.title.contains(query[:200], autoescape=True),
                                        artifacts.c.video_id.startswith(query[:200], autoescape=True)))
    with store.connect() as db:
        counts = dict(db.execute(statement.with_only_columns(artifacts.c.kind, func.count())
                                 .group_by(artifacts.c.kind)).all())
        if kind:
            statement = statement.where(artifacts.c.kind == kind)
        if kinds is not None:
            statement = statement.where(artifacts.c.kind.in_(kinds))
        total = sum(count for name, count in counts.items()
                    if (not kind or name == kind) and (kinds is None or name in kinds))
        if cursor:
            statement = statement.where(artifacts.c.seq < store.statistics.cursor(cursor))
        rows = [] if exhausted else [dict(row) for row in db.execute(
            statement.order_by(artifacts.c.seq.desc()).limit(limit + 1)).mappings()]
    items = rows[:limit]
    for row in items:
        row['content_url'] = '/api/delivery/files/' + row['id']
    return {'items': items, 'next_cursor': str(items[-1]['seq']) if len(rows) > limit else None,
            'total': total, 'counts': counts}
