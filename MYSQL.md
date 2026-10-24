# MySQL 连接与迁移

项目支持 MySQL 8.0.21+，推荐 **MySQL 8.4 LTS**。Docker Compose 默认使用 MySQL；原 PostgreSQL 和 SQLite 连接继续兼容。
下载队列、批次、进度、计数、去重记录、登录检测状态和素材索引使用所选数据库，视频仍保存在文件目录中。

## Docker 部署

参见 [DOCKER.md](DOCKER.md)。复制 `.env.example` 为 `.env`，填写 `MYSQL_PASSWORD` 和独立的 `MYSQL_ROOT_PASSWORD`，再执行：

```powershell
docker compose config --quiet
docker compose up -d --build
```

应用连接内部地址 `mysql:3306`，数据库由 `MYSQL_DATABASE` 指定，默认 `video_downloads`，应用用户默认 `video`。
数据库健康检查通过后启动应用，表结构自动初始化；默认不对宿主机开放数据库端口。

如需本机数据库客户端访问，使用可选覆盖文件：

```powershell
docker compose -f compose.yaml -f compose.mysql-local.yaml up -d
```

此时数据库地址为 `127.0.0.1:13306`，仅宿主机可访问；`MYSQL_LOCAL_PORT` 可在 `.env` 中调整。
持续使用该端口时，后续 Compose 命令保持带两个 `-f` 参数。

## Linux Docker 连接远程 MySQL

使用独立的 `compose.remote-mysql.yaml`，它只启动应用，不创建本地 MySQL，也不需要 `MYSQL_ROOT_PASSWORD`。
**不要同时加载 `compose.yaml`**，默认文件的数据库地址固定为内部容器 `mysql`。

全新部署先复制模板（已有 `.env` 时直接编辑，避免覆盖原配置）：

```bash
cp .env.remote-mysql.example .env
```

在 `.env` 中填写远程数据库信息：

```dotenv
MYSQL_HOST=你的数据库IP或域名
MYSQL_PORT=3306
MYSQL_DATABASE=video_downloads
MYSQL_USER=video
MYSQL_PASSWORD='你的数据库密码'
```

`MYSQL_HOST` 仅填写主机名或 IP，不带协议、端口或路径；密码填写原始值，不需要 URL 编码，保留两侧单引号。
数据库要求 MySQL 8.0.21+，需预先创建库并给应用账号授予下节中的权限；表和索引会在应用启动时自动创建。
数据库需允许应用服务器的出口 IP 连接（MySQL 账号来源限制、云数据库白名单和防火墙端口均需匹配）。

```bash
docker compose -f compose.remote-mysql.yaml config --quiet
docker compose -f compose.remote-mysql.yaml up -d --build
docker compose -f compose.remote-mysql.yaml logs -f --tail=100 app
```

如需方式二的 Chromium 和远程桌面，启动命令改为：

```bash
docker compose -f compose.remote-mysql.yaml -f compose.yt5s.yaml up -d --build
```

