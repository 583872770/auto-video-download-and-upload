# 目录整理盘点（2026-11-11）

本盘点记录 2026-11-11 整理完成后的目录边界。所有移动均为保留式归档，没有删除运行数据。

## 目录职责

| 目录 | 当前内容 | 处理建议 |
| --- | --- | --- |
| `webapp/` | 已按 `routes/services/repositories/worker/adapters/config/runtime` 拆分核心入口实现；保留独立领域模块 | 新功能按职责归入对应模块 |
| `templates/`、`static/` | 页面模板、前端脚本和样式 | 保持现有路径，避免影响 Flask 和静态资源引用 |
| `Utils/` | 配置、路径、存储和上传相关通用工具 | 暂不移动，先减少新增跨层依赖 |
| `docker/` | 入口、健康检查、启动检查和镜像辅助程序 | 保持现有路径，Dockerfile 直接引用 |
| `scripts/` | 验证实现集中在 `verify/`；保留部署、迁移、登录脚本及旧验证路径兼容入口 | 验证脚本内部直接引用新路径，兼容入口导入不执行验证 |
| `tests/` | 单元、集成、浏览器和部署回归测试 | 保持根路径，后续可按领域分组 |
| `docs/` | 架构、运维、设计、调查和历史部署报告 | 新增分类目录，历史报告逐步归档 |
| `.cache/` | 当前仅保留 Web 持久化状态、迁移备份和部署审计 | 保持原路径，不直接清理 `web` 或 `backups` |
| `.runtime/` | 已归档的历史测试数据库、截图、日志、部署中间产物和临时脚本 | 本地运行归档，不进 Git；确认备份后按保留策略清理 |
| `Cookies/`、`videos/`、`Others/` | 本地运行数据或素材 | 不纳入源码整理和自动清理 |
| `.venv/`、`.tools/`、`.idea/` | 本机开发环境 | 保持本机目录，不作为项目源码发布内容 |

## 已确认的持久化路径

以下路径已有代码或部署文档直接引用，不能直接改名：

- `.cache/web`
- `.cache/backups`
- `templates/`
- `static/`
- `Utils/`
- `scripts/login_youtube.py`
- `scripts/migrate_mysql.py`
- `Dockerfile`、`docker/` 和 `compose*.yaml`

## `.cache` 整理结果

已将 82 个历史目录和 420 个散落文件移动到 `.runtime/cache-archive-20261111/`，并将本次整理辅助脚本放入归档的 `root-files/`。当前归档实际占用约 14.8GB（约 13.7GiB），以 `manifest.json` 和磁盘统计为准。当前 `.cache/` 只保留：

- `web/`：本地 Web 持久化状态
- `backups/`：数据库和配置备份
- `deploy-budget/`：最近一次部署审计记录

归档目录包含 `manifest.json`，记录了迁移时间、文件数量和原始大小；后续补入的整理辅助脚本也已登记。迁移是保留文件的移动操作，没有删除数据。

## 本轮完成情况

- 已建立目标目录结构和迁移边界文档。
- 已将验证脚本实现集中至 `scripts/verify/`，原路径保留兼容入口，并同步 CI 引用。
- 已将 44 份无引用历史文档归档到 `docs/archive/`。
- 已将历史 `.cache` 内容归档到 `.runtime/cache-archive-20261111/`，并更新了文本引用。
- 未删除或移动任何运行数据。
- 未修改 `.env`、Cookie、视频、数据库或部署卷。
- 核心后端拆分及验收记录见 [后端整理验收](backend-refactor-validation-20261111.md)。
