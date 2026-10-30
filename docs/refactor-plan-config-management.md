# 配置管理重构方案

## 当前问题

配置来源分散在 **5 个地方**：
1. `config.yaml` - 本地配置文件
2. 环境变量 - `VIDEO_*` 前缀（30+ 个）
3. MySQL `settings` 表 - fleet_budget、downloads_paused 等
4. `fleet-runtime.json` - 实例运行时配置
5. 启动参数 - `--database-url`、`--instance-id` 等

**优先级不明确：**
- `app.py:112` 先读 config.yaml
- `app.py:115` fleet 模式从 MySQL 覆盖
- `web_worker.py:36` 执行时又合并 `store.runtime()`

**结果：**
- 修改配置后不知道哪个生效
- 不知道要不要重启
- 调试时无法追踪配置来源

---

## 重构目标

### 1. 明确的配置优先级

```
环境变量 > MySQL settings > config.yaml > 默认值
```

**解释：**
- 环境变量：容器编排/部署工具设置（最高优先级）
- MySQL settings：管理界面运行时修改（需重启生效的除外）
- config.yaml：本地开发配置
- 默认值：代码内置

### 2. 统一配置接口

```python
from webapp.config import ConfigManager

config = ConfigManager(
    env_file='.env',
    config_file='config.yaml',
    database_url='mysql://...',
    state_dir='.cache/web'
)

# 获取合并后的配置
merged = config.get_merged()

# 查询单个配置项
proxy_mode = config.get('proxy_mode')

# 追踪来源
print(config.explain('proxy_mode'))
# 输出: proxy_mode='clash' (来源: MySQL settings, 于 2026-10-30 14:23:45 由 admin 修改)
```

---

## 实现方案

### webapp/config_manager.py

