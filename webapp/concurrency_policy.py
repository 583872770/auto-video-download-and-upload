"""Pure concurrency decisions shared by admission, sizing and diagnostics.

Callers supply validated configuration and fresh observations. Database locks,
lease/owner checks, Cookie identity, resource sampling and waits stay with their
owners; a computed capacity alone never authorizes a download.
"""
from dataclasses import dataclass

def minimum_limit(*limits):
    finite = [value for value in limits if value is not None]
    return min(finite) if finite else None
from webapp.config.schema import DEFAULTS
from webapp.db_pool_calculator import download_task_limit


MAX_TRANSFERS = 64
MAX_WORKERS = 96
POLICY_TTL = 90


def cookie_pool_start_interval(config):
    """Zero disables future spacing, never an already reserved window."""
    return int(config.get('youtube_cookie_pool_start_interval', DEFAULTS['youtube_cookie_pool_start_interval']))


def cookie_session_limit(config, state):
    configured = max(1, min(MAX_TRANSFERS, int(config.get('youtube_cookie_session_limit', DEFAULTS['youtube_cookie_session_limit']))))
    return configured if state.get('limit', 1) > 1 and not state.get('recovery_mode') else 1


def live_policies(state, stamp):
    return {key: value for key, value in state.get('policies', {}).items()
            if value.get('time', 0) > stamp - POLICY_TTL}


@dataclass(frozen=True)
class ResourceCapacity:
    limit: int | None
    effective: int | None
    cooling: float
    recovering: float


def resource_capacity(cap, state, stamp):
    """Recent participants share the minimum; cooldown wins over recovery."""
    limit = minimum_limit(cap, *(policy['limit'] for policy in live_policies(state, stamp).values()))
    cooling = max(0, state.get('cooldown_until', 0) - stamp)
    recovering = max(0, state.get('recover_until', 0) - stamp)
    effective = 0 if cooling else minimum_limit(1, limit) if recovering else limit
    return ResourceCapacity(limit, effective, cooling, recovering)


def fragment_limit(config):
    """Per-file ceiling, never divided by the number of accounts or tasks."""
    return max(1, min(16, int(config.get('fragment_concurrency', 4))))


def pool_process_limits(config, capacity, memory_total, db_budget):
    capacity = max(1, min(MAX_TRANSFERS, int(capacity)))
    configured = download_task_limit(config)
    # Parallel HTTP retains at most one 10 MiB chunk per fragment. Reserve
    # worker overhead plus those buffers instead of inventing a connection cap.
    per_worker = (160 + 10 * fragment_limit(config)) * 1024**2
    memory_cap = max(1, int((memory_total * .65 - 256 * 1024**2) / per_worker))
    bounds = {'accounts': capacity, 'configured': configured or None,
              'memory': memory_cap, 'database': db_budget['workers']}
    workers = max(1, minimum_limit(*bounds.values()))
    return {'workers': workers, 'configured_worker_limit': configured, 'worker_bounds': bounds,
            'worker_limit_reasons': [key for key, value in bounds.items() if value == workers
                                     and not (key == 'database' and db_budget.get('target_supported'))],
            'memory_worker_cap': memory_cap, 'db_worker_cap': db_budget['workers'],
            'memory_per_worker_mb': per_worker // 1024**2,
            'db_connection_budget': db_budget['total'], 'db_main_connections': sum(db_budget['main']),
            'db_auxiliary_connections': db_budget['auxiliary'], 'db_processing_connections': db_budget['processing'],
            'db_connections_per_worker': db_budget['per_worker']}
