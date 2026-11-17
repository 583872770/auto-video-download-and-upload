"""One per-instance connection contract for the supervisor and its children."""
import os


def _integer(name, default, minimum=0):
    value = int(os.environ.get(name, default))
    if value < minimum:
        raise ValueError(f'{name} must be at least {minimum}')
    return value


def worker_pool_size():
    # Progress/business writes and lease heartbeats can overlap in a worker.
    return (_integer('VIDEO_WORKER_DB_POOL_SIZE', 2, 2),
            _integer('VIDEO_WORKER_DB_MAX_OVERFLOW', 0))


def download_task_limit(config):
    """Zero removes the configured process cap, not resource/admission limits."""
    return max(0, min(64, int(config.get('download_task_limit', 0))))


def target_workers(config):
    """Size the reservation from configured demand, never temporary cooldowns."""
    sessions = len(config.get('youtube_cookie_pool', []))
    per_session = max(1, min(64, int(config.get('youtube_cookie_session_limit', 1))))
    demand = max(1, min(64, sessions * per_session))
    return min(demand, download_task_limit(config) or 64)


def connection_budget(config=None):
    """Reserve main, auxiliary and local delivery workers before network workers.

    This is an instance budget, not the MySQL server's max_connections. Operators
    sharing a database must allocate a suitable budget to each instance.
    """
    per_worker = sum(worker_pool_size())
    auxiliary = _integer('VIDEO_DB_AUXILIARY_CONNECTIONS', 2 * per_worker, 2 * per_worker)
    processing = max(1, min(8, int((config or {}).get('postprocess_limit', 2)))) * per_worker
    main = (_integer('VIDEO_DB_POOL_SIZE', 5, 2), _integer('VIDEO_DB_MAX_OVERFLOW', 2))
    fixed = sum(main) + auxiliary + processing
    # Engine initialization uses the deployment connection budget.
    # Runtime callers provide this instance's latest configuration.
    mode = 'auto' if config is not None else 'deployment'
    target = target_workers(config) if config is not None else None
    required = fixed + target * per_worker if target is not None else None
    requested = required if config is not None else _integer('VIDEO_DB_CONNECTION_BUDGET', 48, 8)
    deployment_cap = (_integer('VIDEO_DB_CONNECTION_BUDGET', 48, 8)
                      if 'VIDEO_DB_CONNECTION_BUDGET' in os.environ else None)
    worker_cap = (_integer('VIDEO_WORKER_DB_CONNECTION_BUDGET', 48, per_worker)
                  if 'VIDEO_WORKER_DB_CONNECTION_BUDGET' in os.environ or config is None else None)
    total = min(requested, deployment_cap) if deployment_cap is not None else requested
    if fixed + per_worker > total:
        raise ValueError(f'数据库连接预算至少需要 {fixed + per_worker} 个，以覆盖主服务、辅助进程和一个下载进程。')
    workers = (total - fixed) // per_worker
    if worker_cap is not None:
        workers = min(workers, worker_cap // per_worker)
    return {'total': total, 'main': main, 'auxiliary': auxiliary, 'processing': processing,
            'per_worker': per_worker, 'workers': workers, 'mode': mode,
            'requested_total': requested, 'required_total': required, 'target_workers': target,
            'deployment_cap': deployment_cap, 'deployment_worker_cap': worker_cap,
            'target_supported': target is None or workers >= target}


def calculate_pool_size(is_worker=False, worker_count=None):
    if is_worker:
        return worker_pool_size()
    budget = connection_budget()
    if worker_count is None:
        worker_count = int(os.environ['VIDEO_THROUGHPUT_WORKERS']) if 'VIDEO_THROUGHPUT_WORKERS' in os.environ else None
    if worker_count is not None and int(worker_count) > budget['workers']:
        raise ValueError('Planned download workers exceed VIDEO_DB_CONNECTION_BUDGET')
    return budget['main']
