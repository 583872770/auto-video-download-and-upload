# 账号池启动检查修复（2026-11-04）

## 原因与行为

实例启动预检查原来只检查 `youtube_cookie` 指向的账号。该账号正在冷却时，即使账号池还有其他可用会话，也会拒绝整个实例启动。

`YouTubeAuth.check_startup()` 现在按去重后的账号池检查未冷却会话，任一登录验证通过即可通过启动检查；指定的默认 Cookie 不会因此被替换。全部会话或整个池正在自动冷却时，启动检查返回等待状态，原调度器继续负责冷却到期后的准入和低频恢复。启动检查不申请下载许可，也不重置账号的冷却、限流次数或恢复节奏。

人工拦截仍拒绝启动。单账号的手动登录检测仍拒绝请求冷却账号。未启用账号池时沿用原有检查。池中 Cookie 文件缺失、无效或过期时仍要求更新。

## 验证

- 本地新增 11 项回归测试通过，覆盖默认账号冷却、全部冷却、全池冷却、人工拦截、登录失败后检查其他账号、HTTP 429 写入冷却、重复凭据去重、检测期间进入冷却，以及单 Cookie 兼容。
- 服务器候选镜像在无网络、无生产数据库环境和挂载的隔离容器中运行 154 项相关测试，153 项通过。
- 唯一失败项为 `tests.test_hot_policy.FleetHotPolicyTests.test_remote_strategy_persists_in_file_registry_and_after_restart`：断言 `throughput_mode` 为 `auto`，实际为 `manual`。用未修改的旧生产镜像单独运行该项，也出现相同失败；本地亦复现。该问题与本次启动检查修复无关。
- Python 编译检查和 `git diff --check` 通过。

隔离回归命令：

```sh
python -m unittest tests.test_pool_startup tests.test_cookie_pool tests.test_youtube_auth tests.test_cookie_profiles tests.test_fleet tests.test_hybrid tests.test_hot_policy tests.test_current_settings tests.test_channel_scheduler -q
```

## 发布文件

服务器发布目录为 `/opt/youtube-downloader/releases/pool-startup-20261104`，包含两份源码、原文件、容器配置备份及部署前后状态快照，目录仅 root 可访问。镜像以原生产镜像为基础，只替换 `webapp/youtube_auth.py` 和 `webapp/fleet_routes.py`。

候选镜像为 `youtube-downloader-local-app:pool-startup-20261104`；旧镜像保留为 `youtube-downloader-local-app:before-pool-startup-20261104`。

首次发布时新容器健康检查通过，但部署保护误将配置记录内实时更新的 `throughput` 采样视为配置变化，已自动回滚。实际下载配置、暂停状态和全部资源冷却状态均一致。部署保护随后改为比较实际保存配置及暂停标记，实时采样不参与相等检查。

2026-11-04 10:27（北京时间）完成发布，运行镜像为 `sha256:b02636b782408687a28966b6e2c4a49c67046437236ea7acc6fe33b1fec178de`，同时更新默认镜像标签。应用容器健康，两份源码字节与发布文件一致；实际保存配置、暂停标记及全部持久化资源状态与发布前一致。旧容器以 `youtube-downloader-local-app-1-before-pool-startup-20261104` 保留且保持停止。

发布后通过实例凭据调用真实 `/api/fleet/preflight`，返回 HTTP 200、`ready: true`，耗时 0.82 秒。没有启动下载队列，实例仍暂停，运行中任务为 0。
