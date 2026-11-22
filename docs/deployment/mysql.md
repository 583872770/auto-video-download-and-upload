# MySQL 连接与迁移

项目支持 **MySQL 8.4 或更新版本**，不支持 MariaDB。SQLite 仅保留给本地测试和一次性迁移工具，正式部署统一使用 MySQL。
下载队列、批次、进度、计数、去重记录、登录检测状态和素材索引使用所选数据库，视频仍保存在文件目录中。

## Linux Docker 连接远程 MySQL

使用独立的 `compose.remote.yaml`，它只启动应用，连接远程 MySQL 和 Redis，也不需要 `MYSQL_ROOT_PASSWORD`。
需要随应用一起启动 MySQL、Redis 容器时，使用 `compose.local.yaml`。两种部署的完整步骤和配置说明统一见 [部署手册](docker.md)。

全新部署先复制模板（已有 `.env` 时直接编辑，避免覆盖原配置）：

```bash
cp .env.remote.example .env
```

数据库只从 `.env` 或启动环境读取，不能在 `/setup` 页面修改；所有协作实例在各自环境文件中填写同一个数据库。配置完整后自动启动，页面可测试当前连接、查看启动状态和修改实例设置。旧 `deployment.json` 中的数据库参数会被忽略，新保存的文件只含实例设置。

启动前在 `.env` 中填写远程数据库信息：

```dotenv
MYSQL_HOST=你的数据库IP或域名
MYSQL_PORT=3306
MYSQL_DATABASE=video_downloads
MYSQL_USER=video
MYSQL_PASSWORD='你的数据库密码'
REDIS_URL='redis://:经过URL编码的密码@你的Redis地址:6379/0'
```

`MYSQL_HOST` 仅填写主机名或 IP，不带协议、端口或路径；密码填写原始值，不需要 URL 编码，保留两侧单引号。
数据库要求 MySQL 8.4+，需预先创建库并给应用账号授予下节中的权限；表和索引会在应用启动时自动创建；已有结构不一致时拒绝启动，详见[当前数据合同](../development/current-schema.md)。
数据库需允许应用服务器的出口 IP 连接（MySQL 账号来源限制、云数据库白名单和防火墙端口均需匹配）。

### 给现有数据库补充中文注释

当前代码会为新建 MySQL 表和字段写入中文注释。已有数据库可在应用代码更新后执行一次注释脚本；脚本只执行 `ALTER ... COMMENT`，不删除数据、不改变字段类型、默认值、约束或索引：

```bash
python scripts/apply_schema_comments.py --dry-run  # 先查看将执行的 DDL
python scripts/apply_schema_comments.py             # 写入表注释和字段注释
```

脚本使用当前进程中的 `VIDEO_DATABASE_URL` 或 `MYSQL_*` 环境变量连接数据库。执行完成后，在 DataGrip 刷新数据库对象即可看到表说明和字段说明。

```bash
docker compose -f compose.remote.yaml config --quiet
docker compose -f compose.remote.yaml up -d --build
docker compose -f compose.remote.yaml logs -f --tail=100 app
```

