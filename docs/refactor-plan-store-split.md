# store.py 重构方案：职责分离

## 当前问题

**TaskStore 类 985 行，60 个方法**，职责混杂：
- 数据库连接与事务
- 任务 CRUD（创建、查询、分页）
- 计数器与状态机（transition）
- 租约管理（claim、release、heartbeat）
- Fleet 集成（实例协调）
- 快照管理（payload 编码）
- 统计与清单（statistics、lists）

**核心矛盾：**
- 所有方法都在同一个事务上下文中操作
- 无法独立测试某个子功能
- 修改计数器逻辑可能影响租约管理

## 重构目标

拆分为 **4 层架构**：

```
webapp/
├── repository/             # 数据访问层（纯 SQL）
│   ├── __init__.py
│   ├── task_repository.py     # 任务表 CRUD
│   ├── counter_repository.py  # 计数器表操作
│   ├── snapshot_repository.py # 快照表操作
│   └── fleet_repository.py    # Fleet 相关表
├── services/              # 业务逻辑层
│   ├── __init__.py
│   ├── task_service.py       # 任务创建、查询、分页
│   ├── claim_service.py      # 租约管理（claim/release/heartbeat）
│   ├── counter_service.py    # 计数器逻辑
│   └── transition_service.py # 状态机转换
├── store.py               # 统一门面（兼容层）
└── database.py            # 表定义（不变）
```

---

## Phase 1: 提取 Repository 层（纯数据访问）

### task_repository.py

```python
"""任务表数据访问层。"""
from sqlalchemy import and_, func, or_, select
from webapp.database import tasks

class TaskRepository:
    def __init__(self, db):
        """db 是 SQLAlchemy connection。"""
        self.db = db
    
    def find_by_id(self, task_id):
        """查询单个任务。"""
        return self.db.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
    
    def find_claimable(self, actions, exclude, stamp, instance_id=None, limit=1, skip_locked=True):
        """查询可领取任务（支持 FOR UPDATE SKIP LOCKED）。
        
        返回原始行，不包含 decode 逻辑。
        """
        statement = select(tasks).where(
            tasks.c.status == 'queued',
            tasks.c.available_at <= stamp
        )
        if actions:
            statement = statement.where(tasks.c.action.in_(actions))
        if exclude:
            statement = statement.where(~tasks.c.action.in_(exclude))
        if instance_id:
            statement = statement.where(
                or_(tasks.c.target_instance_id == instance_id,
                    tasks.c.target_instance_id.is_(None))
            )
        statement = statement.order_by(tasks.c.available_at, tasks.c.seq).limit(limit)
        
        if skip_locked:
            statement = statement.with_for_update(skip_locked=True)
        else:
            statement = statement.with_for_update()
        
        return self.db.execute(statement).mappings().first()
    
    def update_status(self, task_id, status, **kwargs):
        """更新任务状态及其他字段。"""
        values = {'status': status, **kwargs}
        self.db.execute(tasks.update().where(tasks.c.id == task_id).values(**values))
    
    def count_by_status(self, scope_filter=None):
        """按状态聚合计数。"""
        statement = select(tasks.c.status, func.count()).group_by(tasks.c.status)
        if scope_filter:
            # 例如 scope_filter = {'action': 'download'}
            for col, val in scope_filter.items():
                statement = statement.where(getattr(tasks.c, col) == val)
        return {row[0]: row[1] for row in self.db.execute(statement)}
    
    def page(self, limit, offset, filters=None, order_by='seq'):
        """分页查询。"""
        statement = select(tasks)
        if filters:
            for col, val in filters.items():
                statement = statement.where(getattr(tasks.c, col) == val)
        if order_by == 'finished_at':
            statement = statement.order_by(tasks.c.finished_at.desc(), tasks.c.seq.desc())
        else:
            statement = statement.order_by(tasks.c.seq.desc())
        statement = statement.limit(limit).offset(offset)
        return self.db.execute(statement).mappings().all()
```

### counter_repository.py

```python
"""计数器表数据访问层。"""
from sqlalchemy import select
from webapp.database import counters

class CounterRepository:
    def __init__(self, db):
        self.db = db
    
    def increment(self, scope, status, delta):
        """增量更新计数器（使用 UPSERT）。"""
        from webapp.database import Database
        db_instance = Database(None, None)  # 只用 upsert 方法
        self.db.execute(db_instance.upsert(
            counters,
            dict(scope=scope, status=status, value=delta),
            ['scope', 'status'],
            {'value': counters.c.value + delta}
        ))
    
    def get(self, scope, status):
        """查询单个计数器值。"""
        row = self.db.execute(
            select(counters.c.value).where(
                counters.c.scope == scope,
                counters.c.status == status
            )
        ).scalar_one_or_none()
        return row or 0
    
    def get_all(self, scopes):
        """批量查询计数器。"""
        rows = self.db.execute(
            select(counters).where(counters.c.scope.in_(scopes))
        ).mappings().all()
        result = {}
        for row in rows:
            result.setdefault(row['scope'], {})[row['status']] = row['value']
        return result
```

