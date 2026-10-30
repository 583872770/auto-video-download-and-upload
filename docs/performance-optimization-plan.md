# 性能优化方案

## 一、数据库层优化

### 🔴 P0: 连接池耗尽风险

**问题分析：**
```
主进程：pool_size=5 + max_overflow=5 = 10
Worker 进程（自动模式默认 16 个）：16 * (2+0) = 32
浏览器池进程：估计 3-5 个 * 2 = 6-10

总计：10 + 32 + 10 = 52 个连接（理论最小值）
实际峰值：主进程满载 + 所有 worker 满载 = 10 + 16*2 + 10 = 52

MySQL 默认 max_connections=151，看似足够
但多实例部署：3 台实例 * 52 = 156 > 151 → 连接被拒绝
```

**优化方案：**

#### 方案 1: 动态连接池预算（推荐）

```python
# webapp/database.py

import psutil

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
        return (2, 0)
    
    # 主进程：根据总预算分配
    total_budget = int(os.environ.get('VIDEO_DB_CONNECTION_BUDGET', '48'))
    
    # 预留给 worker（假设 16 个，每个 2 连接）
    worker_reserved = (worker_count or 16) * 2
    
    # 主进程可用 = 总预算 - worker 预留
    main_available = max(5, total_budget - worker_reserved)
    
    # pool_size 取 60%，max_overflow 取 40%
    pool_size = int(main_available * 0.6)
    max_overflow = main_available - pool_size
    
    return (pool_size, max_overflow)

class Database:
    def __init__(self, ..., is_worker=False):
        self.is_worker = is_worker
        # ...
        if self.url:
            pool_size, max_overflow = calculate_pool_size(
                is_worker=is_worker,
                worker_count=int(os.environ.get('VIDEO_THROUGHPUT_WORKERS', '16'))
            )
            options.update(
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=15,
                pool_pre_ping=True,
                pool_recycle=300
            )
```

#### 方案 2: 连接池监控与告警

```python
# webapp/diagnostics.py

def monitor_connection_pool():
    """监控连接池使用情况。"""
    from webapp.database import _engines
    
    stats = []
    for url, engine in _engines.items():
        pool = engine.pool
        stats.append({
            'url': url.replace(url.password, '***') if url.password else url,
            'size': pool.size(),           # 当前连接数
            'checked_out': pool.checkedout(),  # 正在使用的连接数
            'overflow': pool.overflow(),   # 溢出连接数
            'max_overflow': pool._max_overflow,
            'usage': f'{pool.checkedout()}/{pool.size() + pool._max_overflow}'
        })
    
    return stats

@app.get('/api/diagnostics/db-pool')
@console_auth.guard(roles=['admin'])
def db_pool_status():
    """查看数据库连接池状态。"""
    return jsonify(pools=monitor_connection_pool())
```

**部署建议：**
```env
# .env 配置
# 单台服务器（MySQL max_connections=151）
VIDEO_DB_CONNECTION_BUDGET=48

# 多实例（3 台，MySQL max_connections=500）
VIDEO_DB_CONNECTION_BUDGET=150  # 每台 150，总计 450
```

---

### 🔴 P1: N+1 查询优化

<function_calls>
<invoke name="Bash">
<parameter name="command">cd "D:\ProgramData\auto-video-download-and-upload" && grep -n 'for.*in.*execute\|for.*in.*all()' webapp/store.py webapp/statistics.py webapp/jobs.py | head -20