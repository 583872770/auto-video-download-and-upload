# 项目目录结构与整理边界

更新时间：2026-11-11

## 当前边界

当前项目可以正常构建和部署，但根目录同时承担源码、部署、文档、测试和本地运行数据。整理时先保持以下启动契约不变：

- `app.py` 继续导出 `create_app`，不创建会遮蔽它的顶层 `app/` 包。
- `web_worker.py` 继续作为下载工作进程入口。
- `Dockerfile`、`docker/`、`compose*.yaml` 保持可直接构建和启动。
- `templates/`、`static/`、`Utils/` 和 `webapp/` 的现有导入路径分批迁移。
- `.cache/web`、视频目录、Cookie、MySQL/Redis 数据和部署密钥不参与自动清理。

## 当前布局

```text
项目根目录/
├─ app.py                         Flask 工厂与 CLI 兼容入口
├─ web_worker.py                  下载进程兼容入口
├─ webapp/                        后端实现
│  ├─ routes/                     HTTP 路由和 Blueprint
│  ├─ application.py              应用装配与后台启动顺序
│  ├─ middleware.py               认证、CSRF、响应头和错误处理
│  ├─ services/                   配置、账号、任务与工作台业务编排
│  ├─ repositories/               TaskStore 数据访问与工作台数据库快照
│  ├─ worker/                     下载执行、工作目录、续租、结算和恢复
│  ├─ adapters/                   下载器进度桥接、渠道执行与实例 RPC
│  ├─ runtime/                    后台任务监督器
│  └─ config/                     配置字段、默认值、校验与保存
├─ Utils/                         兼容工具层，逐步减少新增代码
├─ templates/                     页面模板
├─ static/                        前端资源
├─ Uploaders/                     上传适配器
├─ docker/                        镜像启动、健康检查和容器工具
├─ scripts/                       可重复执行的运维脚本
│  ├─ verify/
│  └─ verify_*.py                  原路径兼容入口，可执行也可导入
├─ tests/                          自动化测试
├─ docs/                           文档
│  ├─ architecture/
│  └─ archive/
└─ .runtime/                       本地测试、归档与临时状态，不进 Git
```

## 分阶段迁移

### 阶段一：盘点和隔离

建立目录和引用清单，标记持久化状态、备份、测试产物和可删除临时文件。不删除任何内容。

### 阶段二：运行产物整理

把测试截图、临时数据库、旧发布包和诊断日志归入 `.runtime/test-artifacts`、`.runtime/backups`、`.runtime/deploy`。先复制或移动已经确认不被运行代码读取的目录，再观察一轮测试和本地启动。

`.cache/web` 仍保留原路径，直到数据库迁移、登录状态和启动脚本全部支持新路径。

### 阶段三：文档和脚本归类

按用途归类文档和脚本。旧脚本路径先保留兼容包装器，更新 CI、Dockerfile 和部署说明后再移除旧入口。

### 阶段四：后端模块拆分

先提取应用初始化和中间件，再按路由、服务、数据访问和外部适配器拆分。每次只迁移一组调用，保留原公开接口和 `app.py` 兼容入口。

2026-11-11：本轮核心后端拆分已完成。应用初始化、中间件、入口原有路由、业务编排、TaskStore 数据访问、下载工作进程、渠道桥接和配置实现已分别归入对应模块。后台组件在全部路由及恢复回调注册后启动。

`app.py`、`web_worker.py`、`webapp/store.py`、`webapp/tasks.py` 保留原公开接口。`webapp/settings.py` 和 `webapp/config_schema.py` 只转出新配置包的实现，默认值没有维护两份。

既有独立领域模块（调度、账号池、租约、资源保护等）、`Utils/` 和前端资源继续沿用现有路径。它们不需要为了目录外观再次搬动；后续新增功能按上述职责放置。Compose 和 Docker 入口仍在原处，持久化路径不迁移。

完整职责说明和本轮验收见 [后端整理验收](backend-refactor-validation-20261111.md)。第一批历史记录见 [应用初始化拆分验证](application-initialization-validation-20261111.md)。

## 每批变更的验收条件

- `python scripts/test_imports.py`
- `python docker/check_startup.py`
- 后端单元测试和对应功能回归
- 前端 JavaScript 语法检查
- Docker 镜像构建检查
- 本地配置、Cookie、任务和数据库连接路径未改变

任何阶段都不直接清理生产数据库、Cookie、视频、`.cache/web` 或服务器上的持久化卷。
