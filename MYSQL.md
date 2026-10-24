# MySQL 连接与迁移

项目支持 **MySQL 5.7.9+（包括 5.7.44）和 MySQL 8.x**，不支持 MariaDB。SQLite 仅保留给本地测试和一次性迁移工具，正式部署统一使用 MySQL。
下载队列、批次、进度、计数、去重记录、登录检测状态和素材索引使用所选数据库，视频仍保存在文件目录中。

## Linux Docker 连接远程 MySQL

使用独立的 `compose.remote-mysql.yaml`，它只启动应用，不创建本地 MySQL，也不需要 `MYSQL_ROOT_PASSWORD`。
这是项目唯一的 Compose 编排文件；它只启动应用，不创建本地 MySQL。

全新部署先复制模板（已有 `.env` 时直接编辑，避免覆盖原配置）：

```bash
cp .env.example .env
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
数据库要求 MySQL 5.7.9+，需预先创建库并给应用账号授予下节中的权限；表和索引会在应用启动时自动创建，自动选择与服务器版本兼容的排序规则及任务锁。
数据库需允许应用服务器的出口 IP 连接（MySQL 账号来源限制、云数据库白名单和防火墙端口均需匹配）。

```bash
docker compose -f compose.remote-mysql.yaml config --quiet
docker compose -f compose.remote-mysql.yaml up -d --build
docker compose -f compose.remote-mysql.yaml logs -f --tail=100 app
```

后续查看状态、停止或更新时，也带上所用的 `-f` 文件参数。
默认网页仅监听服务器 `127.0.0.1:5000`；外部访问设置见 [DOCKER.md](DOCKER.md#局域网或域名访问)。
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

以上建库语句兼容 MySQL 5.7 和 8.x；应用会显式为自己的新表选择排序规则。
MySQL 5.7 使用 InnoDB、DYNAMIC 行格式，需启用 `innodb_large_prefix` 并使用 Barracuda 文件格式（5.7.9+ 默认配置满足），以支持完整的 utf8mb4 索引。
远程账号的来源可以将 `%` 收紧为应用服务器出口 IP；已有数据库和账号时只需确认权限，无需重复创建。

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

## 从旧 SQLite 迁移到 MySQL

使用 `scripts/migrate_mysql.py` 从当前完整表结构迁移到空 MySQL 库。此工具复制所有业务表，包括共享配置快照、导入检查点、任务序号、进度、轮换计数和素材索引。
旧版只有单张任务表的 SQLite 需要先升级到当前表结构。

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

## 实现与验证范围

- 数据表使用 InnoDB；MySQL 8.0.21+ 使用 `utf8mb4_0900_bin`，5.7 和较早的 8.0 使用 `utf8mb4_bin`、DYNAMIC 行格式。YouTube 视频 ID 和去重键区分大小写，支持中文与 Emoji。5.7 的字符串比较会忽略尾部空格，素材目录名不要只靠尾部空格区分。
- MySQL 5.7 的 LONGTEXT 列不设置数据库默认值，由应用在写入时填入；MySQL 8.x 的现有表和默认值保持原样。
- JSON、结果、进度等使用 LONGTEXT，避免 64 KiB 截断；文本索引采用前缀长度，素材根目录最长 512 个字符。
- 租约、重试时间、心跳和素材时间使用 DOUBLE，保留 Unix 时间的小数精度。
- MySQL 使用 READ COMMITTED；8.x 任务通过 `FOR UPDATE SKIP LOCKED` 领取，5.7 自动使用 `FOR UPDATE`，等待其他领取事务提交后重新检查任务状态，避免重复执行。锁仅在领取/更新事务中持有，视频下载在事务外并行；5.7 的调度在锁竞争时会等待，高并发吞吐可能低于 8.x。
- 领取事务遇到 MySQL 死锁错误（1213）时会整体回滚并有限重试，最多尝试 5 次；不会额外消耗下载尝试次数或重复累加队列计数。
- upsert 使用 `ON DUPLICATE KEY UPDATE`；不使用会掩盖数据错误的 `INSERT IGNORE`。

`tests/test_mysql.py` 可在独立的 MySQL 5.7 / 8.x 测试数据库验证并发领取、5.7 行锁等待及状态复查、8.x 锁跳过、文本默认值、批量去重、断点导入、租约、实际下载停止/续传、测试模式清理、Cookie 前置校验、任务视图和跨数据库迁移。
设置专用测试管理员连接 `VIDEO_TEST_MYSQL_URL` 后可运行：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_mysql -v
```

测试会创建并删除随机命名的测试库，未提供测试连接时跳过；测试管理员需要创建和删除数据库的权限。
已在独立的 MySQL 5.7.44 和 8.4.7 实例运行上述集成测试；5.7 验证行锁等待和状态复查，8.4 验证跳过锁。MySQL 的百万、千万规模容量需在目标硬件与实际任务分布下另测。
