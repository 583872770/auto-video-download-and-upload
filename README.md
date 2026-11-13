# YouTube 视频下载与交付工作台

基于 Flask 的批量下载服务，支持客户任务、多实例协作、账号登录与实例绑定、下载进度、文件交付和计费。客户可任选字幕、无声视频、音频、音视频成品，共 15 种非空组合。

日常流程：**创建任务 → 设置交付要求 → 添加视频 → 选择实例并开始下载 → 核对交付与账单**。账号从新增或导入开始，每个账号只保留一份当前 Cookie；再次登录成功会替换旧凭据。

完整说明从 [文档导航](docs/README.md) 进入。常用入口：[部署](docs/deployment/docker.md) · [任务操作](docs/guides/tasks.md) · [账号管理](docs/guides/accounts.md) · [交付内容](docs/guides/delivery.md) · [故障恢复](docs/operations/recovery.md)。

## Docker 部署

正式部署只保留两种方式，均使用 MySQL 和 Redis：

| 场景 | 编排文件 | 环境模板 |
| --- | --- | --- |
| 应用 + MySQL + Redis 一起部署 | `compose.local.yaml` | `.env.local.example` |
| 连接远程 MySQL + Redis | `compose.remote.yaml` | `.env.remote.example` |

全新服务器一起部署示例；已有 `.env` 请直接编辑：

```bash
cp .env.local.example .env
# 填写数据库密码、账号加密密钥，并调整端口和视频目录
docker compose -f compose.local.yaml config --quiet
docker compose -f compose.local.yaml up -d --build
```

远程方案换用对应模板和编排，填写 `MYSQL_*`、`REDIS_URL`。浏览器、FFmpeg、Node.js 和 Mihomo 已包含在镜像内。首次设置、访问地址、保留原数据卷、立即停止更新及备份步骤见 [部署手册](docs/deployment/docker.md)。

## 本地开发

需要 Python 3.13+、FFmpeg、Node.js 或 Deno。

系统设置已迁入 Vue 独立页面 `/settings`。本机运行前需用 Node.js 22.12+ 在 `frontend/` 执行 `npm.cmd ci`、`npm.cmd run build`；Docker 会自动打包。开发监听与验证见[前端开发](docs/development/project-layout.md#前端开发)。其他页面暂沿用现有实现。

安装后端依赖，在 `.env` 配置数据库和 Redis 后运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe app.py
```

默认打开 `http://127.0.0.1:5000/setup`，使用终端中的部署设置访问码配置实例；管理账户在 `/login` 初始化。代码职责与配置维护见 [项目结构](docs/development/project-layout.md)、[配置说明](docs/development/configuration.md)，测试入口见 [验证脚本](scripts/verify/README.md)。

仅保留近期仍有用的[发布与检查记录](docs/archive/README.md)，旧方案和重复总结不再存放仓库。项目仅处理用户有权下载的内容。