---

## Phase 2: 提取 Service 层（业务逻辑）

### claim_service.py

```python
"""租约管理服务。"""
import time
from webapp.repository.task_repository import TaskRepository
from webapp.repository.counter_repository import CounterRepository

class ClaimService:
    def __init__(self, store):
        """store 是原 TaskStore 实例，保持向后兼容。"""
        self.store = store
    
    def claim(self, worker_id, actions, exclude, lease_seconds):
        """领取一个待执行任务。
        
        返回完整的 decoded task（包含 payload）。
        """
        with self.store.connect() as db:
            if self.store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            
            stamp = self._get_timestamp(db)
            
            # 检查 fleet_budget（P0-1 修复后使用共享锁）
            if self.store.fleet or self.store.single:
                self._check_budget(db)
            
            # 检查各种暂停条件
            if self._should_skip_download(db):
                actions = [a for a in (actions or []) if a != 'download']
            
            # 使用 Repository 查询可领取任务
            repo = TaskRepository(db)
            instance_id = self.store.fleet.id if self.store.fleet else None
            row = repo.find_claimable(actions, exclude, stamp, instance_id, skip_locked=True)
            
            if row is None:
                return None
            
            # 状态转换（使用 TransitionService）
            from webapp.services.transition_service import TransitionService
            transition_svc = TransitionService(self.store)
            transition_svc.transition(db, row, 'running')
            
            # 更新任务字段
            values = self._prepare_claim_values(db, row, worker_id, lease_seconds)
            repo.update_status(row['id'], 'running', **values)
            
            # Decode 并返回
            return self.store.decode({**row, **values}, private=True, db=db)
    
    def release(self, task_id, worker_id):
        """释放任务租约。"""
        with self.store.connect() as db:
            # ... 原逻辑，使用 Repository ...
            pass
    
    def heartbeat(self, task_id, worker_id, lease_seconds):
        """续租任务。"""
        with self.store.connect() as db:
            # ... 原逻辑 ...
            pass
    
    # 私有辅助方法
    def _get_timestamp(self, db):
        return self.store.fleet.clock(db) if self.store.fleet else time.time()
    
    def _check_budget(self, db):
        from sqlalchemy import select
        from webapp.database import settings
        db.execute(select(settings.c.value).where(
            settings.c.key == 'fleet_budget'
        ).with_for_update(read=True)).first()
    
    def _should_skip_download(self, db):
        """检查是否应跳过下载任务。"""
        if self.store.claims_paused(db, lock=True):
            return True
        if not self.store._released_queued_count(db):
            return True
        from webapp.disk_budget import resource_blocked
        if resource_blocked(self.store, db):
            return True
        return False
    
    def _prepare_claim_values(self, db, row, worker_id, lease_seconds):
        """准备 claim 时要更新的字段。"""
        values = {
            'worker_id': worker_id or self.store.owner,
            'started_at': now(),
            'finished_at': None,
            'lease_until': time.time() + lease_seconds,
            'attempts': row['attempts'] + 1,
            'error': None,
        }
        if self.store.fleet:
            values.update(
                instance_id=self.store.fleet.id,
                lease_until=self.store.fleet.clock(db) + lease_seconds
            )
        return values
```

### transition_service.py

```python
"""状态机转换服务。"""
from collections import Counter
from webapp.repository.counter_repository import CounterRepository

class TransitionService:
    def __init__(self, store):
        self.store = store
    
    def transition(self, db, row, new_status):
        """执行状态转换，更新计数器。
        
        Args:
            db: 数据库连接
            row: 任务行（dict）
            new_status: 新状态
        """
        scopes = self._build_scopes(row)
        counter_repo = CounterRepository(db)
        
        # 锁定计数器行（按字母顺序避免死锁）
        for scope in sorted(scopes):
            changes = Counter({new_status: 1})
            if row.get('status'):
                changes[row['status']] -= 1
            
            for status in sorted(changes):
                delta = changes[status]
                if delta:
                    counter_repo.increment(scope, status, delta)
    
    def _build_scopes(self, row):
        """构建计数器 scope 列表。"""
        scopes = ['action:' + row['action']]
        if row.get('batch_id'):
            scopes.append('batch:' + row['batch_id'])
        return scopes
```