后续查看状态、停止或更新时，也带上所用的 `-f` 文件参数。
默认网页仅监听服务器 `127.0.0.1:5000`；外部访问设置见 [部署手册](docker.md#访问与首次设置)。
远程库的备份使用数据库供应商或 MySQL 客户端工具，不适用 `docker compose exec mysql`。

**切换数据库不会自动迁移已有任务和素材索引。** 如果目标是空库，页面将显示空任务列表；原数据仍在原库。
需要保留历史和队列时，先按本文迁移步骤操作，确认后再切换连接；Windows 转 Linux 还需转换路径并迁移文件。

## 直接连接已有 MySQL

先在 MySQL 中创建专用库和账号。以下密码仅为占位符，需要替换：

```sql
CREATE DATABASE video_downloads CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
CREATE USER 'video'@'%' IDENTIFIED BY '替换为自己的随机密码';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
ON video_downloads.* TO 'video'@'%';
```

应用统一使用 InnoDB 和 utf8mb4_0900_bin 排序规则。

安装依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

连接串格式如下，数据库必须先存在：

```text
mysql+pymysql://video:密码@127.0.0.1:3306/video_downloads?charset=utf8mb4
```

先在项目根目录 `.env` 中配置 MySQL，再直接运行 `app.py`。`http://127.0.0.1:5000/setup` 只提供连接测试、启动状态和实例设置。PyCharm 无需额外参数。

默认读取项目根目录 `.env`，也支持 `--env-file 文件路径`；PyCharm 中将该参数填入“脚本参数”。密码使用原始值，建议用单引号包裹，无需手动 URL 编码。进程环境变量优先于文件；`VIDEO_DATABASE_URL` 优先于 `MYSQL_*`。页面保存的数据库参数不再生效，后台进程沿用启动配置。修改后重启本地服务；Docker 中执行 `up -d` 更新容器环境。不会自动迁移已有数据。

旧的 `database.json` 仍供迁移工具和直接调用数据库接口使用；通过 `app.py` 启动时请在 `.env` 或环境变量中配置 MySQL。未配置时设置页提示补齐启动配置，不会新建业务 SQLite 数据库。不要把连接串写入 `config.yaml` 或提交到 Git。

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

## 首次升级耗时与失败提示

连接测试成功只代表账号可以连接并执行查询。启动会校验当前字段、类型、约束及必要索引，不执行旧结构升级。结构不一致时请备份并安排开发测试库重建；程序不会自动清库。

多实例首次初始化通过数据库命名锁串行创建表。初始化锁繁忙时等待其他实例完成即可，无需修改实例 ID 或密码。

## 复制当前结构的数据库

使用 `scripts/migrate_mysql.py` 从当前完整表结构迁移到空 MySQL 库。此工具复制所有业务表，包括共享配置快照、导入检查点、任务序号、进度、轮换计数和素材索引。
此工具不支持旧表结构转换，源库必须已使用当前结构。

1. 先备份原数据库、配置、Cookie、日志和视频目录，在页面“停止全部下载”，确认运行中任务为零。
2. 停止 Web 和所有后台下载、导入、索引进程。只关闭网页不会停止进程。
3. 准备目标 MySQL 的空数据库，以及 `.cache/mysql/database.json` 目标连接文件。目标应用在迁移完成前保持停止。
4. 保留原 `.cache/web/database.json` 作为源连接文件，运行：

```powershell
.\.venv\Scripts\python.exe scripts/migrate_mysql.py --source-connection-file .cache/web/database.json --target-connection-file .cache/mysql/database.json
```

SQLite 的源文件也以 JSON 指定，例如 `{"url":"sqlite:///D:/项目/.cache/web/tasks.sqlite3"}`，另存到 `.cache/mysql/source.json` 后作为 `--source-connection-file`。
路径始终按运行迁移脚本的环境解释。目标 MySQL 可以是远程实例；迁移脚本运行环境需要能够访问其地址，且应用账号具备建表和写入权限。

迁移默认每批 500 行，每批复制与检查点在同一事务提交。中断后使用相同命令重跑，会从检查点继续；`--chunk-size 100` 可减小单批内存和网络包。
目标库非空且不是同一来源的未完成迁移时，工具会拒绝复制。全表数量核对通过后，才能加 `--activate` 切换：

```powershell
.\.venv\Scripts\python.exe scripts/migrate_mysql.py --source-connection-file .cache/web/database.json --target-connection-file .cache/mysql/database.json --activate
```

`--activate` 先备份原连接文件，再原子更新指定 `--state`（默认 `.cache/web`）的 `database.json`。原数据库保留，下载队列保持暂停。
若先前设置了 `VIDEO_DATABASE_URL`，需将它改成目标 MySQL 或移除该环境变量，避免它覆盖新的连接文件。启动应用、核对任务和素材后，再在页面继续下载。
完成后如需再次核对迁移，应使用保存在 `.cache/backups/mysql-时间戳/database.json` 的原源连接文件；激活后的当前连接文件已经指向目标，不能再当作迁移源。

该工具只负责换数据库，保持路径和文件原样。**Windows 本机转 Linux 容器还需要迁移文件并转换任务快照、导入文件和素材索引中的绝对路径**，不能直接用本机数据库复制结果恢复容器下载。

## 索引与事务

任务历史使用定长完成时间和序号的完整索引；任务领取使用 READ COMMITTED 与 FOR UPDATE SKIP LOCKED，下载在领取事务提交后执行。快照和较大的 JSON 使用 LONGTEXT，租约和统计时间使用双精度。

MySQL 回归使用 `VIDEO_TEST_MYSQL_URL` 指定测试服务器，每例创建独立数据库并在结束后删除。不要指向应用业务库。
