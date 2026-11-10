"""Publish a bounded, shared login observation from both worker and UI checks."""
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from webapp.account_library import context_key
from webapp.database import cookie_profiles, youtube_profile_auth as table


def publish_profile_auth(store, config, base, result, generation, observed_data):
    if observed_data is None:
        return
    from webapp.cookie_profiles import CookieProfiles
    identity = config.get('_cookie_pool_selected')
    if not identity:
        path = str(config.get('youtube_cookie') or '').replace('\\', '/')
        identity = 'legacy' if path.endswith('Cookies/youtube.txt') else Path(path).stem
    if not identity:
        return
    scope = CookieProfiles.scope_for(base, store)
    with store.connect() as db:
        content = db.execute(select(cookie_profiles.c.content).where(cookie_profiles.c.scope == scope,
            cookie_profiles.c.id == identity, cookie_profiles.c.deleted == 0).with_for_update()).scalar_one_or_none()
        if content is None:
            return
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        if fingerprint != hashlib.sha256(observed_data).hexdigest():
            return  # A Cookie replacement during the request invalidates it.
        old = db.execute(select(table).where(table.c.instance_id == scope,
            table.c.profile_id == identity).with_for_update()).mappings().first()
        previous = json.loads(old['auth']) if old else {}
        if old and previous.get('checked_at', 0) > result.get('checked_at', 0):
            return
        if old and previous == result and old['fingerprint'] == fingerprint and old['context'] == context_key(config, generation):
            return  # Reusing the auth cache does not require another write.
        last = old['last_valid_at'] if old else 0
        if result.get('state') == 'valid':
            last = max(last, result.get('checked_at', 0))
        db.execute(store.db.upsert(table, dict(instance_id=scope, profile_id=identity,
            fingerprint=fingerprint, context=context_key(config, generation),
            auth=json.dumps(result, ensure_ascii=False), last_valid_at=last), ['instance_id','profile_id']))
