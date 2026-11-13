# 配置定义与维护边界

配置的默认值、整数范围和字段分类统一定义在 `webapp/config/schema.py`，`webapp/config_schema.py` 保留为兼容导出入口。
该模块仅依赖 Python 标准库，磁盘、OSS、实例和调度模块均可直接引用，避免循环导入。

## 一项配置在哪里定义

`SETTINGS` 中每个键对应一个不可变的 `Setting`：

- `default` / `factory`：默认值；列表用 `factory=list`，通过 `default_settings()` 获取独立副本。
- `limits`：整数校验范围。布尔值、字符串以及复杂结构继续使用原有校验规则。
- `scope`：历史上的实例本地字段或任务策略字段分类。这不是跨实例共享配置的开关。
- `download`：是否属于现有下载参数接口允许使用的字段。
- `resource_override`：是否允许通过实例卡片的资源配置接口覆盖。
- `persist_runtime`：是否写入独立模式的运行摘要；不是“是否支持热修改”的标记。
- `pipeline_key`：是否参与现有流水线策略指纹中的 JSON 摘要。
- `family`：磁盘、OSS、YouTube 请求节奏等字段分组。
- `custom`：跳过普通文本校验，由原有领域校验处理，例如 URL 和 Cookie 列表。
- `secret`：沿用现有 OSS 接口的脱敏字段集合。

`DEFAULTS`、`INTEGER_FIELDS`、`DOWNLOAD_FIELDS`、`POLICY_FIELDS`、`LOCAL_FIELDS`、
`RESOURCE_LIMITS` 等集合从这些定义生成。原模块仍导出原有公开名称，调用者不必批量迁移。
磁盘和 OSS 模块也继续导出原有的默认值、字段集合和整数范围名称。

字段分类有意保留现有差异。例如 `download_format` 属于任务策略字段，但不在
`DOWNLOAD_FIELDS` 中；`builtin_transfer_limit` 允许实例资源覆盖，
`builtin_resolver_limit` 则不允许。不要为了统一外观而扩大接口白名单。

## 新增或删除参数

1. 在 `SETTINGS` 中维护默认值、范围及消费者标记，不再到多个模块手写同名集合。
2. 枚举、领域约束和跨字段约束继续放在 `settings.py` 或相应领域模块中。
3. 实际功能、前端输入和运行时读取仍需按业务需求实现；元数据不自动改变调度算法。
4. 已删除但旧前端仍可能发送的键放入 `RETIRED_FIELDS`，入口和校验使用同一个清理函数。
5. 有意修改配置契约时，审核并更新 `tests/fixtures/settings_contract.json` 及对应行为测试。
   不要在测试运行时从当前实现生成预期值，否则无法发现意外变化。

MySQL / Redis 连接由启动环境管理，实例地址等由部署模块管理，不属于此配置表。文件职责、重建和重启要求见 [部署手册](../deployment/docker.md)。

## 回归检查

`tests/fixtures/settings_contract.json` 保存经过审核的行为契约，不能从当前实现重新生成预期值来掩盖变化。修改配置时按影响运行参数、保存、热生效和页面回归：

```text
python -m unittest tests.test_config_schema -v
node tests/test_polling.cjs
python scripts/verify/verify_settings_save.py
python scripts/verify/verify_settings_save.py --hybrid
python scripts/verify/verify_hot_policy.py
python scripts/verify/verify_hot_policy.py --hybrid
```

浏览器测试使用临时配置、测试 Cookie 和独立 SQLite 数据库，不读取业务 `.env`。
MySQL 集成测试沿用 CI 的隔离数据库；本地需显式提供测试专用的
`VIDEO_TEST_MYSQL_URL`，不能使用业务数据库连接。

完整测试入口以 CI 和 [验证脚本](../../scripts/verify/README.md) 为准，容量边界另按 [容量测试](capacity-testing.md) 验证。
