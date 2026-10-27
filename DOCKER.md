# Docker Compose 部署

需要 Docker Engine / Docker Desktop（使用 Linux 容器）和 Docker Compose v2+。
支持 Linux amd64、arm64；构建时 Debian 软件包（包括 FFmpeg 和可选 Chromium）默认使用清华大学 Debian 镜像，Python 依赖默认使用清华 PyPI 镜像。基础镜像仍从 Docker Hub 拉取。Mihomo 固定版本的 GitHub 发布包优先通过 `ghfast.top`、`gh-proxy.com` 下载，失败后尝试 GitHub 原站，最多两轮，每个请求超时 30 秒；每次都验证项目固定的 SHA256，通过后才安装。

主应用支持构建参数 `DEBIAN_MIRROR`（如 `https://mirrors.tuna.tsinghua.edu.cn`，不带 `/debian`）和 `PIP_INDEX_URL`（完整的 `/simple` 地址）。Compose 会把这两个参数从 `.env` 传入；如需临时切换源，也可在构建命令中传入 `--build-arg DEBIAN_MIRROR=... --build-arg PIP_INDEX_URL=...`。

国内服务器建议先使用默认清华源。如果服务器到清华源较慢，可在 `.env` 中改为腾讯云或阿里云：

```dotenv
DEBIAN_MIRROR=https://mirrors.cloud.tencent.com
PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
```

镜像地址不要追加 `/debian`；Dockerfile 会根据 Debian 的 sources 文件自动拼接 Debian 和 security 路径。更换后重新执行 `up -d --build app`，不需要删除数据卷，也不要使用 `--no-cache`。

若 Mihomo 下载仍出现 `RemoteDisconnected` 或超时，可在 `.env` 添加 `MIHOMO_DOWNLOAD_BASE_URL=https://你的下载站/MetaCubeX/mihomo/releases/download`，作为优先来源。脚本会追加 `/v1.19.33/mihomo-linux-<架构>-v1.19.33.gz`；自定义来源也必须提供与固定校验值一致的官方压缩包。修改后重新执行远程 MySQL Compose 的 `up -d --build`，无需删除卷或使用 `--no-cache`。

页面支持切换“方式一：yt-dlp”“方式二：yt5s（实验）”和“方式三：yt5s 优先，失败用 yt-dlp”。唯一的 `compose.remote-mysql.yaml` 已包含 Chromium、虚拟显示和网页远程桌面，直接使用它即可。部署说明见 [YT5S_LINUX.md](YT5S_LINUX.md)。

## 启动

