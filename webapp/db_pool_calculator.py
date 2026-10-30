"""数据库连接池动态计算。"""
import os


def calculate_pool_size(is_worker=False, worker_count=None):
    """根据角色和并发数动态分配连接池大小。

    Args:
        is_worker: 是否为 worker 子进程
        worker_count: 总 worker 数（仅主进程需要）

    Returns:
        (pool_size, max_overflow)
    """
    if is_worker:
        # Worker 进程：每个最多 2 个连接（业务写入 + 心跳读取）
        # 不需要 overflow，避免连接数不可控
        pool_size = int(os.environ.get('VIDEO_WORKER_DB_POOL_SIZE', '2'))
        return (pool_size, 0)

    # 主进程：根据总预算分配
    total_budget = int(os.environ.get('VIDEO_DB_CONNECTION_BUDGET', '48'))

    # 预留给 worker（默认 16 个，每个 2 连接）
    if worker_count is None:
        worker_count = int(os.environ.get('VIDEO_THROUGHPUT_WORKERS', '16'))
    worker_reserved = worker_count * 2

    # 主进程可用 = 总预算 - worker 预留
    main_available = max(5, total_budget - worker_reserved)

    # pool_size 取 60%，max_overflow 取 40%
    pool_size = max(2, int(main_available * 0.6))
    max_overflow = max(0, main_available - pool_size)

    return (pool_size, max_overflow)


if __name__ == '__main__':
    # 打印当前配置
    print('当前连接池配置：')
    print('=' * 60)

    main_pool, main_overflow = calculate_pool_size(is_worker=False, worker_count=16)
    print(f'主进程: pool_size={main_pool}, max_overflow={main_overflow}')
    print(f'  → 最多 {main_pool + main_overflow} 个连接')

    worker_pool, worker_overflow = calculate_pool_size(is_worker=True)
    print(f'Worker 进程: pool_size={worker_pool}, max_overflow={worker_overflow}')

    total_workers = int(os.environ.get('VIDEO_THROUGHPUT_WORKERS', '16'))
    total = (main_pool + main_overflow) + total_workers * worker_pool
    print(f'\n总计（1 主 + {total_workers} worker）：{total} 个连接')

    budget = int(os.environ.get('VIDEO_DB_CONNECTION_BUDGET', '48'))
    print(f'预算: {budget}')
    if total > budget:
        print(f'⚠️  警告：总连接数 {total} 超过预算 {budget}')
    else:
        print(f'✓ 在预算内（余量 {budget - total}）')
