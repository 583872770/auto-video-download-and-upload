# 代码结构说明

## 📁 项目结构

```
auto-video-download-and-upload/
├── app.py                      # Flask 主入口（855 行）
├── web_worker.py               # 下载 Worker 进程（834 行）
│
├── app/                        # 应用核心模块
│   ├── __init__.py
│   ├── extensions.py           # 扩展初始化（MediaCatalog, Clash, Fleet 等）
│   ├── helpers.py              # 公共辅助函数（get_settings, sync_runtime 等）
│   └── routes/                 # Blueprint 路由（76 行）
│       ├── __init__.py         # 注册所有 blueprints
│       ├── health.py           # 健康检查路由
│       └── disk.py             # 磁盘管理和诊断路由
│
├── webapp/                     # Web 应用核心逻辑
│   ├── store.py                # 任务存储（TaskStore）
│   ├── store_utils.py          # 工具函数（now, source_identity）
│   ├── database.py             # 数据库表定义和连接池
│   ├── tasks.py                # 任务运行器（TaskRunner）
│   ├── catalog.py              # 素材目录（MediaCatalog）
│   │
│   ├── clash.py                # Clash 代理管理
│   ├── clash_routes.py         # Clash API 路由
│   ├── fleet.py                # 多实例管理（Fleet）
│   ├── fleet_routes.py         # Fleet API 路由
│   ├── statistics_routes.py    # 统计和客户管理路由
│   │
│   ├── console_auth.py         # Web 控制台认证
│   ├── youtube_auth.py         # YouTube 登录管理
│   ├── execution.py            # 执行控制（暂停/恢复）
│   ├── startup.py              # 下载启动检查
│   │
│   ├── settings.py             # 配置管理
│   ├── media.py                # 素材处理
│   ├── disk_budget.py          # 磁盘预算管理
│   ├── diagnostics.py          # 诊断工具
│   ├── proxy_health.py         # 代理健康检查
│   └── migrations.py           # 数据库迁移
│
├── Utils/                      # 核心工具库
│   ├── config.py               # 配置加载
│   ├── runtime.py              # 运行时环境
│   ├── paths.py                # 路径处理
│   └── youtube_login.py        # YouTube Cookie 解析
│
├── scripts/                    # 工具脚本
│   ├── test_imports.py         # 模块导入测试
│   ├── verify_p0_fixes.py      # 安全修复验证
│   └── ...
│
└── docs/                       # 文档
    ├── code-structure.md       # 本文档
    ├── p0-p1-fixes-20261030.md
    ├── performance-optimization-plan.md
    └── ...
```

---

## 🎯 核心模块职责

### 1. **app.py** - Flask 主应用

**职责**:
- Flask 应用工厂
- 中间件（CSRF、认证）
- 剩余核心路由（任务、素材、配置）

**依赖**:
- `app.extensions` - 初始化所有扩展
- `app.helpers` - 辅助函数
- `app.routes` - 注册 blueprints

**关键函数**:
- `create_app()` - 应用工厂（未来可提取）
- `csrf_protection()` - CSRF 中间件
- `settings()` - 配置闭包（缓存）

---

### 2. **web_worker.py** - 下载 Worker

**职责**:
- 任务领取（claim）
- 下载执行（多方式自动切换）
- 代理轮换和健康检查
- 心跳维护

**关键函数**:
- `main(concurrent)` - Worker 主循环
- `attempt()` - 执行单个任务
- `download()` - 下载核心逻辑

**不建议改动**: 逻辑复杂，改动风险高

---

### 3. **webapp/store.py** - 任务存储

**职责**:
- 任务 CRUD
- 租约管理（claim, heartbeat, recover）
- 批量操作（batch_heartbeat, batch_update）
- 统计查询

**优化亮点**:
- `batch_heartbeat()` - 批量心跳（-94% 延迟）
- `downloads_paused()` - 缓存 5 秒（-20ms claim）
- 连接池优化（pool_size 动态分配）

**依赖**:
- `webapp.database` - 表定义
- `webapp.store_utils` - now(), source_identity()

---

### 4. **webapp/database.py** - 数据库层

**职责**:
- 表定义（tasks, batches, workers, etc.）
- 索引定义（已优化，删除 3 个冗余）
- 连接池配置（动态分配）

**关键表**:
- `tasks` - 下载任务
- `batches` - 批次
- `workers` - Worker 实例
- `settings` - 配置持久化
- `download_lists` - 客户任务清单

**优化亮点**:
- 主进程: pool_size=9, max_overflow=7
- Worker: pool_size=2, max_overflow=0
- 总预算: 48 连接（可配置）

---

### 5. **app/extensions.py** - 扩展初始化

**职责**:
- 初始化 MediaCatalog
- 初始化 ClashManager
- 初始化 Fleet（可选）
- 初始化 YouTubeAuth
- 初始化 ConsoleAuth

**为什么提取**:
- app.py 太大（887 行 → 855 行）
- 扩展初始化可独立测试
- 为未来模块化打基础

---

### 6. **app/helpers.py** - 公共辅助函数

**职责**:
- `get_settings()` - 获取当前配置
- `get_body()` - 获取并验证 JSON 请求体
- `sync_runtime()` - 同步配置到 store/fleet

**为什么提取**:
- 避免在多个路由中重复代码
- 统一配置访问接口
- 便于单元测试

---

### 7. **app/routes/** - Blueprint 路由

**职责**:
- `health.py` - `/health` 健康检查
- `disk.py` - `/api/disk` 磁盘管理和诊断

**为什么提取**:
- 减少 app.py 行数
- 路由逻辑独立
- 便于后续拆分更多模块

