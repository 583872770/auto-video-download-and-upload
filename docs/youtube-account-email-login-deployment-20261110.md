# 邮件验证码登录部署记录

- 服务器：洛杉矶 `216.227.169.90`。
- 切换时间：2026-11-10 22:10:17 +08:00。
- 镜像：`youtube-downloader-app:account-email-login-20261110-r1`。
- 镜像 ID：`sha256:2aeb27646e0edb630cca2eacd635724e32696ed23bbb389bafa76b42776fbd4c`。
- 应用容器 ID：`f6c0300d2892a28865a715019a6f2b9916fdede6f4133617f4906c493553606b`。
- 发布目录：`/opt/youtube-downloader/releases/account-email-login-20261110-r1`，权限 `700`。

## 上线内容

账号资料的“登录”默认自动填写邮箱、密码，并在 Google 提供兼容的邮件验证页面时读取新邮件验证码。接码使用资料中保存的 `fbvjdjcf.asia` 地址，HTTP 自动升级为 HTTPS；独立请求会话只向网站提交当前邮箱，不发送 Google 密码、附加信息或 Google Cookie。

Google 登录开始前刷新收件箱，记录旧码。需要邮件验证时每 30 秒检查一次，最多等待 3 分钟；仅提交新出现、来源可确认且唯一的六位 Google 验证码，每次登录最多提交一次。接码不可用、超时、误码或验证方式不兼容时交由用户手动继续。第三段附加信息不用于生成动态码；短信、图片验证码和手机确认不自动处理。成功后仍需核对邮箱，自动新增并关联 Cookie，新 Cookie 不自动入池。

本次从原生产镜像覆盖四个运行文件：`scripts/login_youtube.py`、`webapp/account_login.py`、`webapp/email_codes.py`、`templates/accounts.html`。上线哈希匹配，无数据库结构或依赖变更。

## 验证与切换

服务器候选镜像在无外网、单 CPU、1 GiB 内存的隔离容器中通过 45 项相关单测，以及两组真实 Chromium 模拟验证：邮箱与密码填写兼容性，及邮件码、身份核对、Cookie 导出完整流程。验证覆盖旧码排除、仅提交邮箱、取消关闭浏览器与邮箱会话，未执行真实 Google 账号登录。

暂停领取新任务，等待原 11 项任务自然结束。约 348 秒观测到无运行任务，约 359 秒第二次确认排空后替换应用。最后一项约 2.54 GB 的任务完成后才切换。新容器通过 Docker 与业务健康检查，核对实例身份、账号与 Cookie 数据、下载配置、端口、挂载和文件哈希后恢复原未暂停状态。

上线检查时容器已启动约 72 秒，健康且重启为 0；账号 21 条、Cookie 20 份、关联账号和 Cookie 各 18 个，加密可用。登录页面包含新邮件接码说明，账号接口、登录状态、静态资源正常；过期版本登录请求返回 409，未打开真实账号浏览器。实例无错误或停止请求，正在执行 7 项任务，其中传输 1 项、准备 6 项。两次启动间隔 50 秒、结束后休息 25 秒、账号池上限 20 保留。

健康 MySQL、Redis 容器及其 ID 保持不变，异常 MySQL 仍停止。MySQL 当前行锁等待为 0；该上线窗口日志未发现 Traceback、OperationalError、死锁、锁等待超时或内存不足记录。

实际接码网站此前已通过只提交邮箱的连接、邮箱匹配和刷新检查，当时收件箱没有验证码邮件。真实 Google 邮件投递与后续登录放行尚未验证，不能将模拟流程通过视为真实账号已自动登录成功。

## 备份与回退

发布目录保存完整数据库表结构，以及 `console_users`、`download_instances`、`schema_settings`、`youtube_account_audit`、`youtube_account_cookies`、`youtube_account_owners`、`youtube_accounts`、`youtube_cookie_bindings`、`youtube_cookie_profiles`、`youtube_profile_auth` 十张元数据表的备份，未全量备份任务和媒体数据。

复用并核对原账号加密密钥，原文件 `/opt/youtube-downloader/secrets/account-encryption.env` 权限为 `600`，发布目录另存受限备份。后续重建仍须注入相同密钥，不得重新生成替代。本地 `.env` 未修改。

