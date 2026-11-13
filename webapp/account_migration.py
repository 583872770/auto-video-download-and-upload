"""Account-first credential cleanup, shared by the schema upgrade and maintenance.

Tombstones erase credentials without allowing stale on-disk caches to resurrect
them on older deployments. Ownership hashes and secret-free history survive.
"""
import json

from sqlalchemy import select

from webapp.database import (cookie_profiles as profiles, youtube_accounts as accounts,
    youtube_account_cookies as links, youtube_profile_auth as auth, settings)


def cleanup_credentials(db):
    rows = list(db.execute(select(profiles).with_for_update()).mappings())
    people = {r['id']: r for r in db.execute(select(accounts)).mappings()}
    credentials = {(r['scope'], r['id']): r for r in rows}
    associations = list(db.execute(select(links).order_by(
        links.c.created_at.desc(), links.c.profile_id.desc()).with_for_update()).mappings())
    kept, live, aliases, changed = {}, set(), {}, set()
    removed_links = 0
    for link in associations:
        key = (link['instance_id'], link['profile_id'])
        person, profile = people.get(link['account_id']), credentials.get(key)
        valid = person and person['instance_id'] == key[0] and profile and not profile['deleted']
        if valid and link['account_id'] not in kept:
            kept[link['account_id']] = key
            live.add(key)
            continue
        if valid:
            aliases[key] = kept[link['account_id']][1]
        db.execute(links.delete().where(links.c.instance_id == key[0], links.c.profile_id == key[1]))
        changed.add(link['account_id'])
        removed_links += 1
    removed = []
    for row in rows:
        key = (row['scope'], row['id'])
        if key in live:
            continue
        if not row['deleted'] or row['content']:
            removed.append(key)
            db.execute(profiles.update().where(profiles.c.scope == key[0], profiles.c.id == key[1]).values(
                deleted=1, content=''))
        db.execute(auth.delete().where(auth.c.instance_id == key[0], auth.c.profile_id == key[1]))
    if changed:
        db.execute(accounts.update().where(accounts.c.id.in_(changed)).values(version=accounts.c.version + 1))
    # Persist only IDs for local config reconciliation on each instance startup.
    raw = db.execute(select(settings.c.value).where(settings.c.key == 'youtube_account_cookie_aliases')).scalar_one_or_none()
    saved = json.loads(raw or '{}')
    for (scope, identity), replacement in aliases.items():
        saved.setdefault(scope, {})[identity] = replacement
    value = json.dumps(saved)
    if raw is None:
        db.execute(settings.insert().values(key='youtube_account_cookie_aliases', value=value))
    elif raw != value:
        db.execute(settings.update().where(settings.c.key == 'youtube_account_cookie_aliases').values(value=value))
    return {'removed': removed, 'removed_links': removed_links, 'live': live, 'aliases': saved}


def reconcile_config(config, live, aliases):
    """Remap duplicates and remove deleted selections without choosing other users."""
    result = dict(config)
    pool = list(dict.fromkeys(aliases.get(i, i) for i in config.get('youtube_cookie_pool', [])))
    pool = [i for i in pool if i in live]
    selected = str(config.get('youtube_cookie') or '').replace('\\', '/').rsplit('/', 1)[-1].removesuffix('.txt')
    selected = aliases.get('legacy' if selected == 'youtube' else selected, selected)
    if config.get('youtube_cookie'):
        result['youtube_cookie'] = (f'Cookies/profiles/{selected}.txt' if selected != 'legacy' else 'Cookies/youtube.txt') if selected in live else ''
    if len(pool) == 1:
        result['youtube_cookie'] = f'Cookies/profiles/{pool[0]}.txt' if pool[0] != 'legacy' else 'Cookies/youtube.txt'
        pool = []
    if 'youtube_cookie_pool' in config:
        result['youtube_cookie_pool'] = pool
    return result


def reconcile_local(store, config_path):
    from Utils.config import load_config
    from webapp.cookie_profiles import CookieProfiles
    from webapp.settings import save_settings
    config, base = load_config(config_path)
    scope = CookieProfiles.scope_for(base, store)
    with store.connect() as db:
        live = set(db.execute(select(profiles.c.id).join(links,
            (links.c.instance_id == profiles.c.scope) & (links.c.profile_id == profiles.c.id)
            ).join(accounts, accounts.c.id == links.c.account_id).where(profiles.c.scope == scope,
                profiles.c.deleted == 0, accounts.c.instance_id == scope)).scalars())
        raw = db.execute(select(settings.c.value).where(settings.c.key == 'youtube_account_cookie_aliases')).scalar_one_or_none()
        removed = list(db.execute(select(profiles.c.id).where(profiles.c.scope == scope, profiles.c.deleted == 1)).scalars())
    cleaned = reconcile_config(config, live, json.loads(raw or '{}').get(scope, {}))
    if cleaned != config:
        save_settings(config_path, cleaned)
    # Files are disposable. Never import them as a new credential.
    library = CookieProfiles(base, store)
    for identity in removed:
        target = library.path(identity)
        target.unlink(missing_ok=True)
        if identity != 'legacy':
            target.with_suffix('.json').unlink(missing_ok=True)


def upgrade_account_credentials(engine):
    with engine.begin() as db:
        cleanup_credentials(db)
    next(i for i in links.indexes if i.name == 'uq_youtube_account_cookie').create(engine, checkfirst=True)
