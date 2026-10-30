"""Read-only admission diagnostics; no log scans, probes or scheduler mutations."""
import hashlib
import json
import math
from collections import Counter

from sqlalchemy import func, select

from webapp.channels import backend_key, group_caps, registry
from webapp.database import download_permits, download_resources, settings, tasks
from webapp.throughput import automatic, policy_key


def scopes_for(config, identity):
    scopes = {'local-transfers:' + identity, 'local-connections:' + identity, 'route-probe:' + identity}
    for channel in registry(config):
        for group in {channel.upstream_group, 'youtube-origin'}:
            scopes.update(('upstream:' + group, 'connections:' + group,
                           'local-upstream:' + identity + ':' + group))
        scopes.update(('channel:' + channel.id, 'quota:' + channel.quota_group,
                       'resolver:' + channel.resolver_group,
                       'route:' + channel.id, 'backend-route:' + backend_key(channel),
                       'backend-quota:' + backend_key(channel),
                       'backend-resolver:' + backend_key(channel)))
    return scopes


def read_context(db, configurations, stamp):
    """Batch only known resource identities, and count permits with valid leases."""
    scopes = set().union(*(scopes_for(config, identity) for identity, config in configurations))
    keys = {hashlib.sha256(scope.encode()).hexdigest(): scope for scope in scopes}
    states = {keys[key]: json.loads(value) for key, value in db.execute(
        select(download_resources.c.key, download_resources.c.state).where(download_resources.c.key.in_(keys)))}
    usage = {keys[key]: int(value) for key, value in db.execute(
        select(download_permits.c.resource, func.sum(download_permits.c.units)).select_from(
            download_permits.join(tasks, (download_permits.c.task_id == tasks.c.id)
                                  & (download_permits.c.owner == tasks.c.worker_id))).where(
            download_permits.c.resource.in_(keys), tasks.c.status == 'running', tasks.c.lease_until > stamp
        ).group_by(download_permits.c.resource))}
    disk_keys = ['disk:' + identity for identity, _ in configurations]
    disk = {key.removeprefix('disk:'): json.loads(value) for key, value in db.execute(
        select(settings.c.key, settings.c.value).where(settings.c.key.in_(disk_keys)))}
    return states, usage, disk


