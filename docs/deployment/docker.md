# Docker 部署

只保留以下两种部署方式，二选一，均使用 MySQL 和 Redis。

| 场景 | 编排文件 | 环境模板 | 启动的服务 |
| --- | --- | --- | --- |
| 应用、MySQL、Redis 一起部署 | `compose.local.yaml` | `.env.local.example` | app、mysql、redis |
| 应用连接远程 MySQL、Redis | `compose.remote.yaml` | `.env.remote.example` | app |

两份编排独立使用，不再叠加账号或 IPv6 覆盖文件。每个 app 同时提供管理页面和下载执行，账号在页面新增、导入并绑定实例，无需按账号再部署容器。

需要 Docker Engine 和 Docker Compose V2。镜像包含 FFmpeg / ffprobe、Node.js、Mihomo、Chromium、Xvfb 和 noVNC。以下命令在部署服务器的项目目录执行；Windows PowerShell 将 `cp` 换成 `Copy-Item` 即可。

两套编排共用同一 Dockerfile。构建时自动安装锁定的前端依赖、检查 TypeScript 并打包 Vue 系统设置；运行时由 Flask 在 `/settings` 提供静态资源，无需新增前端容器、端口或 CDN。打包源码必须包含 `frontend/`，宿主机的 `node_modules` 和本地构建产物不会进入镜像上下文。

## 应用、MySQL、Redis 一起部署

新部署复制模板；已有 `.env` 请直接编辑，避免覆盖原密码、端口和实例设置：

```bash
cp .env.local.example .env
```

在 `.env` 中填写 `MYSQL_PASSWORD`，并设置视频目录和访问地址。应用固定连接 Docker 内网的 `mysql:3306` 和 `redis:6379/0`；本方案忽略环境文件中的 `MYSQL_HOST`、`MYSQL_PORT`、`REDIS_URL`，不发布数据库端口。MySQL 与 Redis 健康后才启动应用。

```bash
docker compose -f compose.local.yaml config --quiet
docker compose -f compose.local.yaml up -d --build
docker compose -f compose.local.yaml ps
docker compose -f compose.local.yaml logs --tail=100 app mysql redis
```

MySQL 8.4 数据持久化到 `mysql_data` 卷。首次初始化时 root 和应用账号使用同一个 `MYSQL_PASSWORD`，应用使用专用账号 `video`，不能设为 root。已有数据卷的密码不会随 `.env` 自动更新，改密码需先在数据库中修改账号，再同步环境文件。

`MYSQL_INNODB_BUFFER_POOL_SIZE` 默认 2G，小内存机器请降低，例如 256M。Redis 7.4 仅用于实时进度缓存，限制缓存内存为 256 MB，不持久化；重建不影响 MySQL 中的任务、账号和账单。

## 连接远程 MySQL、Redis

