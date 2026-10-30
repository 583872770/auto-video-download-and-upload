# app.py 重构方案

## 目标结构

```
app/
├── __init__.py              # 导出 create_app
├── factory.py               # 应用工厂（原 create_app 函数）
├── extensions.py            # store/catalog/clash 等扩展初始化
├── middleware.py            # CSRF、响应头、错误处理
├── routes/
│   ├── __init__.py         # 注册所有 Blueprint
│   ├── media.py            # /api/media, /media/*
│   ├── tasks.py            # /api/tasks/*
│   ├── config.py           # /api/config, /api/status
│   ├── youtube.py          # /api/youtube/*
│   ├── disk.py             # /api/disk/*
│   └── health.py           # /health
└── utils.py                # settings(), sync_runtime() 等辅助函数
```

## 迁移步骤

### Phase 1: 提取扩展初始化（最低风险）

**app/extensions.py**
```python
"""Flask 扩展初始化。"""
import threading
from pathlib import Path
from webapp.catalog import MediaCatalog
from webapp.clash import ClashManager
from webapp.console_auth import ConsoleAuth
from webapp.youtube_auth import YouTubeAuth
from webapp.startup import DownloadStartup
from webapp.execution import ExecutionControlService

def init_extensions(app, store, config_path, service_role, instance_id, instance_url, instance_name):
    """初始化所有扩展并注册到 app.extensions。"""
    catalog = MediaCatalog(store)
    app.extensions['media_catalog'] = catalog
    
    settings_lock = threading.RLock()
    media_lock = threading.RLock()
    app.extensions['settings_lock'] = settings_lock
    app.extensions['media_lock'] = media_lock
    
    clash = ClashManager(store.directory / 'clash')
    app.extensions['clash'] = clash
    
    fleet = None
    if service_role or instance_id:
        from webapp.fleet import Fleet
        from Utils.config import load_config, DEFAULT_CONFIG
        from webapp.settings import DEFAULTS
        fleet = Fleet(store, service_role, identity=instance_id, url=instance_url, name=instance_name)
        initial, initial_base = load_config(Path(config_path or DEFAULT_CONFIG).resolve())
        fleet.initialize({**DEFAULTS, **initial}, initial_base, clash.directory)
    app.extensions['fleet'] = fleet
    
    youtube_auth = YouTubeAuth(store)
    app.extensions['youtube_auth'] = youtube_auth
    
    startup = DownloadStartup()
    app.extensions['download_startup'] = startup
    
    execution_control = ExecutionControlService(store)
    app.extensions['execution_control'] = execution_control
    
    console_auth = ConsoleAuth(app, store)
    app.extensions['console_auth'] = console_auth
    
    return {
        'catalog': catalog,
        'settings_lock': settings_lock,
        'media_lock': media_lock,
        'clash': clash,
        'fleet': fleet,
        'youtube_auth': youtube_auth,
        'startup': startup,
        'execution_control': execution_control,
        'console_auth': console_auth,
    }
```

**使用方式：**
```python
# app.py 中
from app.extensions import init_extensions

def create_app(...):
    # ... 前置配置 ...
    exts = init_extensions(app, store, config_path, service_role, instance_id, instance_url, instance_name)
    
    # 后续路由可以通过 from flask import current_app 访问
    # catalog = current_app.extensions['media_catalog']
```

### Phase 2: 提取辅助函数

**app/utils.py**
```python
"""应用级辅助函数。"""
import json
from pathlib import Path
from Utils.config import load_config

def make_settings_loader(config_path, fleet):
    """返回 settings() 闭包函数。"""
    def settings():
        from webapp.settings import DEFAULTS
        config, base = load_config(config_path)
        if fleet and fleet.can_download:
            config.update(json.loads(fleet.row()['limits']))
        return config, base
    return settings

def make_sync_runtime(settings_func, fleet, store, config_path, clash_dir):
    """返回 sync_runtime() 闭包函数。"""
    def sync_runtime(config, base):
        from webapp.settings import DEFAULTS
        from Utils.config import resolve_path
        if fleet:
            fleet.save_local({**DEFAULTS, **config}, base, clash_dir)
            return
        cookie = str(resolve_path(base, config['youtube_cookie'])) if config.get('youtube_cookie') else ''
        store.set_runtime({**DEFAULTS, **config, 'youtube_cookie': cookie})
        store.set_download_settings({**DEFAULTS, **config}, base, clash_dir)
    return sync_runtime
```

