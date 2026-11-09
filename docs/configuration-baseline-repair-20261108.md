# 第二批测试基线修复与验证

开始日期：2026-11-08，验证跨至 2026-11-09。承接 [第一批配置整理](configuration-refactor-validation-20261108.md)。

## 范围与业务规则

本批先建立可重复的测试基线，为后续并发入口整理做准备，不修改并发算法。

- `youtube_cookie_usage=auto`：公开内容先匿名下载，只有收到确实需要登录的响应后才尝试 Cookie。
- `always`：使用 Cookie，执行前检查登录；第三方渠道降级到 yt-dlp 前重新检查。
- `never`：匿名下载，不因渠道降级而强制检查登录。
- Cookie 池保留自己的准入和检查逻辑；Cookie 跨实例绑定、租约和旧执行者隔离继续生效。
- 下载会话跟随实例启动与停止，队列暂时没有任务不结束会话。不同客户需要独立会话时，测试必须显式停止后再启动。
- Cookie 容量和传输额度不等于本机可启动的工作进程数。内存、CPU、IO 压力仍可降低工作进程数。

## 实际代码修复

`web_worker.py` 修复两处条件：

1. 只有所选渠道包含 yt-dlp 时，才恢复和校验选中的 YouTube Cookie。
   修复仅使用第三方渠道，却因为无关 Cookie 路径不存在或不可用而失败的问题。
   包含 yt-dlp 的执行路径仍进行 Cookie 归属校验，未放宽跨实例绑定。
2. 第三方渠道降级到 yt-dlp 前，仅在 `always` 或已选中池内 Cookie 时重新检查登录。
   修复 `auto`、`never` 被无条件登录检查阻断的问题。
   `auto` 收到需要登录的响应后仍可在同一出口尝试所选 Cookie。

完整 MySQL 验证还发现并修复了数据库迁移脚本的实际缺陷：

3. `scripts/migrate_mysql.py` 原先假定空目标库只有一条默认设置。
   当前数据库初始化会写入五条默认设置和结构升级记录，因此全新目标库也会被误判为非空。
   现在只将已知初始化元数据识别为允许的空库内容。
   `webapp/database.py` 将五项初始化设置集中到 `BOOTSTRAP_SETTINGS`，初始化和迁移校验共用定义。
   自定义设置、修改过的固定默认值、无效时间戳、未知结构版本和任意业务表记录仍阻止覆盖。
   保留源库停止检查、中断续传、全表数量核对和核对后才激活连接的流程。

4. `app.py` 的多实例总览将 MySQL `SUM` 的 Decimal 结果直接交给 JSON 序列化，
   素材数量因而成为字符串，与 SQLite 和单实例接口的数字类型不一致。
   在接口输出边界转换为整数，不改变数量和素材目录选择规则。
   空素材、已有素材、切换目录三种状态均继续检查。

`tests/test_cookie_fallback.py` 增加第三方独立执行及四种降级场景的回归：
按需匿名开始、按需收到登录要求后使用 Cookie、永不登录检查、始终使用时复查登录。
现有跨实例 Cookie 绑定冲突测试继续验证在网络和下载调用前阻断执行。
`tests/test_mysql_migration.py` 新增六项空库识别与数据保护检查；原有 MySQL 迁移场景继续覆盖复制、断点和媒体/进度/日志保留。

## 失效测试夹具修正

| 分类 | 根因 | 调整及保留的约束 |
| --- | --- | --- |
| 认证与降级 | 默认已是按需模式，但旧测试仍预期无条件登录检查 | 认证场景显式选择 `always`，同时检查默认匿名行为及同一代理节点 |
| 渠道优先级 | 一个排列用例产生的真实 503 冷却影响了后续独立排列 | 各排列创建独立夹具，不关闭生产冷却保护；保留渠道顺序和独立统计断言 |
| 会话与计费 | 旧测试在队列耗尽时预期会话完成；不同客户复用了同一会话 | 检查空闲实例会话继续存在，显式停止后结算；保留字节数、计费及幂等断言 |
| 资源压力 | 把 Cookie 池传输容量当成实际进程数 | 分别检查容量、进程数和资源压力提示，不移除压力控制 |
| MySQL 执行模式 | 单机队列契约在创建应用后才清除 `store.fleet`，应用闭包仍保留已注册实例 | 新增 `StandaloneMySQLFixture`，在初始化扩展时选择不注册；应用和 TaskStore 模式一致 |
| MySQL 认证夹具 | 多继承顺序跳过了 `AuthContract.setUp`，认证用例实际上仍处于默认按需模式 | 调整继承顺序，先创建数据库夹具再执行认证契约的 `always` 设置 |
| MySQL 测试资源 | 全量运行时各临时数据库的空闲连接池一直存活 | 每个夹具结束时关闭自己的数据库连接池，避免用例之间累积连接；生产连接池策略不变 |
| 本地代理轮换 | 本地 HTTP 夹具误用 YouTube 默认休眠，多文件下载超过子进程超时 | 仅该夹具关闭 YouTube 休眠，保留真实双进程和节点轮换验证 |
| 代理错误夹具 | 空 HTTP 204 并不是稳定的永久失败条件 | 使用确定的 404，继续检查失败状态与选中节点 |
| 本地订阅 | 可信回环测试服务器触发生产订阅地址限制 | 仅测试补丁允许夹具地址；生产地址检查不变 |

