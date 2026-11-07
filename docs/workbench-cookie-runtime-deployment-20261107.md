# Cookie 账号格子发布记录

北京时间 **2026-11-07 14:35:55** 已部署到洛杉矶服务器 `216.227.169.90`，恢复原来的领取状态。

## 内容

工作台从传输名额格子改为按 Cookie 分格。目前所选 19 份 Cookie 显示 19 格，共享传输上限保持 12 路。每格列出该 Cookie 的任务与阶段，显示已观测到的账号间隔、共用启动间隔、并发占满、冷却等原因及倒计时；没有足够采样时显示原因待确认。

更新五个文件：`static/concurrency.js`、`static/concurrency.css`、`webapp/concurrency_diagnostics.py`、`webapp/cookie_pool.py`、`webapp/fleet.py`。账号与启动节奏、冷却及并发策略保持原设置。

## 验证与切换

线上五个源文件与本地 Git 基线按统一换行后核对一致。新镜像在隔离 Linux 容器内通过启动、JavaScript / Python 语法检查和 **100 项测试**，涵盖账号池、并发诊断、实例调度、吞吐及字幕。候选查询在生产 MySQL 上通过只读验证，成功读取 19 个账号名称；旧格式运行快照正确显示未知状态。

暂停领取后，在途任务从 5 个自然结束至零；连续两次确认没有执行中的任务，排空耗时约 44 秒。替换应用后等待健康及业务就绪，核对配置、实例资源限制、任务选择、端口和数据卷一致，再恢复领取。

验收：

- 生产桌面与手机浏览器显示 19 个 Cookie 格子和等待原因；无 JavaScript 错误及横向溢出，验收没有发送写请求。
- 六轮共 30 次健康、配置、工作台、实例、Cookie 接口检查全部 HTTP 200；19 个账号均取得新鲜采样。
- 公网 `http://216.227.169.90:8000/health` 返回 `status=ok`。
- 实例 `paused=0`、`stop_requested=0`、错误为空；观察到新任务正常解析、等待和传输。
- 最终检查已有 5 个切换后完成的成功任务，应用 healthy、重启次数为 0。
- 脚本与样式的在线内容哈希与发布清单一致；原有重复计数和独立账号池保存按钮未恢复。
- MySQL 与 Redis 未重启，之前停止的异常 MySQL 容器保持停止。旧应用容器在验收后移除，保留旧镜像与全部数据卷。

## 版本与回滚

- 镜像：`youtube-downloader-app:cookie-runtime-20261107-r1`。
- 镜像 ID：`sha256:443d32d4cab2752017c0e2d3a8704e3ab08c16b6a0bd353d9e96414b753f1716`。
- 应用容器 ID：`210d8d7441c0267fe94d3796eb7725245d9fe67218113f214f23425d776b05ae`。
- 实例 ID：`9b0f403f331b41268a7272cab0ccda83`。
- 回滚镜像：`youtube-downloader-app:before-cookie-runtime-20261107-r1`。
- 发布目录：`/opt/youtube-downloader/releases/cookie-runtime-20261107-r1`，仅 root 可访问。
- 回滚脚本：该发布目录的 `rollback.py`，排空后恢复上一版应用参数与领取状态。

页面缓存未更新时使用 `Ctrl+F5` 强制刷新。
