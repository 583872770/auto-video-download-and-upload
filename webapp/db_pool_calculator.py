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


def connection_budget():
    """Reserve main + two auxiliary lanes before admitting download workers.

    This is an instance budget, not the MySQL server's max_connections. Operators
    sharing a database must allocate a suitable budget to each instance.
    """
    total = _integer('VIDEO_DB_CONNECTION_BUDGET', 48, 8)
    per_worker = sum(worker_pool_size())
    auxiliary = _integer('VIDEO_DB_AUXILIARY_CONNECTIONS', 2 * per_worker, 2 * per_worker)
    main = (_integer('VIDEO_DB_POOL_SIZE', 5, 2), _integer('VIDEO_DB_MAX_OVERFLOW', 2))
    if sum(main) + auxiliary + per_worker > total:
        raise ValueError('VIDEO_DB_CONNECTION_BUDGET cannot cover the main pool, auxiliary lanes and one worker')
    workers = min((total - sum(main) - auxiliary) // per_worker,
                  _integer('VIDEO_WORKER_DB_CONNECTION_BUDGET', 48, per_worker) // per_worker)
    return {'total': total, 'main': main, 'auxiliary': auxiliary,
            'per_worker': per_worker, 'workers': workers}


def calculate_pool_size(is_worker=False, worker_count=None):
    if is_worker:
        return worker_pool_size()
    budget = connection_budget()
    if worker_count is None:
        worker_count = int(os.environ['VIDEO_THROUGHPUT_WORKERS']) if 'VIDEO_THROUGHPUT_WORKERS' in os.environ else None
    if worker_count is not None and int(worker_count) > budget['workers']:
        raise ValueError('Planned download workers exceed VIDEO_DB_CONNECTION_BUDGET')
    return budget['main']