def diagnose(config, identity, progress, rate, context, stamp, *, global_limit=0, global_running=0, paused=False, online=True):
    states, usage, disks = context
    requested = int(config.get('worker_count', 1))
    auto = automatic(config)
    fresh = bool(rate and rate.get('policy') == policy_key(config) and 0 <= stamp - rate.get('time', 0) < 20)
    transfers = int(rate['transfers']) if auto and fresh else min(requested, 2) if auto else requested
    channels = registry(config)
    caps = group_caps(channels)
    backend_caps = {}
    for channel in channels:
        key = backend_key(channel)
        limits = (channel.task_limit, channel.resolver_limit, channel.transfer_limit)
        backend_caps[key] = tuple(min(a, b) for a, b in zip(backend_caps.get(key, limits), limits))

    def resource(scope, cap):
        state = states.get(scope, {})
        live = [p['limit'] for p in state.get('policies', {}).values() if p.get('time', 0) > stamp - 90]
        ceiling = min([cap, *live])
        cooling = max(0, state.get('cooldown_until', 0) - stamp)
        recovering = max(0, state.get('recover_until', 0) - stamp)
        return {'scope': scope.split(':', 1)[0], 'group': scope.split(':', 1)[1], 'limit': ceiling,
                'effective': 0 if cooling else min(1, ceiling) if recovering else ceiling,
                'used': usage.get(scope, 0), 'cooldown_seconds': math.ceil(cooling),
                'recovery_seconds': math.ceil(recovering),
                'last_throttle_status': state.get('cooldown_status') if cooling or recovering else None,
                'last_throttle_kind': state.get('cooldown_kind') if cooling or recovering else None,
                'last_throttle_at': state.get('last_throttle_at')}

    rows, group_limits = [], {}
    for channel in channels:
        group = channel.upstream_group
        group_limit = min(transfers, caps['upstream:' + group])
        if auto and fresh:
            group_limit = min(group_limit, rate.get('groups', {}).get(group, {}).get('limit', group_limit))
        resources = [resource('channel:' + channel.id, channel.task_limit if config.get('scheduler_mode') == 'auto' else 4096),
                     resource('upstream:' + group, caps['upstream:' + group]),
                     resource('quota:' + channel.quota_group, caps['quota:' + channel.quota_group]),
                     resource('local-upstream:' + identity + ':' + group, group_limit)]
        resources.append(resource('local-transfers:' + identity, transfers))
        if config.get('scheduler_mode') == 'auto':
            resources.append(resource('backend-quota:' + backend_key(channel), backend_caps[backend_key(channel)][2]))
        effective = min(transfers, *(r['effective'] for r in resources))
        # Manual yt-dlp reserves this many connection units before extraction.
        connection_units = (min(int(config.get('fragment_concurrency', 4)), channel.connection_limit,
                               max(1, int(config.get('download_connection_limit', 32)) // transfers))
                            if channel.adapter == 'yt_dlp' and not auto else None)
        connection = resource('connections:' + group, caps['connections:' + group])
        local_connection = resource('local-connections:' + identity, int(config.get('download_connection_limit', 32)))
        if connection_units:
            effective = min(effective, connection['effective'] // connection_units,
                            local_connection['effective'] // connection_units)
        else:
            effective = min(effective, connection['effective'], local_connection['effective'])
        resources.extend((connection, local_connection))
        resolving = ([resource('resolver:' + channel.resolver_group, caps['resolver:' + channel.resolver_group])]
                     if channel.adapter != 'yt_dlp' or auto else [])
        if config.get('scheduler_mode') == 'auto':
            resolving.extend((resource('backend-resolver:' + backend_key(channel), backend_caps[backend_key(channel)][1]),
                              resource('route:' + channel.id, channel.task_limit),
                              resource('backend-route:' + backend_key(channel), backend_caps[backend_key(channel)][0])))
            if not (states.get('channel:' + channel.id, {}).get('rate', 0) > 0
                    or states.get('channel:' + channel.id, {}).get('successes', 0) > 0):
                resolving.append(resource('route-probe:' + identity, 1))
        cooldown = max(r['cooldown_seconds'] for r in resources)
        recovery = max(r['recovery_seconds'] for r in resources)
        channel_state = states.get('channel:' + channel.id, {})
        rows.append({'id': channel.id, 'label': channel.label, 'group': group,
                     'transfer_limit': channel.transfer_limit, 'resolver_limit': channel.resolver_limit,
                     'resolver_separate': channel.adapter != 'yt_dlp' or auto,
                     'connection_limit': connection['limit'], 'connection_used': connection['used'],
                     'connection_units': connection_units, 'shared_limit': resources[1]['limit'],
                     'effective_limit': effective, 'occupied': resources[1]['used'],
                     'cooldown_seconds': cooldown, 'recovery_seconds': recovery,
                     'resolver_cooldown_seconds': max((r['cooldown_seconds'] for r in resolving), default=0),
                     'resources': [{**r, 'phase': phase} for phase, items in (('transfer', resources), ('resolve', resolving)) for r in items],
                     'failures': channel_state.get('failures', 0),
                     'status': 'cooldown' if cooldown else 'recovering' if recovery else 'available',
                     'throttle_status': next((r['last_throttle_status'] for r in resources if r['last_throttle_status']), None),
                     'throttle_kind': next((r['last_throttle_kind'] for r in resources if r['last_throttle_kind']), None)})
        group_limits[group] = max(group_limits.get(group, 0), effective)
    upper = min(transfers, sum(group_limits.values()))
    if global_limit:
        upper = min(upper, max(0, global_limit - max(0, global_running - len(progress))))
    stages = Counter(p.get('stage', 'preparing') for p in progress)
    waits = Counter(p.get('transfer_wait_resource') or 'local-transfers' for p in progress if p.get('stage') == 'waiting_transfer')
    disk = disks.get(identity, {})
    disk_fresh = 0 <= stamp - disk.get('time', 0) < 90
    return {'time': stamp, 'online': online, 'paused': bool(paused), 'requested': requested,
            'mode': 'auto' if auto else 'manual', 'sample_fresh': fresh,
            'sample_time': rate.get('time') if rate else None,
            'local_health': (rate or {}).get('risk_local'),
            'transfer_limit': transfers, 'effective_limit': upper, 'global_limit': global_limit,
            'global_running': global_running,
            'adjustment_reason': rate.get('reason') if fresh else None,
            'connection_used': usage.get('local-connections:' + identity, 0),
            'connection_budget': int(config.get('download_connection_limit', 32)),
            'running': len(progress), 'transferring': stages.get('downloading', 0),
            'preparing': sum(value for stage, value in stages.items() if stage not in ('downloading', 'waiting_transfer', 'processing')),
            'processing': stages.get('processing', 0), 'waiting_transfer': stages.get('waiting_transfer', 0),
            'wait_resources': dict(waits), 'stages': dict(stages), 'channels': rows,
            'subtitle_mode': config.get('subtitle_mode', 'optional'),
            'browser_limit': int(rate.get('browsers', config.get('yt5s_browser_count', 2))) if fresh else int(config.get('yt5s_browser_count', 2)),
            'disk_sample_fresh': disk_fresh,
            'disk_blocked': bool(disk_fresh and disk.get('blocked')),
            'disk_message': disk.get('message', '') if disk_fresh else ''}
