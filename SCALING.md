# 大批量下载：第一阶段

这一阶段解决任务和素材元数据增长、批量导入、单机多进程及恢复问题。Web 保留 YouTube 下载、Cookie 文件上传、Clash 节点及轮换功能。现有视频不搬动、不删除。

## 页面使用

1. 首页点击“导入 TXT / CSV 文件”。TXT 每行一个 11 位 YouTube ID 或 HTTP/HTTPS 链接；CSV 支持 `video_id`、`id`、`url`、`link` 列，UTF-8 编码。单文件上限 1 GB，单行上限 16 KB。普通文本框仍最多 100 行。
2. 上传后，独立维护进程每 500 条提交任务、数据库去重结果和文件断点。重复项跳过，无效项统计并保留最多 10 个错误位置；已提交的导入中断后可恢复。导入提交后下载进程可以同时开始下载。
3. 批次内每页 25 个视频、任务每页 50 条、素材每页 24 条。任务和素材支持视频 ID、任务 ID 或标题前缀查询，以页面提示为准。
4. “下载设置 → 下载队列与存储”可调整并行进程、任务尝试次数、磁盘预留、日志上限和日志保留天数。默认 1 个下载进程，最多 16 个；另外有一个导入/索引维护进程。
5. 页面素材查询读取数据库索引。外部复制、移动或删除视频后，点击素材库“同步本地文件”，后台更新索引。正常下载结束会自动入库。

不要把 16 个进程直接当作推荐值。先用 1–2 个观察带宽、磁盘和实际下载结果，再逐步增加。

## 数据与恢复行为

- PostgreSQL 保存任务、共享配置快照、导入断点、下载去重记录、批次计数和素材索引。页面刷新不全量读取任务，也不扫描视频目录。
- 下载队列使用事务领取和 `FOR UPDATE SKIP LOCKED`。租约默认 60 秒、每 10 秒心跳，调度器定期恢复过期任务；数据库拒绝旧执行者继续更新任务状态。系统属于可重试执行，不能保证所有外部操作恰好执行一次。
- 默认最多尝试 3 次，临时超时、连接重置和部分 HTTP 429/5xx 错误按 5–300 秒退避；登录失效、404 等直接失败。重启恢复也受尝试次数约束。终态失败需要修复原因后重新提交。
- Web 下载去重使用数据库；启动任务时增量导入旧 `videos.txt`。CLI 继续使用文本下载档案，两个入口不承诺实时双向同步。
- 新下载默认存放在 `视频目录/视频ID前2位/视频ID/标题 [视频ID].扩展名`，已有目录保持不动。视频 ID 的首两位分散目录项，标题限制字节长度。
- Clash 每个下载进程使用独立内核、代理端口及节点选择，节点池相同、轮换次数独立；“每 N 个视频轮换”按各进程计算。主进程计数兼容旧记录。缩容在任务之间生效，子代理可继续保留供复用，停止 Clash 会关闭项目拥有的子内核。
- 磁盘预留默认 2 GB，在领取任务后、开始下载前检查，空间不足时退回队列并暂停，释放空间后自动恢复。该阈值不是单视频空间预估，超大视频仍可能耗尽磁盘。
- 日志按任务 ID 分目录。结束后默认保留末尾 10 MB；历史日志默认永久保留，配置保留天数后分批清理。不会清理视频或任务历史。
- 导入源文件保存在 `.cache/web/imports/`，目前不自动删除。确认导入任务已成功后可定期归档/清理对应源文件；运行中、排队中和等待恢复的导入文件必须保留。

## PostgreSQL 与迁移

Windows 可安装项目内 PostgreSQL 17.6，不注册系统服务、不修改系统 PATH：

```powershell
.\.venv\Scripts\python.exe scripts/setup_database.py
# 先停止 Web，确认现有下载进程已经完成，再迁移并激活：
.\.venv\Scripts\python.exe scripts/migrate_database.py --activate
.\run.ps1 -Mode web
```

内核位于 `.tools/postgresql/pgsql/`，数据库目录 `.cache/postgres/data/`，默认只监听 `127.0.0.1:15432`。连接及随机密码保存在 `.cache/postgres/` 的私有 JSON 文件中，不要提交或分享。已激活的项目数据库会在 `app.py` 启动时尝试启动；首次启动不会自动下载安装。

迁移先通过 SQLite 在线备份生成 `.cache/backups/时间戳/tasks.sqlite3`，复制配置，然后保留任务 ID、批次、轮换计数及旧日志引用，导入下载档案并重建素材索引。数量核对通过才原子写入 `.cache/web/database.json`。原 SQLite 和视频保留。迁移可重跑；应在目标开始接收新任务前完成迁移核对。

