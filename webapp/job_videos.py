"""Bounded job video reads; ID lookups never fall back to a title scan."""
import re
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.mysql import match
from werkzeug.exceptions import NotFound

from webapp.database import delivery_runs, delivery_artifacts, test_samples, download_lists, job_profiles, tasks
from webapp.delivery.artifacts import task_details
from webapp.repositories.task_codec import LIST_FIELDS
from webapp.store_utils import source_identity


PAGE_SIZE = 50


def search_value(query, search):
    if search not in ('video_id', 'title'):
        raise ValueError('请选择视频 ID / 链接或标题关键词查询。')
    query = query.strip()
    if not query:
        return ''
    if search == 'title':
        if not 2 <= len(query) <= 200:
            raise ValueError('标题关键词请输入 2 到 200 个字符。')
        return query
    if len(query) > 2048:
        raise ValueError('视频链接过长。')
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', query):
        return query
    try:
        parsed = urlsplit(query)
        video_id, _ = source_identity(query)
        if (parsed.hostname or '').lower() in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
            if parsed.path.startswith('/embed/'):
                video_id = parsed.path.split('/')[2]
        if parsed.scheme in ('http', 'https') and re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id or ''):
            return video_id
    except ValueError:
        pass
    raise ValueError('请输入完整的 11 位视频 ID 或有效的 YouTube 视频链接。')


def candidates(identity, *, query='', search='video_id', status=None, delivery_status=None,
               cursor=None, mysql=False, samples_only=False):
    query = search_value(query, search)
    if status not in (None, 'queued', 'running', 'succeeded', 'failed', 'interrupted', 'removed'):
        raise ValueError('不支持的视频状态。')
    if delivery_status not in (None, '', 'pending', 'partial', 'review', 'complete', 'accepted'):
        raise ValueError('不支持的交付状态。')
    statement = select(tasks.c.id, tasks.c.seq).where(
        tasks.c.action == 'download',
        tasks.c.status == status if status else tasks.c.status != 'removed')
    if identity:
        statement = statement.where(tasks.c.batch_id == identity)
    else:
        statement = statement.join(download_lists, download_lists.c.id == tasks.c.batch_id).outerjoin(
            job_profiles, job_profiles.c.id == tasks.c.batch_id).where(
                (job_profiles.c.deleted == 0) | job_profiles.c.id.is_(None))
        if not query and not samples_only:
            # Start with the ordered task index and stop after one page. Starting
            # with the few jobs instead materializes and sorts their entire history.
            statement = statement.prefix_with('STRAIGHT_JOIN', dialect='mysql').with_hint(
                tasks, 'FORCE INDEX (' + ('ix_tasks_status_seq' if status else 'ix_tasks_action_seq') + ')',
                dialect_name='mysql')
    if samples_only:
        # Drive this bounded view from the small retained-sample table; no
        # per-row correlated file lookup through millions of queued tasks.
        kept = select(test_samples.c.task_id).where(test_samples.c.state == 'ready')
        if identity:
            kept = kept.where(test_samples.c.job_id == identity)
        statement = statement.where(tasks.c.id.in_(kept))
    if query:
        if search == 'video_id':
            statement = statement.where(tasks.c.video_id == query).with_hint(
                tasks, 'FORCE INDEX (' + ('ix_tasks_job_video' if identity else 'ix_tasks_video') + ')', dialect_name='mysql')
        elif mysql:
            # ngram supports Chinese titles as well as Latin text. This is keyword
            # search, deliberately separate from exact, case-sensitive IDs.
            statement = statement.where(match(tasks.c.title, against=query).in_natural_language_mode())
        else:
            # SQLite is the isolated test backend; production uses MySQL FULLTEXT.
            statement = statement.where(tasks.c.title.contains(query, autoescape=True))
    if delivery_status:
        statement = statement.where(select(delivery_runs.c.task_id).where(
            delivery_runs.c.task_id == tasks.c.id,
            delivery_runs.c.status == delivery_status).exists())
    if cursor is not None:
        statement = statement.where(tasks.c.seq > cursor)
    return statement.order_by(tasks.c.seq).limit(PAGE_SIZE + 1)


def page(store, identity=None, *, query='', search='video_id', status=None, delivery_status=None, cursor=None, samples_only=False):
    statement = candidates(identity, query=query, search=search, status=status,
        delivery_status=delivery_status, cursor=store.statistics.cursor(cursor) if cursor else None,
        mysql=store.db.mysql, samples_only=samples_only)
    with store.connect() as db:
        # A list read only needs existence/deletion checks, never the job's live
        # execution summary, import history or row locks used by editing APIs.
        exists = not identity or db.execute(select(download_lists.c.id).outerjoin(
            job_profiles, job_profiles.c.id == download_lists.c.id).where(
                download_lists.c.id == identity,
                (job_profiles.c.deleted == 0) | job_profiles.c.id.is_(None))).first()
        if not exists:
            raise NotFound('任务不存在或已删除。')
        found = db.execute(statement).all()
        ids = [row.id for row in found[:PAGE_SIZE]]
        rows = {row['id']: row for row in db.execute(
            select(*LIST_FIELDS).where(tasks.c.id.in_(ids))).mappings()} if ids else {}
        delivery = task_details(db, ids)
        job_ids = {row['batch_id'] for row in rows.values()}
        job_names = {row.id: f'{row.customer} / {row.name}' for row in db.execute(select(
            download_lists.c.id, download_lists.c.customer, download_lists.c.name).where(download_lists.c.id.in_(job_ids)))} if job_ids else {}
        kept = {row[0] for row in db.execute(select(test_samples.c.task_id).where(
            test_samples.c.task_id.in_(ids), test_samples.c.state == 'ready'))} if ids else set()
        artifact_ids = {r['artifact_id'] for d in delivery.values() for r in d['requirements'] if r.get('artifact_id')}
        available = {row[0] for row in db.execute(select(delivery_artifacts.c.task_id).where(
            delivery_artifacts.c.id.in_(artifact_ids), delivery_artifacts.c.available == 1,
            delivery_artifacts.c.test_mode == 0))} if artifact_ids else set()
    items = store.decode_many(rows[key] for key in ids if key in rows)
    return dict(items=[{**item, 'delivery': delivery[item['id']], 'job_name': job_names.get(item['batch_id'], ''),
        'file_state': ('sample' if item['id'] in kept else 'stored' if item['id'] in available else
            'cleaned' if delivery[item['id']].get('test_mode') and any(r['status'] == 'done' for r in delivery[item['id']]['requirements']) else 'none')}
        for item in items],
        next_cursor=str(found[PAGE_SIZE - 1].seq) if len(found) > PAGE_SIZE else None)
