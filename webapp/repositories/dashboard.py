"""Bounded database snapshots for dashboard composition."""
import json
import time
from sqlalchemy import func, select
from webapp.concurrency_diagnostics import read_context
from webapp.database import tasks as diagnostic_tasks, settings as diagnostic_settings, delivery_work


def concurrency_inputs(store, config, base, fleet, queue):
    identity = fleet.id if fleet else 'local'
    with store.connect() as db:
        stamp = fleet.clock(db) if fleet else time.time()
        statement = select(diagnostic_tasks.c.id, diagnostic_tasks.c.batch_id,
            diagnostic_tasks.c.video_id, diagnostic_tasks.c.title, diagnostic_tasks.c.progress,
            diagnostic_tasks.c.status, diagnostic_tasks.c.worker_id, diagnostic_tasks.c.started_at,
            diagnostic_tasks.c.attempts, diagnostic_tasks.c.lease_until).where(
            diagnostic_tasks.c.status == 'running', diagnostic_tasks.c.action == 'download',
            ~select(delivery_work.c.task_id).where(delivery_work.c.task_id == diagnostic_tasks.c.id).exists())
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
        from webapp.database import notifications, notification_tasks
        waiting = db.execute(select(diagnostic_tasks.c.id, diagnostic_tasks.c.title,
            diagnostic_tasks.c.video_id, diagnostic_tasks.c.progress).select_from(
                notifications.join(notification_tasks, notification_tasks.c.group_id == notifications.c.id)
                .join(diagnostic_tasks, diagnostic_tasks.c.id == notification_tasks.c.task_id)).where(
                    notifications.c.instance_id == identity, notifications.c.code == 'disk',
                    diagnostic_tasks.c.status == 'queued').order_by(notification_tasks.c.task_id).limit(100)).mappings()
        waiting_tasks = [{**dict(row), 'progress': json.loads(row['progress'] or '{}')} for row in waiting]
        context = read_context(db, [(identity, config)], stamp)
        budget = db.execute(select(diagnostic_settings.c.value).where(diagnostic_settings.c.key == 'fleet_budget')).scalar_one_or_none()
        global_running = db.execute(select(func.count()).select_from(diagnostic_tasks).where(
            diagnostic_tasks.c.status == 'running', diagnostic_tasks.c.action == 'download',
            ~select(delivery_work.c.task_id).where(delivery_work.c.task_id == diagnostic_tasks.c.id).exists())).scalar_one() if int(budget or 0) else 0
        local_counts = dict(db.execute(select(diagnostic_tasks.c.status, func.count()).select_from(
            diagnostic_tasks.join(delivery_work, diagnostic_tasks.c.id == delivery_work.c.task_id)).where(
                delivery_work.c.instance_id == identity, diagnostic_tasks.c.status.in_(('queued', 'running')))
                .group_by(diagnostic_tasks.c.status)).all())
        local_processing = dict(queued=local_counts.get('queued', 0), running=local_counts.get('running', 0),
                                limit=int(config.get('postprocess_limit', 2)))
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
        if fleet:
            from webapp.database import counters, download_lists, job_profiles
            selected = db.execute(select(diagnostic_settings.c.value).where(
                diagnostic_settings.c.key == 'workbench_job:' + identity)).scalar_one_or_none()
            pending = db.execute(select(counters.c.value).where(
                counters.c.scope == 'batch:' + selected, counters.c.status == 'queued',
                select(download_lists.c.id).where(download_lists.c.id == selected).exists(),
                ~select(job_profiles.c.id).where(job_profiles.c.id == selected,
                    job_profiles.c.deleted == 1).exists())).scalar_one_or_none() if selected else 0
        else:
            pending = queue.get('queued', 0)
    return dict(identity=identity, stamp=stamp, progress=progress, current_tasks=current_tasks,
                waiting_tasks=waiting_tasks, context=context, local_processing=local_processing,
                budget=int(budget or 0), global_running=global_running, profiles=profile_metadata,
                paused=bool(node['paused']) if fleet else queue.get('paused', False),
                error=node.get('error', ''), pending_count=int(pending or 0))
