# 单机模拟多实例容量测试

## 范围

`scripts/verify/verify_capacity.py` 使用真实 TaskStore、Fleet、CookiePool、ChannelScheduler、DownloadStartGate 和 Redis 进度缓存，模拟多个实例共用一个 MySQL 和 Redis。下载耗时由计时器代替，不访问 YouTube、不启动浏览器、不下载或合并文件。

每个模拟实例有独立状态目录、实例 ID、任务所有者、配置、Cookie 和 SQL 连接池。协调器在一个 Python 进程的独立线程中运行，并非二十份完整应用；不能用此结果证明二十份应用和数百个 yt-dlp 子进程能同机运行。

默认创建随机命名的隔离 MySQL/Redis 容器及内部网络。只挂载后端源码、工具源码、验证脚本和本次结果目录，不挂载业务配置、Cookie 或视频目录，不连接业务数据库。所有测试服务不发布宿主机端口。

## 执行

需要 Docker，以及本机已有的应用镜像、`mysql:8.4`、`redis:7.4-alpine`。脚本不自动下载镜像。默认应用镜像是 `youtube-downloader-local-app:latest`，可通过 `--image` 指定已安装镜像。

```powershell
.venv\Scripts\python.exe scripts/verify/verify_capacity.py
```

默认先批量放入 10,000 个任务，再依次运行 1、5、10、20 个模拟实例，每级 60 秒，最后在 20 实例下持续运行 600 秒；每级允许已启动任务结算后再切换。实际总时间还包括数据库初始化、数据准备和任务结算。

短时冒烟验证：

```powershell
.venv\Scripts\python.exe scripts/verify/verify_capacity.py --stages 1,2 --cookies 2 --stage-seconds 10 --soak-seconds 0 --task-seconds 3 --queue-depth 50 --shared-interval 0
```

提高模拟准入压力：

```powershell
.venv\Scripts\python.exe scripts/verify/verify_capacity.py --shared-interval 0 --account-interval 30 --start-period .25 --task-seconds 30 --soak-seconds 180
```

这些压力参数只作用于虚构账号，不能当作真实 YouTube 账号的推荐参数。真实账号间隔仍受现有预热和最低间隔算法约束，模拟器不删除这类保护。

默认按每个虚拟节点 16GB 内存计算准入预算，将真实账号容量摘要和计算后的进程预算发布为该节点的本地运行快照；每节点最多模拟 `--slots` 个任务，默认 8 个。测试容器本身仍受真实 Docker CPU、内存限制。这用于模拟分散在多台主机上的协调请求，不会实际创建对应下载进程。

`--modeled-instance-memory-mib 0` 则直接使用测试容器自身的资源判断。1GB 测试容器会把所有虚拟节点都降到约 2 个任务，结果应标注实际并发，不能称为每实例 8 路压测。

`--queue-mode continuous` 在每次尝试领取前新增任务，用于观察持续入队和调度争抢。默认 `prefill` 批量预填队列，避免把逐条入队产生的计数器竞争当作通常的下载调度开销。

## 资源保护与清理

测试资源硬上限：

| 组件 | CPU | 内存 |
| --- | ---: | ---: |
| MySQL | 1 核 | 1536 MiB |
| Redis | 0.25 核 | 128 MiB |
| 模拟协调器 | 1.5 核 | 1024 MiB |

Linux 上每轮约 5 秒采样一次。可用内存低于 2GB 时中止；CPU 执行占比超过 92% 或 I/O PSI `full avg10` 超过 20%，连续三次时中止。测试容器内存超过各自上限的 90% 时中止；另有总运行时限。

可用 `--health-url http://127.0.0.1:8000/health` 同时观察现有服务，连续三次健康检查失败即中止。CPU 指标不包含 I/O wait；I/O PSI 是宿主机观测，不能仅凭其上升就认定由应用或测试引起。

结束或异常时只删除带有本次随机标签的测试容器及其匿名卷、测试网络，不处理业务容器、业务卷或已有目录。强制终止宿主机进程或机器掉电无法保证执行清理，此时先核对 `frameflow.capacity` 标签再处理遗留资源。

## 输出与解释

默认输出到 `.runtime/capacity_<随机值>/`，也可用 `--output` 指定：

- `report.json`：分阶段完成数、操作耗时分位数、SQL 分类耗时、数据库锁等待与连接采样、错误及最终一致性。
- `runner.log`：阶段进度和采样记录。
- `docker-stats.json`：测试容器资源、宿主机压力和可选健康检查耗时。
- `runner-state.json`：容器退出状态和 OOM 标记。
- `abort.json`：触发外层保护时的原因。
- `launch-summary.json`：外层退出、中止原因、报告是否生成和资源清理结果。

校验包括跨实例 Cookie 导入被拒绝、选中 Cookie 属于执行实例、任务不会重复领取或重复结算、账号和实例并发不超限、已结算任务拒绝迟到进度/续租/结算，以及结束时没有运行任务或遗留许可。

`passed` 表示本次负载完成且一致性检查通过，不等于性能达标。必须同时审查 P95/P99、实际活跃任务数、CPU 配额限制和锁等待。`data_lock_waits` 的行数是阻塞关系数，一个等待事务可能对应多行；新版同时记录等待事务去重数。

`claim` 包含本地启动门控快速返回，不能把其整体 P95 当成“成功领取任务”的 P95；`claim_success` 单独记录成功取得任务的调用耗时，仍应结合准入数及数据库锁定查询耗时分析。队列剩余量也不能直接认定调度积压：没有释放的账号启动份额本来就应等待。

该测试不覆盖真实公网延迟、百万级队列、全部前端 HTTP 请求、多进程池连接峰值、视频磁盘吞吐或全天内存泄漏。它适合定位调度和共享数据库的首批瓶颈，不用于承诺二十台服务器的最终吞吐。

首次 VPS 实测及中止边界见 [2026-11-11 容量摸底记录](capacity-probe-20261111.md)。