其他系统/外部 PostgreSQL 使用 `VIDEO_DATABASE_URL`（优先于默认状态目录的配置），或在指定状态目录的 `database.json` 保存 `{"url":"postgresql+psycopg://..."}`。大批量部署使用 PostgreSQL；未配置时保留 SQLite 便携模式。数据库升级采用本阶段的一次性旧 SQLite 适配，未来 schema 变更应另加版本化迁移。

## 备份、恢复与回滚

```powershell
# 生成一致性数据库快照，并备份当前配置、Cookie、节点和代理设置：
.\.venv\Scripts\python.exe scripts/backup_database.py

# 日常启动：
.\run.ps1 -Mode web
# 计划停库前，先停 Web 并等待下载进程全部结束：
.\.tools\postgresql\pgsql\bin\pg_ctl.exe -D .cache/postgres/data -m fast -w stop
```

备份默认保存在 `.cache/backups/时间戳/`，`database.dump` 为 PostgreSQL custom 格式。脚本通过进程环境传递密码，不放到命令行。备份包含凭据，应放在受控位置，定期复制到其他磁盘/机器；Windows 任务计划程序可每天调用备份脚本，并自行设置备份轮换。

**数据库备份不包含视频和任务日志**。还需要同步备份视频目录、`.cache/web/logs/` 和旧根目录的 `*.log`。备份期间尽量停止修改配置；数据库快照和文件备份并非一个跨文件事务。

恢复先创建空 PostgreSQL 数据库，并把目标连接写入独立 JSON 文件（不要覆盖正在使用的连接文件），然后：

```powershell
.\.venv\Scripts\python.exe scripts/backup_database.py --restore .cache/backups/时间戳/database.dump --connection-file restore-connection.json
```

恢复工具拒绝覆盖非空数据库，并在一个事务内恢复。验证任务数和素材文件后，停止应用及下载进程，再将目标连接复制为 `.cache/web/database.json`，按原路径恢复配置、Cookie、Clash 设置、视频及日志，启动并验证。旧数据库保留，确认新库可用后再安排清理。

仅在迁移后尚无新写入时，才可停服务后移走 `.cache/web/database.json`，回到原 SQLite。迁移后有新任务、新下载或设置修改时，不能直接回旧库，否则会丢失这部分记录；应从最新备份恢复 PostgreSQL 或另做增量数据回迁。

## 验证范围和下一阶段

`scripts/benchmark_scale.py --rows 1000000` 在独立 PostgreSQL 库验证百万任务流式导入、百万素材元数据及分页查询，报告存入 `.cache/validation/bench_video_*/report.json`，测试库结束后删除。它不会下载 YouTube 视频，也不创建百万个实际媒体文件。

2026-10-23 本机 Windows / PostgreSQL 17 实测：100 万条任务流式导入 344.493 秒，约 2903 条/秒；100 万条素材元数据入库 461.857 秒，Python 进程峰值 RSS 86.6 MB（不含 PostgreSQL）。首次完整测试的任务页、素材页、概览接口 P95 分别为 181、270、307 ms；这是 Flask 测试客户端下各 20 次串行请求，并发服务容量需另测。

另用 `scripts/benchmark_queries.py` 在各 100 万条合成记录下补测带实际过滤条件的查询计划及搜索，任务页、素材页、概览 P95 分别约 181、208、295 ms，特定视频 ID / 标题前缀搜索 P95 约 178–222 ms。确认分页走时间/序号索引、精确目标的搜索走索引；过宽的标题前缀会匹配大量数据，不代表所有搜索都具有同样延迟。该补测通过 SQL 生成测试数据，不用于衡量流式导入速度。

本机报告：`.cache/validation/bench_video_b27c5a1937b7/report.json` 和 `.cache/validation/query-scale-52c59060ad/report.json`。首轮完整测试报告中的素材 EXPLAIN 取了缺少根目录过滤的辅助 SQL；补测捕获真实 API 的 SQL，已确认 `ix_media_root_recent` 索引生效。所有性能数据仅适用于本机和这些测试分布。

`scripts/verify_scale.py` 用真实 PostgreSQL、浏览器和两个下载子进程验证 CSV 上传、去重/错误统计、本地 HTTP 文件下载、分页、搜索和移动布局。`tests/test_scale.py` 覆盖断点恢复、并发领取、租约失效和有限重试；`tests/test_rotation.py` 通过真实 Mihomo 验证进程间代理隔离；`tests/test_migration.py` 验证真实数据库迁移、重复执行和备份恢复。

这一阶段是单机基础。千万条记录需用目标硬件和真实任务分布另行压测；百万、千万个实际视频还需要 NAS/对象存储、容量预算、跨机器下载调度、监控告警，以及索引和历史数据维护。比如平均每个视频 100 MB，100 万个约 100 TB、1000 万个约 1 PB，尚未计入封面、字幕、日志、副本和预留空间。当前不承诺单机长期承载这一文件规模。