后续查看状态、停止或更新时，也带上所用的 `-f` 文件参数。
默认网页仅监听服务器 `127.0.0.1:5000`；外部访问设置见 [DOCKER.md](DOCKER.md#局域网或域名访问)。
远程库的备份使用数据库供应商或 MySQL 客户端工具，不适用 `docker compose exec mysql`。

**切换数据库不会自动迁移已有任务和素材索引。** 如果目标是空库，页面将显示空任务列表；原数据仍在原库。
需要保留历史和队列时，先按本文迁移步骤操作，确认后再切换连接；Windows 转 Linux 还需转换路径并迁移文件。

## 直接连接已有 MySQL

先在 MySQL 中创建专用库和账号。以下密码仅为占位符，需要替换：

```sql
CREATE DATABASE video_downloads CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
CREATE USER 'video'@'%' IDENTIFIED BY '替换为自己的随机密码';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
ON video_downloads.* TO 'video'@'%';
```

安装依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

连接串格式如下，数据库必须先存在：

```text
mysql+pymysql://video:密码@127.0.0.1:3306/video_downloads?charset=utf8mb4
```

默认状态目录 `.cache/web` 优先读取环境变量 `VIDEO_DATABASE_URL`，随后读取 `.cache/web/database.json` 中的 `url`。两个入口都支持 MySQL。
自定义状态目录从该目录的 `database.json` 读取。不要把连接串写入 `config.yaml` 或提交到 Git。

对于全新部署，可设置环境变量再启动：

```powershell
$env:VIDEO_DATABASE_URL = 'mysql+pymysql://video:经过URL编码的密码@127.0.0.1:3306/video_downloads?charset=utf8mb4'
.\.venv\Scripts\python.exe app.py --port 5000
```

密码中的 `@`、`:`、`/`、`#`、`%` 等字符必须进行 URL 编码。也可使用 SQLAlchemy `URL.create` 组装，以下为 Python 示例：

```python
import json
from pathlib import Path
from sqlalchemy.engine import URL

url = URL.create('mysql+pymysql', username='video', password='填写原始密码',
                 host='127.0.0.1', port=3306, database='video_downloads',
                 query={'charset': 'utf8mb4'})
folder = Path('.cache/mysql')
folder.mkdir(parents=True, exist_ok=True)
(folder / 'database.json').write_text(
    json.dumps({'url': url.render_as_string(hide_password=False)}), encoding='utf-8')
```

这会生成独立的目标连接文件，不会切换现有应用。新项目确认没有原有任务后，可将它复制为 `.cache/web/database.json`；已有数据则先按下面步骤迁移。
数据库账号需要能创建表和索引；应用启动不会自动创建数据库或安装 MySQL 系统服务。

## 迁移现有 PostgreSQL / SQLite 数据

使用 `scripts/migrate_mysql.py` 从当前完整表结构迁移到空 MySQL 库。此工具复制所有业务表，包括共享配置快照、导入检查点、任务序号、进度、轮换计数和素材索引。
旧版只有单张任务表的 SQLite 需要先升级到当前表结构。原 `migrate_database.py` 和 `backup_database.py` 仍为 PostgreSQL 专用历史工具。

1. 先备份原数据库、配置、Cookie、日志和视频目录，在页面“停止全部下载”，确认运行中任务为零。
2. 停止 Web 和所有后台下载、导入、索引进程。只关闭网页不会停止进程。
3. 准备目标 MySQL 的空数据库，以及 `.cache/mysql/database.json` 目标连接文件。目标应用在迁移完成前保持停止。
4. 保留原 `.cache/web/database.json` 作为源连接文件，运行：

```powershell
.\.venv\Scripts\python.exe scripts/migrate_mysql.py --source-connection-file .cache/web/database.json --target-connection-file .cache/mysql/database.json
```

SQLite 的源文件也以 JSON 指定，例如 `{"url":"sqlite:///D:/项目/.cache/web/tasks.sqlite3"}`，另存到 `.cache/mysql/source.json` 后作为 `--source-connection-file`。
路径始终按运行迁移脚本的环境解释。若目标是 Compose MySQL，可以先只启动数据库，临时开放本机端口：

```powershell
docker compose -f compose.yaml -f compose.mysql-local.yaml up -d mysql
```

此时目标连接文件使用 `127.0.0.1:13306`，在宿主机运行迁移；不要先启动会写入目标库的 `app`。

迁移默认每批 500 行，每批复制与检查点在同一事务提交。中断后使用相同命令重跑，会从检查点继续；`--chunk-size 100` 可减小单批内存和网络包。
目标库非空且不是同一来源的未完成迁移时，工具会拒绝复制。全表数量核对通过后，才能加 `--activate` 切换：

```powershell
.\.venv\Scripts\python.exe scripts/migrate_mysql.py --source-connection-file .cache/web/database.json --target-connection-file .cache/mysql/database.json --activate
```

`--activate` 先备份原连接文件，再原子更新指定 `--state`（默认 `.cache/web`）的 `database.json`。原数据库保留，下载队列保持暂停。
若先前设置了 `VIDEO_DATABASE_URL`，需将它改成目标 MySQL 或移除该环境变量，避免它覆盖新的连接文件。启动应用、核对任务和素材后，再在页面继续下载。
完成后如需再次核对迁移，应使用保存在 `.cache/backups/mysql-时间戳/database.json` 的原源连接文件；激活后的当前连接文件已经指向目标，不能再当作迁移源。

该工具只负责换数据库，保持路径和文件原样。**Windows 本机转 Linux 容器还需要迁移文件并转换任务快照、导入文件和素材索引中的绝对路径**，不能直接用本机数据库复制结果恢复容器下载。

## 实现与验证范围

- 数据表使用 InnoDB 和 `utf8mb4_0900_bin`，YouTube 视频 ID、去重键及文件路径区分大小写，支持中文与 Emoji。
- JSON、结果、进度等使用 LONGTEXT，避免 64 KiB 截断；文本索引采用前缀长度，素材根目录最长 512 个字符。
- 租约、重试时间、心跳和素材时间使用 DOUBLE，保留 Unix 时间的小数精度。
- 任务通过事务和 `FOR UPDATE SKIP LOCKED` 领取；MySQL 使用 READ COMMITTED，批量去重按任务 UUID 判断归属。
- upsert 使用 `ON DUPLICATE KEY UPDATE`；不使用会掩盖数据错误的 `INSERT IGNORE`。

`tests/test_mysql.py` 在独立测试数据库验证并发领取、锁跳过、批量去重、断点导入、租约、实际下载停止/续传、测试模式清理、Cookie 前置校验、任务视图和跨数据库迁移。
设置专用测试管理员连接 `VIDEO_TEST_MYSQL_URL` 后可运行：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_mysql -v
```

测试会创建并删除随机命名的测试库，未提供测试连接时跳过；测试管理员需要创建和删除数据库的权限。
原 PostgreSQL 百万级压测结果仅代表当时环境，MySQL 的百万、千万规模容量需在目标硬件与实际任务分布下另测。