---

## Phase 3: 保留 store.py 作为门面（兼容层）

```python
"""任务存储统一门面（向后兼容）。"""
from webapp.services.claim_service import ClaimService
from webapp.services.task_service import TaskService
from webapp.services.counter_service import CounterService

class TaskStore:
    def __init__(self, directory, database_url=None, *, startup_report=None):
        # ... 原初始化逻辑 ...
        self._claim_service = None
        self._task_service = None
        self._counter_service = None
    
    # 懒加载服务
    @property
    def claim_service(self):
        if self._claim_service is None:
            self._claim_service = ClaimService(self)
        return self._claim_service
    
    @property
    def task_service(self):
        if self._task_service is None:
            self._task_service = TaskService(self)
        return self._task_service
    
    # 保留原有公开方法，委托给 Service
    def _claim(self, worker_id, actions, exclude, lease_seconds):
        """兼容方法：委托给 ClaimService。"""
        return self.claim_service.claim(worker_id, actions, exclude, lease_seconds)
    
    def create(self, action, payload):
        """兼容方法：委托给 TaskService。"""
        return self.task_service.create(action, payload)
    
    def page(self, *, limit=50, cursor=None, **kwargs):
        """兼容方法：委托给 TaskService。"""
        return self.task_service.page(limit, cursor, **kwargs)
    
    # 直接暴露 Repository（给内部使用）
    def task_repo(self, db):
        from webapp.repository.task_repository import TaskRepository
        return TaskRepository(db)
    
    def counter_repo(self, db):
        from webapp.repository.counter_repository import CounterRepository
        return CounterRepository(db)
```

---

## 迁移收益

### 1. 可测试性
```python
def test_claim_service():
    """独立测试租约逻辑，mock Repository。"""
    mock_repo = Mock(spec=TaskRepository)
    mock_repo.find_claimable.return_value = {'id': 'task123', 'status': 'queued', ...}
    
    service = ClaimService(store)
    task = service.claim('worker1', ['download'], None, 60)
    
    assert task['status'] == 'running'
    mock_repo.update_status.assert_called_once()
```

### 2. 性能优化空间
```python
# Repository 层可以专门优化 SQL
class TaskRepository:
    def find_claimable_batch(self, count=10):
        """批量领取（减少事务次数）。"""
        # ... 一次 SELECT FOR UPDATE 返回 10 条 ...
```

### 3. 事务边界清晰
```python
# Service 层明确控制事务范围
def claim(self, ...):
    with self.store.connect() as db:
        # 最小化锁持有时间
        row = repo.find_claimable(...)  # 持锁查询
        # 释放锁
    
    # 外部处理（不持锁）
    decoded = self._process_payload(row)
    
    with self.store.connect() as db:
        # 二次写入
        repo.update_status(...)
```

---

## 迁移步骤

### Week 1: Repository 层（无风险）
1. 创建 `webapp/repository/` 目录
2. 提取 `task_repository.py`（纯查询，不改 store.py）
3. 提取 `counter_repository.py`
4. 单元测试覆盖（mock 数据库）

### Week 2: Service 层（中等风险）
1. 创建 `webapp/services/` 目录
2. 提取 `claim_service.py`（复制原逻辑）
3. 在 `store.py` 中添加委托方法
4. 集成测试验证（真实数据库）

### Week 3: 切换调用方（高风险）
1. `web_worker.py` 改为调用 `store.claim_service.claim()`
2. 其他模块逐步切换
3. 删除 `store.py` 中的原方法

### Week 4: 优化与文档
1. Repository 层 SQL 优化
2. Service 层添加日志
3. 更新架构文档

---

## 风险控制

### 双写验证
```python
def _claim(self, ...):
    # 同时调用旧逻辑和新逻辑
    old_result = self._claim_old(...)
    new_result = self.claim_service.claim(...)
    
    # 记录差异
    if old_result != new_result:
        logger.error(f'Mismatch: old={old_result}, new={new_result}')
    
    return old_result  # 暂时返回旧结果
```

### 功能开关
```python
USE_NEW_CLAIM_SERVICE = os.environ.get('VIDEO_USE_NEW_CLAIM') == 'true'

def _claim(self, ...):
    if USE_NEW_CLAIM_SERVICE:
        return self.claim_service.claim(...)
    else:
        return self._claim_old(...)
```

---

## 时间估算

- Repository 层：1 周（40 小时）
- Service 层：1 周（40 小时）
- 调用方切换：5 天（30 小时）
- 测试与优化：3 天（20 小时）

**总计：3-4 周**（130 小时），建议至少 2 人协作。
