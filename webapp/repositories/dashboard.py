"""Bounded database snapshots for dashboard composition."""
import json
import time
from sqlalchemy import func, select
from webapp.concurrency_diagnostics import read_context
from webapp.database import tasks as diagnostic_tasks, settings as diagnostic_settings
from webapp.subtitles import summary as subtitle_summary


def concurrency_inputs(store, config, base, fleet, queue):
    identity = fleet.id if fleet else 'local'
    with store.connect() as db:
        stamp = fleet.clock(db) if fleet else time.time()
        statement = select(diagnostic_tasks.c.id, diagnostic_tasks.c.batch_id,
            diagnostic_tasks.c.video_id, diagnostic_tasks.c.title, diagnostic_tasks.c.progress,
            diagnostic_tasks.c.status, diagnostic_tasks.c.worker_id, diagnostic_tasks.c.started_at,
            diagnostic_tasks.c.attempts, diagnostic_tasks.c.lease_until).where(
            diagnostic_tasks.c.status == 'running', diagnostic_tasks.c.action == 'download')
        if fleet:
            statement = statement.where(diagnostic_tasks.c.instance_id == identity)
        active = list(db.execute(statement.order_by(diagnostic_tasks.c.seq)).mappings())
        live = store.progress_cache.read_many(active)
        current_tasks = [
            {key: task[key] for key in ('id', 'batch_id', 'video_id', 'title', 'lease_until')} |
            {'progress': live[task['id']]['progress'] if task['id'] in live else json.loads(task['progress'] or '{}')}
            for task in active
        ]
        progress = [task['progress'] for task in current_tasks]
        context = read_context(db, [(identity, config)], stamp)
        budget = db.execute(select(diagnostic_settings.c.value).where(diagnostic_settings.c.key == 'fleet_budget')).scalar_one_or_none()
        global_running = db.execute(select(func.count()).select_from(diagnostic_tasks).where(
            diagnostic_tasks.c.status == 'running', diagnostic_tasks.c.action == 'download')).scalar_one() if int(budget or 0) else 0
        from webapp.database import cookie_profiles as diagnostic_profiles, youtube_account_cookies as diagnostic_links, youtube_accounts as diagnostic_accounts
        from webapp.cookie_profiles import CookieProfiles
        profile_metadata = []
        if config.get('youtube_cookie_pool'):
            source = diagnostic_profiles.outerjoin(diagnostic_links,
                (diagnostic_links.c.instance_id == diagnostic_profiles.c.scope) & (diagnostic_links.c.profile_id == diagnostic_profiles.c.id))
            source = source.outerjoin(diagnostic_accounts, diagnostic_accounts.c.id == diagnostic_links.c.account_id)
            profile_metadata = list(db.execute(select(diagnostic_profiles.c.id, diagnostic_profiles.c.name,
                diagnostic_accounts.c.id.label('account_id'), diagnostic_accounts.c.email).select_from(source).where(
                    diagnostic_profiles.c.scope == CookieProfiles.scope_for(base, store), diagnostic_profiles.c.deleted == 0,
                    diagnostic_profiles.c.id.in_(config['youtube_cookie_pool']))).mappings())
        node = fleet.row(db) if fleet else {}
        subtitles = subtitle_summary(db, stamp, identity)
    return dict(identity=identity, stamp=stamp, progress=progress, current_tasks=current_tasks, context=context,
                budget=int(budget or 0), global_running=global_running, profiles=profile_metadata,
                paused=bool(node['paused']) if fleet else queue.get('paused', False),
                error=node.get('error', ''), subtitles=subtitles)


def fleet_media_overview(store):
    from sqlalchemy import func, select
    from webapp.database import media as catalog_media, roots as catalog_roots, instances as fleet_instances
    with store.connect() as db:
        current_roots = []
        for identity, value in db.execute(select(fleet_instances.c.id, fleet_instances.c.config)
                                          .where(fleet_instances.c.role.in_(('worker', 'hybrid')))):
            directory = json.loads(value).get('directory')
            if directory:
                current_roots.append('@' + identity + '|' + directory)
        total = db.execute(select(func.coalesce(func.sum(catalog_roots.c.count), 0)).where(catalog_roots.c.root.in_(current_roots))).scalar_one()
        recent = [dict(row) for row in db.execute(select(catalog_media).where(catalog_media.c.root.in_(current_roots))
                  .order_by(catalog_media.c.modified.desc()).limit(4)).mappings()]
    for item in recent:
        item['instance_id'] = item['root'][1:].split('|', 1)[0]
    return int(total), recent
