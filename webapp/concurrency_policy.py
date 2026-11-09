"""Pure concurrency decisions shared by admission, sizing and diagnostics.

Callers supply validated configuration and fresh observations. Database locks,
lease/owner checks, Cookie identity, resource sampling and waits stay with their
owners; a computed capacity alone never authorizes a download.
"""
from dataclasses import dataclass

from webapp.channels import backend_key, group_caps, registry
from webapp.config_schema import DEFAULTS


MAX_TRANSFERS = 64
MAX_WORKERS = 96
POLICY_TTL = 90


def automatic(config):
    # Routing mode does not select the instance's concurrency mode.
    return not config.get('youtube_cookie_pool') and config.get('throughput_mode', 'manual') == 'auto'


def cookie_pool_start_interval(config):
    """Zero disables future spacing, never an already reserved window."""
    return int(config.get('youtube_cookie_pool_start_interval', DEFAULTS['youtube_cookie_pool_start_interval']))


def youtube_start_interval(config):
    if config.get('_cookie_pool_selected'):
        return cookie_pool_start_interval(config)
    return int(config.get('youtube_start_interval', DEFAULTS['youtube_start_interval']))


def cookie_session_limit(config, state):
    configured = max(1, min(MAX_TRANSFERS, int(config.get('youtube_cookie_session_limit', DEFAULTS['youtube_cookie_session_limit']))))
    return configured if state.get('limit', 1) > 1 and not state.get('recovery_mode') else 1


def fragment_connection_budget(config, channel_limit, transfers):
    if channel_limit is None:
        channel_limit = int(config.get('download_connection_limit', 32))
    return max(1, min(16, int(config.get('fragment_concurrency', 4)), channel_limit,
                      max(1, int(config.get('download_connection_limit', 32)) // max(1, transfers))))


def backend_limits(channels):
    limits = {}
    for channel in channels:
        key = backend_key(channel)
        current = (channel.task_limit, channel.resolver_limit, channel.transfer_limit)
        limits[key] = tuple(min(a, b) for a, b in zip(limits.get(key, current), current))
    return limits


def assignment_transfer_limit(channel, local):
    """Preparation demand may later resolve onto the YouTube media upstream."""
    groups = local.get('groups', {})
    limit = min(channel.transfer_limit, groups.get(channel.upstream_group, {}).get('limit', local['transfers']))
    if channel.upstream_group == 'unverified-media':
        limit = min(channel.transfer_limit, max(limit, groups.get('youtube-origin', {}).get('limit', limit)))
    return limit


def resolver_requirements(channel, caps):
    return [('resolver:' + channel.resolver_group, caps['resolver:' + channel.resolver_group], 1),
            ('quota:' + channel.quota_group, caps['quota:' + channel.quota_group], 1)]


def transfer_requirements(config, identity, channel, caps, local, streams=1):
    """Ordered overlapping permits; connection units differ from video units."""
    group = channel.upstream_group
    ceiling = caps.get('upstream:' + group, channel.transfer_limit)
    limit = min(ceiling, local.get('groups', {}).get(group, {}).get('limit', ceiling))
    requirements = [('upstream:' + group, ceiling, 1),
                    ('local-transfers:' + identity, local['transfers'], 1),
                    ('local-upstream:' + identity + ':' + group, limit, 1),
                    ('local-connections:' + identity, int(config.get('download_connection_limit', 32)), streams),
                    ('quota:' + channel.quota_group, caps['quota:' + channel.quota_group], 1)]
    shared = caps.get('connections:' + group, channel.connection_limit)
    if shared is not None:
        requirements.append(('connections:' + group, shared, streams))
    return requirements


def live_policies(state, stamp):
    return {key: value for key, value in state.get('policies', {}).items()
            if value.get('time', 0) > stamp - POLICY_TTL}


@dataclass(frozen=True)
class ResourceCapacity:
    limit: int
    effective: int
    cooling: float
    recovering: float


def resource_capacity(cap, state, stamp):
    """Recent participants share the minimum; cooldown wins over recovery."""
    limit = min([cap, *(policy['limit'] for policy in live_policies(state, stamp).values())])
    cooling = max(0, state.get('cooldown_until', 0) - stamp)
    recovering = max(0, state.get('recover_until', 0) - stamp)
    effective = 0 if cooling else min(1, limit) if recovering else limit
    return ResourceCapacity(limit, effective, cooling, recovering)


def pool_process_limits(config, capacity, memory_total, db_budget):
    """Static process bounds; live account/transfer permits still control work.

    Keep one worker when all accounts are cooling so state can be rechecked.
    The worker floor never grants an account or a media transfer permit.
    """
    capacity = max(1, min(MAX_TRANSFERS, int(capacity)))
    channels = registry(config)
    caps = group_caps(channels)
    groups = {}
    for channel in channels:
        units = fragment_connection_budget(config, channel.connection_limit, capacity) if channel.adapter == 'yt_dlp' else 1
        group = channel.upstream_group
        limit = min(capacity, channel.transfer_limit, caps['upstream:' + group],
                    int(config.get('download_connection_limit', 32)) // units)
        if 'connections:' + group in caps:
            limit = min(limit, caps['connections:' + group] // units)
        groups[group] = max(groups.get(group, 0), limit)
    transfer_capacity = min(capacity, sum(groups.values()), int(config.get('download_connection_limit', 32)))
    preparation = max(0, min(16, int(config.get('youtube_cookie_pool_preparation_workers', 4))))
    configured = max(1, min(MAX_TRANSFERS, int(config.get('youtube_cookie_pool_worker_limit', 20))))
    memory_cap = max(1, int((memory_total * .65 - 256 * 1024**2) / (160 * 1024**2)))
    workers = max(1, min(capacity, configured, transfer_capacity + preparation, memory_cap, db_budget['workers']))
    return {'workers': workers, 'configured_worker_limit': configured,
            'preparation_workers': preparation, 'worker_transfer_capacity': transfer_capacity,
            'memory_worker_cap': memory_cap, 'db_worker_cap': db_budget['workers'],
            'db_connection_budget': db_budget['total'], 'db_main_connections': sum(db_budget['main']),
            'db_auxiliary_connections': db_budget['auxiliary'], 'db_connections_per_worker': db_budget['per_worker']}