先准备 MySQL 数据库及专用账号，授权应用服务器访问，建库与权限见 [MySQL 连接与迁移](mysql.md#直接连接已有-mysql)。远程 Redis 同样需要允许应用服务器连接。

```bash
cp .env.remote.example .env
```

填写 `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_DATABASE`、`MYSQL_USER`、`MYSQL_PASSWORD`、`REDIS_URL`。MySQL 支持 5.7.9+ 和 8.x，不支持 MariaDB。主机字段只填写域名或 IP，不带协议和端口。

```dotenv
MYSQL_HOST=mysql.example.com
MYSQL_PORT=3306
MYSQL_DATABASE=video_downloads
MYSQL_USER=video
MYSQL_PASSWORD='替换为数据库原始密码'
REDIS_URL='redis://:经过URL编码的密码@redis.example.com:6379/0'
```

MySQL 密码使用原始值，单引号保留 `$`、`#` 等字符；Redis URL 中的用户名、密码需要 URL 编码，无密码时使用 `redis://redis.example.com:6379/0`，TLS 使用 `rediss://`。多实例共享同一 MySQL 和 Redis 库，各自使用独立状态卷，详见 [多实例部署](instances.md)。

```bash
docker compose -f compose.remote.yaml config --quiet
docker compose -f compose.remote.yaml up -d --build
docker compose -f compose.remote.yaml ps
docker compose -f compose.remote.yaml logs --tail=100 app
```

缺少 MySQL 地址、密码或 Redis URL 时，Compose 会在启动前报错。此方案只启动 app，不创建或管理远程数据库。Redis 故障时进度缓存仍会自动降级到 MySQL，恢复后重试缓存连接。

## 配置文件职责

| 文件 / 位置 | 用途 |
| --- | --- |
| `.env` | 数据库、Redis、端口、目录、实例初始信息及密钥；不提交 Git |
| 两份 `compose.*.yaml` | 容器、端口映射、健康检查和持久卷 |
| `Dockerfile`、`.dockerignore` | 构建运行镜像；排除本机配置、Cookie、日志和视频 |
| `docker/config.yaml` | 首次启动复制到配置卷的下载默认值 |
| 容器 `/data/config/config.yaml` | 当前实例的下载策略，后续由页面维护；重建不覆盖 |
| 容器 `/app/.cache/web` | 实例身份、部署设置、管理登录密钥等持久状态 |
| 根目录 `config.yaml`、`run.ps1` | 本机开发及命令行使用，不挂载进正式 Docker 容器 |

环境模板包含常用设置；OSS 临时凭据可按需在 `.env` 增加 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_SECURITY_TOKEN`，两套编排均支持。

账号新增、导入需要 `VIDEO_ACCOUNT_ENCRYPTION_KEY`。新安装用 `openssl rand -base64 32` 生成一次并填入 `.env`；所有共享数据库实例使用同一密钥。已有部署保留原密钥，备份时一起保存；应用不会自动生成或替换此密钥。

MySQL 连接池可用模板中的 `VIDEO_DB_*`、`VIDEO_WORKER_DB_*` 调整，多个实例需合计数据库连接预算。实例 ID 留空会自动生成；升级时保留状态卷，新实例不要复制其他实例的状态目录。

构建源可修改 `DEBIAN_MIRROR`、`PIP_INDEX_URL`，洛杉矶服务器可分别使用 `https://deb.debian.org`、`https://pypi.org/simple`。Mihomo 下载源可按需设置 `MIHOMO_DOWNLOAD_BASE_URL`。

## 访问与首次设置

默认页面只监听宿主机 `127.0.0.1:5000`。需要局域网或公网访问时，将 `WEB_BIND_IP` 改为 `0.0.0.0` 或服务器内网 IP，设置 `WEB_PORT`、`VIDEO_WEB_HOSTS`；域名 / IP 列表不含协议和端口，并保留 `127.0.0.1` 供健康检查使用。HTTPS 反向代理可设置 `VIDEO_WEB_HTTPS=1`。

`VIDEO_DOWNLOAD_DIR` 必须是 Docker 引擎所在机器的绝对路径，例如 Linux `/srv/youtube-videos`，Windows Docker Desktop `D:/youtube-videos`。页面内的下载目录使用容器路径 `/data/videos`。

打开 `/setup`，输入启动日志中的“部署设置访问码”，测试连接并配置实例。MySQL、Redis 连接由启动环境管理；实例设置保存到状态卷的 `deployment.json`，重启后生效。管理账户在 `/login` 初始化，初始设置码位于 `/app/.cache/web/console-bootstrap.key`，可通过 `docker compose -f 所选编排文件 exec app cat /app/.cache/web/console-bootstrap.key` 读取。

noVNC 默认只监听宿主机 `127.0.0.1:6080`，通过 SSH 隧道访问：

```bash
ssh -L 16080:127.0.0.1:6080 用户@服务器
```

然后打开 `http://127.0.0.1:16080/vnc.html`；修改 `YT5S_DESKTOP_PORT` 后相应调整隧道目标端口。浏览器运行与排错见 [浏览器环境](browser.md)。

## 更新、停止与恢复

以下以本地方案为例；远程方案将文件名替换为 `compose.remote.yaml`。每次使用相同的环境文件及项目名。保留兼容的默认项目名：本地 `youtube-downloader-local`，远程 `youtube-downloader`；若旧部署用了自定义 `-p`，后续也必须带同一个 `-p`，避免另建空数据卷。

旧 `compose.local-mysql.yaml`、`compose.remote-mysql.yaml` 分别重命名为 `compose.local.yaml`、`compose.remote.yaml`，环境模板对应改为 `.env.local.example`、`.env.remote.example`。旧环境文件可继续通过 `--env-file 原文件` 使用，不必覆盖；PyCharm 或部署脚本中的 Compose 文件路径需同步更新。

更新代码后先验证并构建，镜像就绪后立即停止应用再替换，不等待当前下载完成：

```bash
docker compose -f compose.local.yaml config --quiet
docker compose -f compose.local.yaml build app
docker compose -f compose.local.yaml stop -t 0 app
docker compose -f compose.local.yaml up -d --no-build app
docker compose -f compose.local.yaml ps
docker compose -f compose.local.yaml logs --tail=100 app
```

本地 MySQL、Redis 不随应用更新重启。两套编排的 app 均设置立即停止；正常停止整个本地栈时，MySQL 仍保留 60 秒退出时间。

修改 `.env` 后使用 `up -d app` 重建容器环境，单纯 `restart` 不会读取新环境。停止应用使用 `stop -t 0 app`，恢复使用 `up -d app`。未完成任务在旧租约到期后可恢复，已暂停任务保持暂停。健康状态为 `unhealthy` 时需查看日志，Docker 不会仅因健康检查失败自动重启。

`down` 会删除容器与网络、保留命名卷；`down -v` 会删除数据卷，不用于升级。切换本地 / 远程方案不会自动迁移数据或实例身份；需要先备份、迁移数据库和状态，再切换。

旧的按账号多容器编排、固定 IPv6 覆盖文件及创建脚本已移除；这不会删除服务器现有容器、网络或卷。若原实例强制配置 `youtube_source_address: '::'`，重建前确认 Docker 网络具有 IPv6 出口；普通直连使用空的源地址让系统自动选择。

## 备份

备份 MySQL、`.env`（含账号加密密钥）、视频目录、应用配置卷和状态卷。远程库使用服务商或 MySQL 工具备份；本地库也应使用 MySQL 逻辑备份或停库后的卷快照，不直接复制运行中的数据目录。Redis 是可重建缓存，无需备份。

```bash
mkdir -p backup
docker compose -f compose.local.yaml cp app:/data/config ./backup/config
docker compose -f compose.local.yaml cp app:/app/.cache/web ./backup/state
```

这些备份包含账号和登录凭据，应与数据库备份一并保存。跨机器迁移还需迁移视频及数据库中的路径，不能只复制一个空 `.env`；旧 SQLite 的一次性迁移见 [MySQL 连接与迁移](mysql.md#从旧-sqlite-迁移到-mysql)。