### Phase 3: 拆分路由到 Blueprint

**app/routes/media.py**
```python
"""素材相关路由。"""
from flask import Blueprint, jsonify, request, send_file, current_app
from pathlib import Path
from Utils.paths import io_path
from Utils.config import video_directory
from webapp.media import VIDEO_SUFFIXES

bp = Blueprint('media', __name__, url_prefix='/api/media')

@bp.get('/')
def list_media():
    catalog = current_app.extensions['media_catalog']
    # ... 原 app.py 中的逻辑 ...
    
@bp.post('/')
def import_media():
    store = current_app.extensions['task_store']
    # ... 原逻辑 ...

# /media/videos/<path:name> 和 /media/covers/<path:name> 
# 由于 url_prefix 不同，需要单独注册或放在主 app
```

**app/routes/__init__.py**
```python
"""注册所有 Blueprint。"""
from flask import Flask

def register_routes(app: Flask):
    """注册所有路由模块。"""
    from . import media, tasks, config, youtube, disk, health
    
    app.register_blueprint(media.bp)
    app.register_blueprint(tasks.bp)
    app.register_blueprint(config.bp)
    app.register_blueprint(youtube.bp)
    app.register_blueprint(disk.bp)
    app.register_blueprint(health.bp)
```

**app/factory.py**
```python
"""Flask 应用工厂。"""
from flask import Flask
from .extensions import init_extensions
from .middleware import register_middleware
from .routes import register_routes
from .utils import make_settings_loader, make_sync_runtime

def create_app(config_path=None, state_dir=None, *, start_worker=True, ...):
    configure_runtime()
    # ... 数据库配置 ...
    
    app = Flask(__name__)
    app.request_class = BudgetRequest
    # ... 基础配置 ...
    
    # 初始化扩展
    exts = init_extensions(app, store, config_path, service_role, instance_id, instance_url, instance_name)
    
    # 注册辅助函数
    settings = make_settings_loader(config_path, exts['fleet'])
    sync_runtime = make_sync_runtime(settings, exts['fleet'], store, config_path, exts['clash'].directory)
    app.extensions['settings'] = settings
    app.extensions['sync_runtime'] = sync_runtime
    
    # 注册中间件和错误处理
    register_middleware(app, exts)
    
    # 注册路由
    register_routes(app)
    
    # 启动 worker
    if start_worker:
        from web_worker import Worker
        worker = Worker(store, store.directory, config_path, settings, sync_runtime, exts['clash'])
        app.extensions['worker'] = worker
        worker.start()
    
    return app
```

## 迁移收益

### 1. 可测试性
```python
# 测试单个路由
def test_media_list():
    from app.routes.media import bp
    app = Flask(__name__)
    app.register_blueprint(bp)
    with app.test_client() as client:
        resp = client.get('/api/media/')
        assert resp.status_code == 200
```

### 2. 并行开发
- 不同开发者修改不同 Blueprint，减少合并冲突
- 路由文件控制在 100-200 行，易于理解

### 3. 按需加载
```python
# 只启动管理端，不加载下载路由
def create_admin_app():
    app = create_app(start_worker=False)
    # 不注册 tasks.bp 和 youtube.bp
    return app
```

## 风险控制

### 渐进式迁移
1. **先提取 extensions.py**（不改路由逻辑）
2. **再提取 middleware.py**（不改路由逻辑）
3. **逐个拆分路由**（每次提交一个 Blueprint）
4. **最后重命名** `app.py` → `app/factory.py`

### 兼容性保证
```python
# 保留 app.py 作为入口点
from app import create_app

if __name__ == '__main__':
    # 原启动逻辑不变
    app = create_app()
    app.run()
```

### 回归测试
每次迁移后运行：
```bash
python -m unittest discover -s tests -v
python scripts/verify_web.py
```

## 时间估算

- Phase 1: 2-3 小时（extensions.py + utils.py）
- Phase 2: 5-8 小时（拆分 6 个 Blueprint）
- Phase 3: 1-2 小时（测试 + 文档）

总计：**8-13 小时**，建议分 3 个 PR 完成。