```python
"""统一配置管理。"""
import os
import json
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional
import yaml

class ConfigSource:
    """配置源抽象。"""
    def __init__(self, name: str, priority: int):
        self.name = name
        self.priority = priority  # 数字越大优先级越高
    
    def load(self) -> Dict[str, Any]:
        """加载配置，返回 dict。"""
        raise NotImplementedError
    
    def explain(self, key: str) -> Optional[str]:
        """说明某个 key 的来源和时间。"""
        return None


class EnvSource(ConfigSource):
    """环境变量配置源。"""
    def __init__(self, prefix='VIDEO_'):
        super().__init__('环境变量', priority=400)
        self.prefix = prefix
        self._mappings = {
            # 环境变量名 -> 配置 key
            'VIDEO_DATABASE_URL': 'database_url',
            'VIDEO_INSTANCE_ID': 'instance_id',
            'VIDEO_INSTANCE_NAME': 'instance_name',
            'VIDEO_INSTANCE_URL': 'instance_url',
            'VIDEO_SERVICE_ROLE': 'service_role',
            'VIDEO_DB_POOL_SIZE': 'db_pool_size',
            'VIDEO_WORKER_DB_POOL_SIZE': 'worker_db_pool_size',
            'VIDEO_ALLOW_LOCAL_SETUP': 'allow_local_setup',
            'VIDEO_TRUSTED_PROXY': 'trusted_proxy',
            'VIDEO_WEB_HOSTS': 'web_hosts',
            'VIDEO_WEB_HTTPS': 'web_https',
        }
    
    def load(self) -> Dict[str, Any]:
        config = {}
        for env_key, config_key in self._mappings.items():
            value = os.environ.get(env_key)
            if value is not None:
                # 类型转换
                if config_key in ('db_pool_size', 'worker_db_pool_size'):
                    config[config_key] = int(value)
                elif config_key in ('allow_local_setup', 'trusted_proxy', 'web_https'):
                    config[config_key] = value.lower() in ('true', '1', 'yes')
                elif config_key == 'web_hosts':
                    config[config_key] = value.split(',')
                else:
                    config[config_key] = value
        return config
    
    def explain(self, key: str) -> Optional[str]:
        # 反向查找环境变量名
        env_key = next((k for k, v in self._mappings.items() if v == key), None)
        if env_key and os.environ.get(env_key):
            return f'来源: {self.name} ({env_key})'
        return None


class MySQLSettingsSource(ConfigSource):
    """MySQL settings 表配置源。"""
    def __init__(self, store):
        super().__init__('MySQL settings', priority=300)
        self.store = store
        self._history = {}  # key -> (value, timestamp, user)
    
    def load(self) -> Dict[str, Any]:
        config = {}
        try:
            with self.store.connect() as db:
                from sqlalchemy import select
                from webapp.database import settings
                rows = db.execute(select(settings)).mappings().all()
                for row in rows:
                    key = row['key']
                    value = row['value']
                    # 解析特殊类型
                    if key == 'fleet_budget':
                        config['fleet_budget'] = json.loads(value) if value else {}
                    elif key == 'downloads_paused':
                        config['downloads_paused'] = (value == 'true')
                    elif key in ('fleet_policy', 'download_active_config'):
                        config[key] = json.loads(value) if value else {}
                    else:
                        config[key] = value
        except Exception:
            # 数据库未就绪时跳过
            pass
        return config
    
    def explain(self, key: str) -> Optional[str]:
        if key in self._history:
            value, ts, user = self._history[key]
            return f'来源: {self.name}, 于 {ts} 由 {user} 修改'
        return None


class YAMLFileSource(ConfigSource):
    """config.yaml 配置源。"""
    def __init__(self, file_path):
        super().__init__('config.yaml', priority=200)
        self.file_path = Path(file_path)
    
    def load(self) -> Dict[str, Any]:
        if not self.file_path.is_file():
            return {}
        with self.file_path.open(encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    
    def explain(self, key: str) -> Optional[str]:
        config = self.load()
        if key in config:
            return f'来源: {self.name}'
        return None


class DefaultSource(ConfigSource):
    """默认值配置源。"""
    def __init__(self, defaults):
        super().__init__('默认值', priority=100)
        self.defaults = defaults
    
    def load(self) -> Dict[str, Any]:
        return self.defaults.copy()
    
    def explain(self, key: str) -> Optional[str]:
        if key in self.defaults:
            return f'来源: {self.name} (代码内置)'
        return None


class ConfigManager:
    """统一配置管理器。"""
    def __init__(self, env_file=None, config_file=None, store=None, defaults=None):
        """
        Args:
            env_file: .env 文件路径（可选，优先从环境变量读取）
            config_file: config.yaml 路径
            store: TaskStore 实例（用于读 MySQL）
            defaults: 默认配置字典
        """
        self.sources = []
        
        # 按优先级添加配置源
        if defaults:
            self.sources.append(DefaultSource(defaults))
        
        if config_file:
            self.sources.append(YAMLFileSource(config_file))
        
        if store:
            self.sources.append(MySQLSettingsSource(store))
        
        self.sources.append(EnvSource())
        
        # 按优先级排序（高优先级在后，覆盖前面的）
        self.sources.sort(key=lambda s: s.priority)
        
        self._merged_cache = None
        self._cache_time = None
    
    def get_merged(self, *, use_cache=True, cache_ttl=5.0) -> Dict[str, Any]:
        """获取合并后的配置。
        
        Args:
            use_cache: 是否使用缓存
            cache_ttl: 缓存有效期（秒）
        """
        now = datetime.now().timestamp()
        if use_cache and self._merged_cache and (now - self._cache_time) < cache_ttl:
            return self._merged_cache.copy()
        
        merged = {}
        for source in self.sources:
            config = source.load()
            merged.update(config)
        
        self._merged_cache = merged
        self._cache_time = now
        return merged.copy()
    
    def get(self, key: str, default=None) -> Any:
        """获取单个配置项。"""
        merged = self.get_merged()
        return merged.get(key, default)
    
    def explain(self, key: str) -> str:
        """解释配置项来源。"""
        # 从高优先级往低优先级找
        for source in reversed(self.sources):
            explanation = source.explain(key)
            if explanation:
                return explanation
        return f'{key}: 未配置'
    
    def dump(self) -> str:
        """导出所有配置及来源（调试用）。"""
        merged = self.get_merged()
        lines = ['当前生效配置:\n']
        for key in sorted(merged.keys()):
            value = merged[key]
            # 脱敏处理
            if 'password' in key.lower() or 'secret' in key.lower() or 'token' in key.lower():
                value = '***'
            lines.append(f'  {key} = {value!r}')
            lines.append(f'    {self.explain(key)}')
        return '\n'.join(lines)
    
    def invalidate_cache(self):
        """使缓存失效（配置修改后调用）。"""
        self._merged_cache = None
        self._cache_time = None
```