实例注册、调度和跨实例绑定测试保留原有注册路径，不因引入单机夹具而禁用。
Cookie 池契约保留原有夹具模式和准入规则。

## 真实 Redis 契约与 CI

新增 `tests/test_progress_cache_integration.py`，使用真实 Redis，同时覆盖 SQLite 与 MySQL：

1. 独立 TaskStore 能读取缓存中的实时进度，任务完成时最新进度持久化到 SQL。
2. 真实连接失败后进度落回 SQL，恢复后的旧缓存不能覆盖更新的数据库进度。
3. 任务释放、换执行者后，即使释放归还了重试次数，旧缓存和旧进程也不能覆盖新执行；新执行者可正常更新和完成。

测试必须显式设置 `VIDEO_TEST_REDIS_URL`，不以偶然存在的业务 `REDIS_URL` 为测试授权。
每个测试独立创建数据库，只删除自己的任务缓存键，不清空 Redis。

CI 增加独立 Redis 7.4 服务与健康检查，保留 MySQL 8.4；显式传入测试专用 URL。
默认测试不向所有测试强制启用共享缓存，仅真实 Redis 契约明确使用它。

## 验证记录

本机使用 Python 3.14、Node、MySQL 8.4.7、MySQL 5.7.44、真实 Redis、Chromium、Mihomo、ffmpeg。
CI 的 Python 为 3.13，本轮未取得远端 CI 运行结果。
MySQL 与 Redis 均为本轮创建的独立回环服务，随机端口和测试密码；不复用业务数据库。
测试中出现的断连、404、503、限流和登录要求等日志，有一部分是主动注入的故障，不能仅凭日志字样认定测试失败。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| 冻结代码后的完整回归（MySQL 8.4 与真实 Redis 服务可用） | 运行 1163 项：1159 项通过、4 项跳过，0 失败、0 错误；2180.701 秒，退出码 0 | `full-final-mysql84.log` |
| Cookie 使用、降级及绑定隔离 | 18 项通过 | `cookie-final.log` |
| MySQL 5.7 兼容与真实 Redis | 27 项通过 | `mysql57-final-clean.log` |
| 共享 MySQL 8.4 与 Redis 同时启用 | 23 项通过 | `shared-redis-mysql84.log` |
| MySQL 认证契约修正后复测 | 9 项通过 | `mysql-auth-final.log` |
| MySQL 单机业务与计费契约 | 10 项通过 | `mysql-redesign-final.log` |
| MySQL 8.4 迁移与目标库保护 | 9 项通过 | `mysql-migration-final.log` |
| MySQL 5.7 迁移与目标库保护 | 9 项通过 | `mysql57-migration-final.log` |
| MySQL 5.7 认证与业务契约 | 19 项通过 | `mysql57-auth-business-final.log` |
| SQLite/MySQL 8.4 总览数量契约 | 2 项通过 | `mysql-overview-final.log` |
| MySQL 5.7 总览数量契约 | 1 项通过 | `mysql57-overview-final.log` |
| MySQL 5.7 阻塞领取与锁隔离 | 2 项通过 | `mysql57-locking-final.log` |
| 原失败的真实代理、浏览器及媒体传输场景 | 5 项通过，66.301 秒 | `native.log` |
| 修正夹具后的集中复测 | 12 项通过 | `followup.log` |
| 最终代码的 Cookie 状态、实例互相管理、任务恢复 UI | 3 条命令通过，无页面错误 | `ui-final-cookie.log`、`ui-final-instances.log`、`ui-final-resume.log` |
| 第一批配置契约 | 10 项再次通过 | 命令返回 0 |
| Node 状态及轮询回归、JavaScript 语法 | 通过 | `node tests/test_polling.cjs`、`node --check static/app.js` |
| 源码语法与 CI 结构 | 39 个 Python 文件 AST 通过，CI 服务与专用连接变量通过 | 完整回归后核对了 43 个变更源码/契约文件的哈希，均与冻结时一致 |

