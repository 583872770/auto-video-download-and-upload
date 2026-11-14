"""Small, public projections for the delivery artifact catalog."""
from sqlalchemy import or_, select

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


def page(store, *, job=None, kind='', language='', instance_id='', query='', cursor=None, limit=50):
    """Return only fields needed by catalog UIs and previews.

    Host paths and object-store keys stay out of the result set, so the
    database driver never transfers private storage metadata for a listing.
    """
    if not 1 <= limit <= 100:
        raise ValueError('交付文件每页条数必须在 1 到 100 之间。')
    statement = select(*PUBLIC_FIELDS).where(artifacts.c.test_mode == 0, artifacts.c.available == 1)
    if job:
        statement = statement.where(artifacts.c.job_id == job)
    if kind:
        statement = statement.where(artifacts.c.kind == kind)
    if language:
        statement = statement.where(artifacts.c.language == language)
    if instance_id:
        statement = statement.where(artifacts.c.instance_id == instance_id)
    if cursor:
        statement = statement.where(artifacts.c.seq < store.statistics.cursor(cursor))
    if query:
        escaped = query[:200].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        statement = statement.where(or_(artifacts.c.title.contains(escaped, autoescape=True),
                                        artifacts.c.video_id.startswith(escaped, autoescape=True)))
    with store.connect() as db:
        rows = [dict(row) for row in db.execute(
            statement.order_by(artifacts.c.seq.desc()).limit(limit + 1)
        ).mappings()]
    items = rows[:limit]
    for row in items:
        row['content_url'] = '/api/delivery/files/' + row['id']
    return {'items': items, 'next_cursor': str(rows[limit]['seq']) if len(rows) > limit else None}