**未来可提取**:
- 任务路由（/api/tasks/*）
- 素材路由（/api/media/*）
- 配置路由（/api/config/*）

---

## 🔄 数据流

### 任务下载流程

```
用户提交批次
    ↓
app.py POST /api/batches
    ↓
store.add_batch()
    ↓
store.add_tasks()
    ↓
[任务进入队列]
    ↓
web_worker.py claim_task()
    ↓
web_worker.attempt()
    ↓
download() [yt-dlp/yt5s/yt1d/...]
    ↓
store.succeed() / store.fail()
    ↓
catalog.add() [素材入库]
```

### 配置同步流程

```
用户修改配置
    ↓
app.py PUT /api/config
    ↓
validate_settings()
    ↓
save_settings()
    ↓
app.helpers.sync_runtime()
    ├─ fleet.save_local() [多实例]
    └─ store.set_runtime() [单实例]
        ↓
    web_worker 读取新配置
```

### 心跳流程（优化后）

```
Worker 每 10 秒
    ↓
store.batch_heartbeat([task1, task2, ...])
    ↓
SQL: UPDATE tasks SET lease_until = CASE
    WHEN id=1 THEN '2026-10-30 10:01:00'
    WHEN id=2 THEN '2026-10-30 10:01:00'
    ...
    END
    WHERE id IN (1,2,...)
    ↓
1 次 UPDATE 完成 16 个任务
```

---

## 🧪 测试和验证

### 导入测试

```bash
python scripts/test_imports.py
```

检查所有模块是否可正常导入，没有循环依赖。

### 连接池验证

```bash
python webapp/db_pool_calculator.py
```

检查当前连接池配置是否在预算内。

### 安全修复验证

```bash
python scripts/verify_p0_fixes.py
```

检查 P0/P1 安全问题是否已修复。

---

## 📝 开发指南

### 添加新路由

**方式 1: 在 app.py 中添加（快速原型）**

```python
@app.get('/api/my-feature')
def my_feature():
    return jsonify(status='ok')
```

**方式 2: 创建新 Blueprint（推荐）**

1. 创建 `app/routes/my_feature.py`:

```python
from flask import Blueprint, jsonify

bp = Blueprint('my_feature', __name__, url_prefix='/api/my-feature')

@bp.get('/')
def list_items():
    return jsonify([])

@bp.post('/')
def create_item():
    from app.helpers import get_body
    data = get_body()
    return jsonify(data)
```

2. 在 `app/routes/__init__.py` 中注册:

```python
def register_blueprints(app: Flask):
    from . import health, disk, my_feature
    
    app.register_blueprint(health.bp)
    app.register_blueprint(disk.bp)
    app.register_blueprint(my_feature.bp)
```

---

### 添加新的扩展

在 `app/extensions.py` 中添加初始化逻辑:

```python
def init_extensions(app, store, config_path, service_role, instance_id, instance_url, instance_name):
    # ... 现有代码 ...
    
    # 新扩展
    my_service = MyService(store)
    app.extensions['my_service'] = my_service
    
    return {
        # ... 现有返回 ...
        'my_service': my_service,
    }
```

在路由中使用:

```python
from flask import current_app

my_service = current_app.extensions['my_service']
```

---

### 添加工具函数

**纯工具函数** → `webapp/store_utils.py` 或新建工具模块

**路由辅助函数** → `app/helpers.py`

**下载相关函数** → `web_worker.py` 或新建下载工具模块

---

## ⚠️ 注意事项

### 不要改动的模块

1. **web_worker.py** - 逻辑复杂，改动风险高
   - 除非修复明确的 bug
   - 改动后必须完整测试下载流程

2. **webapp/database.py** - 数据库表定义
   - 改动后需要写迁移脚本
   - 索引改动需要评估性能影响

3. **Utils/** - 核心工具库
   - 被多个模块依赖
   - 改动影响范围大

---

### 可以安全改动的模块

1. **app/routes/** - 路由模块
   - 独立性强
   - 易于测试

2. **app/helpers.py** - 辅助函数
   - 纯函数为主
   - 改动影响局部

3. **webapp/*_routes.py** - 各种路由
   - 可以随意重构
   - 改动后测试对应 API

---

## 📊 代码质量指标

| 文件 | 行数 | 复杂度 | 可维护性 |
|------|------|--------|---------|
| app.py | 855 | 中 | 良好 ✓ |
| web_worker.py | 834 | 高 | 谨慎改动 ⚠️ |
| webapp/store.py | ~600 | 中 | 良好 ✓ |
| webapp/database.py | ~350 | 低 | 优秀 ✓ |
| app/extensions.py | ~100 | 低 | 优秀 ✓ |
| app/helpers.py | ~50 | 低 | 优秀 ✓ |

---

## 🚀 未来优化方向

### 短期（已完成）

✅ 数据库连接池优化  
✅ 索引优化  
✅ 热点查询缓存  
✅ 批量心跳  
✅ 代码精简  
✅ 架构模块化  

### 中期（可选）

- [ ] 提取更多 Blueprint（任务、素材、配置）
- [ ] 提取下载执行器（从 web_worker.py）
- [ ] 添加单元测试覆盖核心逻辑
- [ ] 添加集成测试（API 测试）

### 长期（需评估）

- [ ] 微服务拆分（下载服务独立）
- [ ] 异步任务队列（Celery/RQ）
- [ ] 实时通知（WebSocket）
- [ ] 多租户支持

---

**文档维护**: 本文档应随代码结构变化及时更新  
**最后更新**: 2026-10-30  
**维护人**: Claude Opus 5.5