旧应用保留为停止状态的 `youtube-downloader-local-app-1-before-account-email-login-20261110-r1`，原镜像回退标记为 `youtube-downloader-app:before-account-email-login-20261110-r1`。回退应先排空任务，保留当前账号数据与原密钥。

本地发布与脱敏核验结果位于 `.cache/account-email-login-deploy/`，服务器发布目录保存切换记录、备份和最终核验结果。使用方式见 [统一账号管理](youtube-account-management.md)。

## 验证方式选择与输入页兼容修复（22:37:48）

用户反馈仍需手动接码。核实先前两次登录虽最终保存 Cookie，但验证码由用户手动填写；旧状态文件只保留最后一条状态，不能据此认定自动接码成功。只读检查确认账号已保存受支持的接码网址，服务器能访问收件箱、核对当前邮箱，并从实际邮件页面识别 Google 验证码。

用户保留的实际 Google 页面位于 `/v3/signin/challenge/selection`，显示两个发送到同一当前账号邮箱的“Get a verification code at …”选项。此前代码不会选择邮件方式。用户点击第一项后确认下一页为 `/challenge/ipe/verify`，此前正则仅识别 `/challenge/ipe`，因而又交由手动处理。两处均为页面兼容遗漏。

修复在受信任 Google 登录来源上自动选择明确显示当前账号完整邮箱的邮件验证码选项；重复选项仅点击第一项一次，无法明确确认目标的选项交由用户处理。同时兼容 `ipe` 和 `ipe/verify`，后续仍使用原有新邮件筛选、单次提交、30 秒检查及 3 分钟等待规则。登录状态额外保留最近 24 条步骤记录，覆盖准备接码、选择方式、提交验证码及保存结果，不记录验证码、密码或收件箱会话 URL。

本地及服务器候选镜像均通过 49 项相关测试；两组真实 Chromium 模拟测试通过。邮件专项已改为完整的“邮箱→密码→selection 重复邮件选项→ipe/verify→新邮件码→身份核对→Cookie 导出”路径，并保留取消和旧码排除测试。此次未自动提交真实 Google 登录或验证码，真实账号全流程仍待用户重试确认。

北京时间 2026-11-10 22:37:48 切换完成。镜像 `youtube-downloader-app:account-email-choice-20261110-r1`，ID `sha256:beaf76bbd63e8b8b7ae81d64ef706cc050e4f2306af67aeaaddc13fafbbf5ffe`；应用容器 `5c819f1f32c48905b0f92248e509bfc255fafc58c230ae5eaf16a856be5721ba`。只覆盖 `scripts/login_youtube.py`、`webapp/email_codes.py`、`webapp/youtube_login.py`，无数据库结构、依赖、下载配置变更，运行文件哈希一致。

现有登录结束后备份，再暂停领取新任务，等待原 14 项任务自然完成，约 185 秒连续两次确认无运行任务后切换。健康检查通过并恢复原未暂停状态。核验时容器启动约 88 秒、重启为 0；账号 21 条、Cookie 22 份，关联账号 19 个、关联 Cookie 20 份，数据及加密保持。执行任务 9 项，其中传输 3 项、准备 6 项。部署时用户配置为两次启动间隔 50 秒、任务结束后休息 20 秒、账号池上限 20，切换前后保持一致。

MySQL 和 Redis 容器保持健康且 ID 不变，异常 MySQL 仍停止。当前行锁等待为 0，上线窗口日志未发现前述程序异常。再次备份完整表结构及上述十张元数据表，复用并备份原加密密钥；未全量备份任务和媒体数据，本地 `.env` 未修改。

发布与核验位于 `.cache/account-email-choice-deploy/` 及服务器 `/opt/youtube-downloader/releases/account-email-choice-20261110-r1`。旧应用以 `youtube-downloader-local-app-1-before-account-email-choice-20261110-r1` 保留停止，回退镜像标记为 `youtube-downloader-app:before-account-email-choice-20261110-r1`。

## 实际验证码输入框修复（22:59:34）

用户保留在填码页的浏览器经只读检查确认：路径为 `/v3/signin/challenge/ipe/verify`，可见输入框实际是 `id="idvPinId" name="Pin" type="tel"`，原选择器未覆盖，因而直接交由手动处理，并未开始轮询新邮件。现已兼容这两个字段，保留旧字段兼容；提交按钮增加可见“Next/下一步”角色匹配，覆盖实际不同大小写、无表单的按钮容器。