各行覆盖存在重叠，数量不应相加当作独立用例总数。
代理、浏览器和媒体传输运行真实进程，但上游内容使用本地 HTTP 夹具；这些检查不等于生产 YouTube 出口可用性验证。

首次启用 MySQL 8.4 与真实 Redis 的完整运行执行了 1156 项，10 项断言失败、4 项错误、4 项跳过，
耗时 2332.901 秒。其中两项失败来自新增 Redis 测试错误地假定释放任务仍消耗重试次数；
其余暴露了 MySQL 认证/单机夹具、迁移脚本和总览计数问题。上述失败均已有修正后的单项或整组通过记录。
该运行在修复期间已加载旧代码，不能与修复后的结果混用；最终完整回归已在冻结代码后重新运行。

最终完整回归于 2026-11-09 完成，运行 1163 项，1159 项通过、4 项跳过，0 失败、0 错误，
耗时 2180.701 秒（约 36 分 21 秒），测试命令和隔离服务运行器均返回 0。
这轮同时提供独立 MySQL 8.4 与真实 Redis；真实 Redis 契约显式使用它，其他测试按各自配置决定是否启用缓存。
所有指定隔离测试同时启用共享缓存的结果另见 `shared-redis-mysql84.log`。

4 项跳过的原因如下：

- `test_disk_budget.DiskBudgetTests.test_symlink_is_not_a_cleanup_target`：Windows 当前进程没有创建符号链接的权限。
- `test_disk_budget.DiskWebTests.test_linux_chromium_socket_and_fifo_do_not_break_disk_api_or_admission`：需要 POSIX 文件系统 socket 和 FIFO，本机 Windows 不支持该夹具。
- `test_mysql.MySQLScaleTests.test_waiting_claim_rechecks_status_after_first_task_is_claimed`：该分支仅适用于 MySQL 5.7，在本轮 MySQL 8.4 中跳过；已在独立 MySQL 5.7 阻塞领取复测中通过。
- `test_storage.WorkspaceTests.test_invalid_task_id_and_symlink_cannot_select_a_deletion_target`：Windows 当前进程没有创建符号链接的权限。

没有为消除这些跳过而弱化权限检查或模拟平台能力；远端 Linux CI 结果仍需独立确认。
早先基线中出现过一次 Chromium 临时字典目录清理的 PermissionError，本批真实进程场景和完整回归未复现，
本批没有针对未复现问题增加猜测性修复，不能据此认定该偶发问题已彻底消除。

全量入口为 `python -m unittest discover -s tests -v`。
MySQL 用例需要测试管理员连接 `VIDEO_TEST_MYSQL_URL`，每个测试创建并删除独立数据库；
真实 Redis 用例还需要 `VIDEO_TEST_REDIS_URL`。
共享缓存复测额外将 `REDIS_URL` 指向同一个测试 Redis，只运行指定的隔离测试。
这些变量均不得指向业务服务。CI 中对应的隔离服务定义见 `.github/workflows/tests.yml`。

日志位于忽略的 `.cache/baseline-repair/`。本地临时服务的退出脚本同时处理 Windows MySQL
监控进程及其实际子进程，使用 SQL `SHUTDOWN` 并清理本轮拥有的进程。
最终运行器已退出，MySQL 日志记录正常关闭；按最终隔离目录核对，没有遗留对应的 MySQL 或 Redis 进程。
已结束的临时 MySQL 数据目录自动删除被执行安全检查拒绝，返回信息仅为 `blocked by policy`。
未执行删除，目录与日志保留；这不影响测试进程的正常关闭，也不涉及业务数据目录。

## 边界与下一步

未读取或修改业务 `.env`、用户 Cookie、生产数据库、用户媒体；未部署、未提交 Git。
已存在的第一批配置重构和前端修复保留。

本批的回归测试不能替代 20 实例、每实例至少 20 个 Cookie 的长时间运行和故障压测。
下一阶段先补并发决策契约，再逐步统一实例上限、Cookie 容量、准备与传输阶段、冷却和启动间隔的决策入口。
通过后再拆分后端职责与前端状态，避免把结构调整和业务规则变化混在同一批。