**使用已有远程 MySQL：** 请按 [远程数据库部署步骤](MYSQL.md#linux-docker-连接远程-mysql) 使用唯一的 `compose.remote-mysql.yaml` 和 `.env.remote-mysql.example`。项目不创建本地 MySQL 容器。

在项目根目录执行：

```powershell
cp .env.remote-mysql.example .env
```

推荐在启动后的 `/setup` 页面填写远程 MySQL、服务角色和实例地址，数据库环境变量可以留空。
已有 `.env` 的数据库设置仍兼容；页面保存后优先使用页面配置。若使用环境变量填密码，保留两侧单引号，避免 `$`、`#` 被 Compose 解释。
`VIDEO_DOWNLOAD_DIR` 设置宿主机的视频目录，Windows 可写 `D:/youtube-videos`，Linux 可写 `/srv/youtube-videos`。
含空格的目录用引号包围。默认保存到 Docker 引擎所在 Linux 服务器的 `/srv/youtube-videos`。

这里的“宿主机”是 **Docker 引擎所在的机器**。如果 Windows 上的 PyCharm 连接远程 Linux Docker，`.env` 应填写服务器上的绝对目录，例如 `VIDEO_DOWNLOAD_DIR=/srv/youtube-videos`。不要填写 Windows 路径或保留 `./docker-data/videos`，后者可能被本地 Compose 展开成 `D:\...` 后传给 Linux，报 `invalid volume specification`。目录位于服务器，不会自动映射到 Windows；网页中的保存目录仍填 `/data/videos`。修改后重新运行 Compose 的 `up -d` 即可，无需重新构建镜像。

已有 `.env` 或 PyCharm 运行配置中的 `VIDEO_DOWNLOAD_DIR` 会覆盖默认值。如果仍报 `D:\...` 路径错误，请检查运行配置中的环境变量、指定的 `--env-file`，将其中的视频目录也改为服务器绝对路径。更换挂载目录不会自动搬迁已有视频；已有素材时应填写其实际所在的服务器目录。

```powershell
docker compose -f compose.remote-mysql.yaml config --quiet
docker compose -f compose.remote-mysql.yaml up -d --build
docker compose -f compose.remote-mysql.yaml ps
docker compose -f compose.remote-mysql.yaml logs -f --tail=100 app
```

访问 <http://127.0.0.1:5000/setup>，输入日志中的“部署设置访问码”，测试 MySQL 连接并保存。可选择管理后台、下载实例或单机模式，集群步骤见 [MULTI_INSTANCE.md](MULTI_INSTANCE.md)。如果本机服务已占用 5000，把 `.env` 中的 `WEB_PORT` 改为 `5001`，访问对应端口。
首次构建会安装依赖。未配置时容器正常提供设置页，健康检查也会通过；首次保存时才连接远程数据库创建表并启动业务服务。配置保存到持久化状态卷的 `deployment.json`；以后启动自动读取。运行中修改部署配置，保存后需执行 `docker compose -f compose.remote-mysql.yaml restart app` 生效。

旧配置误存为单机模式、但没有明确的转换请求时，启动仍会恢复原集群角色。需要切回单机时，在 `/setup` 选择“单机模式”，勾选转换确认并保存，然后重启本机服务。保存后本机停止领取新任务，转换在重启时执行。

切换前先暂停下载，等待下载、导入和索引任务结束，停止其他管理端和下载实例并等待 45 秒。转换会再次检查这些条件；条件不满足时显示具体原因，保留转换请求供重试。请使用原来的 MySQL 和状态卷。转换保留任务、统计账单、去重记录和本机素材索引，将队列设为暂停；进入单机工作台后手动开始下载。其他机器上的视频仍保留在原机器，转换不会搬迁远端视频。旧集群身份文件会备份，已转换队列会阻止集群实例重新加入；本功能只提供集群转单机，未提供反向转换。

浏览器桌面启动失败时，技术详情会提示查看容器内 `/tmp/xvfb.log`、`/tmp/x11vnc.log`、`/tmp/novnc.log`。

首次使用在页面上传 YouTube Netscape Cookie 文件，然后检测登录；需要代理时，在“代理与节点”中导入订阅，选择节点并设置自动轮换。
原有 Cookie 校验、批量进度、停止/继续、并行下载和三种保存选项沿用现有功能，其中网盘仍为待开发。

## 服务与持久化

| 服务 | 内容 |
| --- | --- |
| `app` | Flask / Waitress、独立下载和维护子进程、一个共享 Mihomo 内核 |

镜像内已安装 FFmpeg / ffprobe、Node.js 22、锁定版本的 Python 依赖和 Mihomo v1.19.33。
Mihomo 只运行一个内核，各下载槽使用独立的本地监听端口和节点选择组，仍可独立选择、轮换节点。控制端口和全部代理端口仅监听应用容器内部的回环地址，无需增加 Docker 端口映射，所有节点管理继续通过现有页面进行。

从旧的多内核版本升级时，先在页面“停止全部下载”，等待当前任务退出，再使用原来的项目名、`--env-file` 和 Compose 文件组合执行 `up -d --build`，保留已有卷。首次使用代理时会准备各下载槽的监听端口，并停止经进程身份校验确认属于本项目的旧 worker 内核；保留订阅、Cookie、队列和各槽轮换计数。之后调整下载进程数不会重载 Mihomo。
每个实例的并行数量在页面设置为 1–16；每台服务器保留一个 `app` 容器，多台服务器通过共享 MySQL 协作。

| 数据 | 保存位置 |
| --- | --- |
| 配置、上传的 Cookie、命令行下载归档 | `app_config` 命名卷，容器内 `/data/config` |
| 部署配置和访问码、任务日志、会话密钥、订阅、节点和轮换状态、测试下载临时文件 | `app_state` 命名卷，容器内 `/app/.cache/web` |
| 任务、批次、索引、下载去重和轮换计数 | 远程 MySQL 数据库 |
| 视频、封面、字幕、元数据 | `VIDEO_DOWNLOAD_DIR` 指定的宿主机目录，映射到 `/data/videos` |

默认 Compose 项目名为 `youtube-downloader`，配置和状态使用命名卷；任务数据保存在远程 MySQL。
配置模板只在第一次启动时复制，之后在网页保存的设置和 Cookie 不会被覆盖。
页面的指定目录应填写 **容器路径 `/data/videos`**，宿主机路径在 `.env` 中修改；更改目录映射不会搬迁原有文件。
测试模式下载到状态卷内，完成后清理文件。任务记录仍保存在 MySQL。
远程 MySQL 5.7.9+ 和 8.x 均可使用，应用会自动选择兼容的建表和加锁方式，详见 [MYSQL.md](MYSQL.md)。

整个配置目录使用卷挂载，支持页面原子保存配置。需要检查配置时：

```powershell
docker compose -f compose.remote-mysql.yaml cp app:/data/config/config.yaml ./docker-config.yaml
```

配置和状态中可能包含登录信息、订阅及节点凭据，备份时一并妥善保存。

## 浏览器登录

远程 MySQL 编排内置 Chromium、Xvfb 和 noVNC，方式二、三可以直接使用服务器浏览器完成 yt5s 解析；需要人工验证时通过 SSH 隧道访问 6080 端口。
YouTube“独立浏览器登录”仍建议在自己的正常浏览器登录后导出 Netscape Cookie 文件，再在页面上传。Cookie 仍可能被 YouTube 撤销或过期，失效后需要重新上传。

## 局域网或域名访问

默认仅允许宿主机访问。需要从其他设备访问时，修改 `.env`：

```dotenv
WEB_BIND_IP=0.0.0.0
```

运行 `docker compose -f compose.remote-mysql.yaml up -d` 应用端口映射修改。进入 `/setup` 后，“允许访问页面的域名 / IP”会自动补入当前主机，也可手动填写实际域名/IP，保留 `127.0.0.1` 供健康检查使用；已经运行的服务保存后重启生效。宿主机端口、监听 IP 和卷映射由 Docker 决定，不能在容器页面内修改。工作台使用独立的管理账户登录；部署设置访问码仅解锁 `/setup`，管理账户在 `/login` 初始化。HTTPS 部署可设置 `VIDEO_WEB_HTTPS=1`。跨容器的下载实例地址需从管理端实际可达，不能填写另一台机器的 `127.0.0.1`。

## 停止、恢复与更新

```powershell
# 停止整个容器栈，保留数据
docker compose -f compose.remote-mysql.yaml stop

# 继续运行
docker compose -f compose.remote-mysql.yaml up -d

# 更新代码后重新构建应用
docker compose -f compose.remote-mysql.yaml up -d --build app
```

页面“停止全部下载”只暂停任务，Web 和远程数据库继续运行；Compose stop 会结束容器内全部下载和代理进程。
容器重启后，未完成任务会在旧租约过期后恢复；已暂停的队列保持暂停，部分下载文件保留。
健康检查失败会显示 `unhealthy`，Docker 不会仅因这一状态自动重启；应用进程退出时由 `unless-stopped` 策略重启。
`docker compose -f compose.remote-mysql.yaml down` 删除容器和网络但保留命名卷。首次初始化后更改 `.env` 的数据库用户、库名或密码，不会自动修改远程 MySQL 里已有的账号；需同步修改数据库账号。

## 备份

远程 MySQL 使用数据库供应商或 MySQL 客户端完成备份与恢复；Compose 不再启动或管理数据库容器。另行备份 `VIDEO_DOWNLOAD_DIR`、`.env` 以及应用卷中的配置和状态：

```powershell
mkdir backup
docker compose -f compose.remote-mysql.yaml cp app:/data/config ./backup/config
docker compose -f compose.remote-mysql.yaml cp app:/app/.cache/web ./backup/state
```

数据库备份请使用远程 MySQL 服务商或 MySQL 客户端工具。

## 与当前 Windows 部署的关系

这是独立的新部署，不会自动迁移 Windows 本机旧数据库、现有百万级任务、Cookie 或订阅。
构建上下文排除了本机配置、`.cache`、`.tools`、Cookie、视频和 `.env`。
同一主机、相同路径下迁移旧 SQLite 到 MySQL，可用 [MYSQL.md](MYSQL.md) 中的断点迁移工具。迁移到 Linux 容器还需要转换数据库任务中的 Windows 绝对路径，并迁移配置、Cookie、日志和视频；不要直接把旧 `.cache/web` 绑定到新容器。
旧数据库文件不会自动转换或删除；确认迁移和备份前请保留原数据。
此编排每台机器启动一个服务，可在页面配置为管理端或下载实例，共享 MySQL。能否承受具体任务量仍取决于磁盘空间、数据库资源和下载并发。