---

## 使用示例

### app.py 中初始化

```python
from webapp.config_manager import ConfigManager
from webapp.settings import DEFAULTS

def create_app(...):
    # ... 初始化 store ...
    
    config_manager = ConfigManager(
        config_file=config_path,
        store=store,
        defaults=DEFAULTS
    )
    app.extensions['config_manager'] = config_manager
    
    # 获取合并配置
    config = config_manager.get_merged()
    
    # 设置 Flask 配置
    app.config.update(
        TRUSTED_HOSTS=config.get('web_hosts', ['localhost', '127.0.0.1']),
        # ...
    )
```

### 路由中使用

```python
@app.get('/api/config')
def read_config():
    config_mgr = current_app.extensions['config_manager']
    return jsonify(config_mgr.get_merged())

@app.get('/api/config/explain/<key>')
def explain_config(key):
    config_mgr = current_app.extensions['config_manager']
    return jsonify(explanation=config_mgr.explain(key))
```

### 诊断页面

```python
@app.get('/api/config/debug')
@console_auth.guard(roles=['admin'])
def debug_config():
    """管理员专用：查看所有配置来源。"""
    config_mgr = current_app.extensions['config_manager']
    return Response(config_mgr.dump(), mimetype='text/plain')
```

---

## 迁移步骤

### Phase 1: 实现 ConfigManager（1 天）
1. 创建 `webapp/config_manager.py`
2. 单元测试覆盖
3. 不改现有代码

### Phase 2: 在 app.py 中引入（2 天）
1. 在 `create_app` 初始化 ConfigManager
2. 逐步替换 `load_config()` 调用
3. 保留旧接口兼容

### Phase 3: 清理环境变量读取（3 天）
1. 统一环境变量命名（VIDEO_ 前缀）
2. 移除散落的 `os.environ.get`
3. 全部改为 `config_manager.get()`

### Phase 4: 添加诊断接口（1 天）
1. `/api/config/debug` 路由
2. `/api/config/explain/<key>` 路由
3. 前端展示配置来源

---

## 收益

### 1. 可追溯性
```bash
# 用户问："我改了 proxy_mode 为什么不生效？"
curl http://localhost:5000/api/config/explain/proxy_mode
# 输出: "来源: MySQL settings, 于 2026-10-30 14:23:45 由 admin 修改"
# 说明: 环境变量被数据库配置覆盖
```

### 2. 配置验证
```python
class ConfigManager:
    def validate(self) -> List[str]:
        """检查配置冲突和缺失。"""
        errors = []
        config = self.get_merged()
        
        # 检查必填项
        if not config.get('database_url'):
            errors.append('database_url 未配置')
        
        # 检查冲突
        if config.get('proxy_mode') == 'clash' and not config.get('clash_config'):
            errors.append('proxy_mode=clash 但 clash_config 未配置')
        
        return errors
```

### 3. 运行时修改
```python
@app.put('/api/config/<key>')
@console_auth.guard(roles=['admin'])
def update_config(key):
    """通过管理界面修改配置。"""
    value = request.json.get('value')
    
    # 写入 MySQL
    with store.connect() as db:
        db.execute(settings.update().where(settings.c.key == key).values(value=value))
    
    # 使缓存失效
    config_mgr = current_app.extensions['config_manager']
    config_mgr.invalidate_cache()
    
    return jsonify(message='配置已更新', requires_restart=_needs_restart(key))

def _needs_restart(key):
    """判断配置项是否需要重启生效。"""
    restart_keys = {'database_url', 'instance_id', 'db_pool_size'}
    return key in restart_keys
```

---

## 时间估算

- Phase 1: 1 天（8 小时）
- Phase 2: 2 天（16 小时）
- Phase 3: 3 天（24 小时）
- Phase 4: 1 天（8 小时）

**总计：7 天**（56 小时），单人可完成。
