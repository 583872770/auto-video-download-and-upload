"""Persisted, secret-free Cookie event history used by the settings UI."""
import json
import re
import time
import uuid

from sqlalchemy import desc, select

from webapp.database import youtube_cookie_events
from webapp.diagnostics import safe_text


def record(store, *, profile_id=None, profile_name=None, event_type='failure',
           phase=None, kind=None, message='', incident_id=None, http_status=None,
           retry_after=None, occurred_at=None, previous_interval=None,
           new_interval=None, previous_state=None, new_state=None,
           cooldown_until=None, task_id=None, metadata=None, db=None, scope=None):
    """Insert one bounded event. ``db`` may be supplied by an outer transaction."""
    occurred_at = time.time() if occurred_at is None else occurred_at
    row = dict(id=uuid.uuid4().hex, seq=None,
               scope=scope or ('youtube-cookie:' + str(profile_id or 'unknown')),
               profile_id=str(profile_id)[:32] if profile_id else None,
               profile_name=safe_text(profile_name)[:80] if profile_name else None,
               incident_id=str(incident_id)[:32] if incident_id else None,
               event_type=safe_text(event_type)[:32], phase=safe_text(phase)[:32] if phase else None,
               kind=safe_text(kind)[:64] if kind else None,
               message=re.sub(r'https?://[^\s<>"\']+', '[url]', safe_text(message))[:1000], http_status=http_status,
               retry_after=retry_after, occurred_at=occurred_at,
               previous_interval=previous_interval, new_interval=new_interval,
               previous_state=safe_text(previous_state)[:32] if previous_state else None,
               new_state=safe_text(new_state)[:32] if new_state else None,
               cooldown_until=cooldown_until, task_id=str(task_id)[:32] if task_id else None,
               metadata=json.dumps(metadata or {}, ensure_ascii=False)[:4000])
    def insert(connection):
        row['seq'] = store.db.sequence(connection, 'youtube-cookie-event')
        connection.execute(youtube_cookie_events.insert().values(row))
    if db is not None:
        insert(db)
    else:
        with store.connect() as connection:
            insert(connection)
    return row


def list_events(store, profile_id, *, since=None, until=None, kind=None,
                phase=None, event_type=None, only_interval_changes=False, limit=100):
    try:
        limit = max(1, min(500, int(limit or 100)))
    except (TypeError, ValueError):
        limit = 100
    with store.connect() as db:
        query = select(youtube_cookie_events).where(youtube_cookie_events.c.profile_id == str(profile_id))
        if since is not None:
            query = query.where(youtube_cookie_events.c.occurred_at >= float(since))
        if until is not None:
            query = query.where(youtube_cookie_events.c.occurred_at <= float(until))
        if kind:
            query = query.where(youtube_cookie_events.c.kind == str(kind)[:64])
        if phase:
            query = query.where(youtube_cookie_events.c.phase == str(phase)[:32])
        if event_type:
            query = query.where(youtube_cookie_events.c.event_type == str(event_type)[:32])
        if only_interval_changes:
            query = query.where(youtube_cookie_events.c.previous_interval.is_not(None),
                                youtube_cookie_events.c.new_interval.is_not(None),
                                youtube_cookie_events.c.previous_interval != youtube_cookie_events.c.new_interval)
        rows = db.execute(query.order_by(desc(youtube_cookie_events.c.occurred_at)).limit(limit)).mappings()
        result = []
        for row in rows:
            value = dict(row)
            try:
                value['metadata'] = json.loads(value.get('metadata') or '{}')
            except (TypeError, ValueError):
                value['metadata'] = {}
            result.append(value)
        return result
