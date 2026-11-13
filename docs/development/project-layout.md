# 项目结构与维护边界

更新时间：2026-11-13。命令从项目根目录运行，文档统一从 [文档导航](../README.md) 查阅。

## 入口与模块

| 路径 | 职责 |
| --- | --- |
| `app.py` | Flask 工厂与 Web / CLI 兼容入口，继续导出 `create_app` |
| `web_worker.py` | 下载工作进程入口 |
| `webapp/application.py`、`middleware.py` | 应用装配、启动顺序、认证及请求处理 |
| `webapp/routes/` | HTTP 路由与 Blueprint |
| `webapp/services/` | 任务、配置、账号及工作台业务编排 |
| `webapp/repositories/` | TaskStore 数据访问与工作台快照 |
| `webapp/worker/` | 执行、工作目录、续租、结算与恢复 |
| `webapp/adapters/` | 下载器进度、渠道执行与实例 RPC |
| `webapp/runtime/` | 后台组件监督 |
| `webapp/config/` | 参数定义、默认值、校验和保存 |
| `webapp/delivery/` | 交付要求、产物执行、校验、存储与 API |
| `Utils/` | 兼容工具及下载实现 |
| `templates/`、`static/` | 页面模板与前端资源 |
| `docker/` | 镜像启动、浏览器桌面、健康检查与打包验证 |
| `scripts/verify/` | 可重复执行的验证脚本，`scripts/verify_*.py` 为原路径兼容入口 |
| `tests/`、`.github/workflows/` | 自动化测试与 CI |

既有独立领域模块继续负责账号池、调度、租约和资源保护等逻辑；不为目录外观再次移动运行模块。`webapp/store.py`、`tasks.py`、`settings.py`、`config_schema.py` 继续保留公开兼容接口。

## 部署与文档目录

```text
项目根目录/
├─ README.md                     项目介绍与最短启动路径
├─ Dockerfile、.dockerignore      镜像构建
├─ compose.local.yaml            应用 + MySQL + Redis
├─ compose.remote.yaml           应用连接远程 MySQL、Redis
├─ .env.local.example            本地一起部署的环境模板
├─ .env.remote.example           远程服务环境模板
├─ docs/
│  ├─ README.md                  当前文档导航与维护规则
│  ├─ deployment/                部署、迁移和浏览器环境
│  ├─ guides/                    页面与功能使用
│  ├─ operations/                调度、账号池、资源和恢复
│  ├─ development/               实现、配置与验证
│  └─ archive/                   少量近期发布、回滚与检查记录
└─ scripts/verify/README.md       验证脚本约定
```

当前说明采用小写连字符文件名，近期发布记录使用日期前缀；已由真实页面取代的原型不再保留。

## 持久化与变更验证

`.env`、根配置、`.cache/web`、视频、账号凭据、数据库和部署密钥属于实际配置或运行状态，不随代码和文档整理移动、覆盖或清理。Docker 配置卷、状态卷、MySQL 卷及下载绑定目录保留现有部署的项目名和路径。

后端变更按影响运行模块导入、`docker/check_startup.py` 和相应业务回归；前端变更检查 JavaScript 与页面行为；部署变更检查 Compose 解析及打包。单纯文档移动检查本地链接、标题锚点、原型路径和仓库引用，无需启动下载或连接生产服务。

模块拆分完成后直接维护本手册，不再保留逐批迁移计划和重复验收报告。
