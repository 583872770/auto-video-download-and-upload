# YouTube 登录预检超时导致实例暂停

## 服务器日志核对

2026-10-30 对生产服务器进行了只读日志检查，未修改线上代码、配置、代理或队列，未重启服务。

北京时间 16:19:07、16:24:16、16:24:27 和 16:27:46，Mihomo 记录下载进程经 `🇺🇸美国01-0.1倍 | 高速专线-hy2` 访问 `www.youtube.com:443` 时出现 `context deadline exceeded`。紧接着，任务日志报告：

```text
登录检查未通过，队列已停止，任务保留：无法连接 YouTube 验证登录……
reason="authentication_required", error_type="YouTubeAuthRequired"
```

例如 16:24:27.313 的代理超时对应 16:24:27.416 的登录暂停；16:27:46.719 的代理超时对应 16:27:47.256 的登录暂停。

同一实例的日志也记录了其他节点正常完成 yt-dlp 下载，例如 16:16:36 的美国圣何塞节点、16:19:24 的日本节点。因此，这些失败说明当次服务端连接超时，不能据此认定 Cookie 失效，也不能认定每个节点始终不可用。未确认同一失败任务随后成功。

## 原因及修正

登录预检原先对所有未通过的状态抛出同一种异常，执行器统一将其当成确定的登录失败。单机暂停队列，多实例模式隔离当前执行实例；网络恢复后也需手动恢复实例。

现在执行器区分两种结果：

- `network_error`、`unconfirmed`、`checking`：尚不能判断登录是否有效，任务保留并延迟 30 秒重试，不消耗下载尝试次数，不暂停实例或改变业务任务运行意愿。仍需预检通过才会调用下载器。
- 缺少或无效 Cookie、服务端明确未登录、要求额外验证：保留原有暂停或实例隔离行为。此修复不会绕过登录检查或站点验证。

使用项目代理时，网络预检失败也计入该节点的 yt-dlp 线路健康状态。120 秒内连续三次失败会进入现有冷却机制；开启自动轮换且存在健康候选节点时，可选用其他节点。固定节点模式仍使用所选节点。

预检错误信息进一步区分连接超时、TLS / 证书错误和 HTTP 状态。诊断事件只记录错误类别、HTTP 状态及是否配置代理，不记录原始异常文本、Cookie 或代理凭据。

手动登录检测和显式启动前的预检仍要求验证成功；自动重试针对已领取任务执行期间的临时预检失败。服务器浏览器或其他设备能打开 YouTube，不保证每次后台进程通过同一出口访问登录检测页面都成功。

## 验证

定向回归覆盖网络错误后的任务延迟与自动恢复、下载尝试次数、代理线路冷却、超时及证书错误的安全诊断，以及明确登录失败时仍停止领取。多实例回归使用共享数据库验证业务任务运行意愿不变、失败实例不被暂停、延迟期间其他实例不能提前领取、到期后其他实例可以接手完成；同时验证明确登录失败只隔离当前实例。

运行命令：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_youtube_auth tests.test_fallback tests.test_throughput tests.test_hybrid.HybridTests.test_network_preflight_retains_job_intent_and_executor_and_can_move_to_peer tests.test_hybrid.HybridTests.test_confirmed_login_failure_still_isolates_only_the_failing_executor tests.test_rotation
```

结果：49 项测试中 48 项通过、1 项跳过（未配置独立 MySQL 测试库）。包括真实 Mihomo 代理轮换与并行进程回归；新增共享队列回归使用隔离的 SQLite 数据库。Python 语法与 Git 空白检查通过。测试输出位于本地忽略目录 `.cache/youtube-auth-recovery-tests.log`。

本地验证完成时尚未部署。用户部署后的线上核验结果如下。

## 部署后核验

用户通知部署完成后，再次只读检查生产环境。容器于北京时间 2026-10-30 16:41:38 启动，经过旧实例心跳到期等待后，日志确认初始化完成、服务就绪。线上 `web_worker.py` 和 `webapp/youtube_auth.py` 的 SHA-256 与本地已验证文件逐一一致。

16:43:00.027 和 16:43:12.790 的任务事件均为 `reason=youtube_auth_check_unavailable`、`auth_state=network_error`、`retry_after=30`；对应预检诊断的底层错误为 `SSLEOFError`，即 TLS 连接意外结束，不能据此认定证书或 Cookie 无效。后续其他任务继续领取，说明这两次失败未再隔离整个实例。尚未确认这两个任务随后下载成功。

同次核验也发现部署后的多个任务出现 `disk_pressure`，具体消息为“本机临时文件分类预算不足”。当时所在文件系统约 113 GB，已用 78 GB、可用 31 GB；这说明还存在项目内部分类预算造成的资源等待，不能将其混同为登录失败或物理磁盘已满。本次仅检查日志和文件，没有修改预算、清理文件、切换代理或操作队列。