2026-11-10 22:59:34 +08:00 切换为 `youtube-downloader-app:account-email-pin-20261110-r1`，镜像 ID `sha256:6d04988b3fb283b705ed34692278a6291d305f4f1f362ebac49f625874a76a76`，容器 ID `500db244283307149114d80dcb30e7a1b17023eed02447697c7fcd3deb9a225d`。仅覆盖 `scripts/login_youtube.py`，服务器 49 项相关测试和两组 Chromium 模拟测试通过。原 13 项任务自然排空约 141 秒后切换；健康检查、数据配置哈希、原密钥、MySQL/Redis 身份均核对一致，恢复原调度状态，重启数 0，当前数据库锁等待 0。本地 `.env` 未修改。

上线后针对用户当前同一账号做真实自动登录复测。第一次未匹配到可操作的邮件选项；第二次成功选中邮件方式，但进入 `/challenge/ipe/consent` 后 Google 持续显示加载动画，发送按钮存在但隐藏。约 260 秒观测结束仍未出现可操作按钮，未进入验证码输入步骤，未手动填码，也未新增 Cookie。以上结果不能视为真实邮件接码成功；没有证据将持续加载归因于某个网络、账号或 Google 风控原因。

此后补充发送确认页处理：只有本次会话先明确选择当前完整邮箱后，才会在该 Google 路径点击可见、可用的“Send/发送”，最多一次；按钮隐藏、禁用或发送后未跳转超过 30 秒时提供具体接管提示，不强制点击隐藏控件或重复请求验证码。Chromium 模拟覆盖可见、隐藏、禁用、未经本次邮箱选择四种确认页，以及包含发送确认页的新码接收、提交、邮箱核对、Cookie 导出完整流程。

## 发送确认页与超时提示部署（23:35:59）

2026-11-10 23:35:59 +08:00 切换为 `youtube-downloader-app:account-email-consent-20261110-r1`，镜像 ID `sha256:3ce85e1c6045f79521845dc538509fcb032fa3b12939dd0b54c0933f6566b8ee`，容器 ID `c8da8b7b0580835f23a519adf803a1f6bb98b74f21499561b7416a7bcbcb0b99`。仅覆盖登录脚本；本地、服务器 49 项相关测试及两组 Chromium 模拟测试通过，包括 13 组输入框、来源、发送确认页兼容场景。

原计划等待 12 项任务自然排空。等待约 467 秒后，检查发现实例已收到新的停止下载指令，最后一个大视频返回队列；部署保护中止首次切换，未停止旧应用。核对实例无错误后采用当前 `paused=1, stop_requested=1` 的运行意图，再次两次确认无运行任务后切换。更新保留停止状态，没有覆盖停止指令，也未自动恢复下载。

原应用以 `youtube-downloader-local-app-1-before-account-email-consent-20261110-r1` 保留停止，回退镜像 `youtube-downloader-app:before-account-email-consent-20261110-r1`。发布目录 `/opt/youtube-downloader/releases/account-email-consent-20261110-r1` 保留原表结构及十张元数据表备份、原密钥备份、首次等待与重新切换记录。下载配置、账号及 Cookie 元数据、挂载、密钥核对一致，无数据库结构或依赖变更；本地 `.env` 未修改。

上线核验容器健康、重启数 0，MySQL/Redis 身份保持且健康，异常 MySQL 仍停止，当前锁等待 0。账号 21 条、Cookie 22 份，关联账号 19 个、关联 Cookie 20 份；启动间隔 50 秒、任务结束休息 20 秒、池上限 20 保持。上线窗口约 132 秒日志未发现 Traceback、OperationalError、死锁、锁等待超时或 OOM。接口、前端资源、加密、过期版本拦截正常，实例保持 `paused=1, stop_requested=1`。

23:37 和 23:39 的同账号真实复测停在选择方式页，未提交验证码、未增加 Cookie。23:39 会话经只读页面检查得到确定原因：两个当前邮箱选项均附带 `Unavailable because of too many attempts. Please try again later.`。因此严格文本匹配没有点击带限制说明的选项；不是邮箱目标变化或验证码输入框修复未上线。停止继续登录尝试，不推测 Google 的恢复时长。真实自动接码仍未验证通过，需邮件验证方式恢复可用后再验证。
