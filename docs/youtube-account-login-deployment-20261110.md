# 账号定向浏览器登录部署记录

- 服务器：洛杉矶 `216.227.169.90`。
- 切换时间：2026-11-10 21:03:18 +08:00。
- 镜像：`youtube-downloader-app:account-login-20261110-r1`。
- 镜像 ID：`sha256:649ec77d6b9be20bd3acaa40556ba580ba1d2c5230d572b623c91c8a0502a797`。
- 应用容器 ID：`05a78807df61220a5a9b3fbc28c4797a9ee471d7467ddc43a0cac2371e889b95`。
- 发布目录：`/opt/youtube-downloader/releases/account-login-20261110-r1`，权限 `700`。

## 上线内容

账号资料列表右侧新增管理员“登录”按钮，详情中增加“登录此账号”。登录使用账号绑定实例；未绑定账号须明确选择在线实例。默认自动填写并提交已保存的邮箱和密码，可取消后手动操作。验证码、二次验证等由用户在浏览器完成。

成功后核对 YouTube 账号头部邮箱，在同一事务中新增 Cookie、登记归属并关联账号。邮箱不符或资料版本变化时拒绝保存。新 Cookie 需在账号池中启用，不自动改动原池成员或下载设置。密码仅在目标实例内存中解密，不返回前端或写入登录状态文件。每实例沿用单个浏览器登录锁。

本次以原生产镜像覆盖 9 个运行文件构建：`app.py`、`scripts/login_youtube.py`、`static/accounts.css`、`static/accounts.js`、`templates/accounts.html`、`webapp/account_login.py`、`webapp/account_routes.py`、`webapp/console_auth.py`、`webapp/youtube_login.py`。上线后文件哈希一致；无依赖或数据库结构变更。

## 验证与切换

本地 51 项相关测试通过；真实 Chrome 的模拟登录交互检查通过。服务器候选镜像在单 CPU、1 GiB 内存的隔离容器中通过 51 项 Linux 测试，另在随机独立 MySQL 8.4 测试库通过 8 项浏览器登录和多实例测试。测试库已清理，未向生产账号库写入虚构资料，未执行真实 Google 登录。

暂停领取新任务后，等待原 6 项运行任务自然完成，排空约 66 秒后切换。新容器通过 Docker 与业务健康检查，核对配置、端口、挂载、实例身份与账号资料后恢复原未暂停状态。健康 MySQL、Redis 容器未替换；异常 MySQL 保持停止。

上线后约三分钟检查：新容器重启次数为 0；账号资料 21 条、Cookie 20 份，18 个账号与 18 份 Cookie 的关联保留；账号加密可用。首页登录弹框、静态资源、账号登录状态和专用 RPC 接口正常。以过期版本请求验证返回 409，未启动真实账号浏览器登录。实例无暂停、停止请求或实例错误，已恢复执行任务。原两次启动间隔 50 秒、任务结束后休息 25 秒及账号池上限 20 保留。

随后只读采样确认正在执行 13 项任务，其中实际传输 6 项、准备 5 项、处理 1 项、等待传输 1 项，动态上限为 15。

MySQL 当前行锁等待为 0。启动日志未发现 Traceback、OperationalError、死锁、锁等待超时或内存不足记录。核验范围是上线窗口，不代表长期运行或全部上游请求均无异常。

## 备份与回退

发布目录保存原容器检查信息、配置、实例运行状态、切换与核验结果。数据库备份包含完整表结构，以及 `console_users`、`download_instances`、`schema_settings`、`youtube_account_audit`、`youtube_account_cookies`、`youtube_account_owners`、`youtube_accounts`、`youtube_cookie_bindings`、`youtube_cookie_profiles`、`youtube_profile_auth` 十张元数据表；未全量备份约 6 GB 的任务与媒体数据。账号、归属、关联及审计四张表的切换前后哈希一致。

复用并备份原账号资料加密密钥，核对与生产容器一致。原密钥文件 `/opt/youtube-downloader/secrets/account-encryption.env` 权限为 `600`，后续重建必须继续注入相同密钥，不得重新生成替代。本地 `.env` 未修改。

旧应用保留为停止状态的 `youtube-downloader-local-app-1-before-account-login-20261110-r1`，原镜像回退标记为 `youtube-downloader-app:before-account-login-20261110-r1`。回退前应排空任务并保留当前数据和原密钥。

本地发布与脱敏核验结果位于 `.runtime/cache-archive-20261111/account-login-deploy/`。使用方式见 [统一账号管理](youtube-account-management.md)。

## 自动填写兼容性修复（21:30:29）

用户反馈邮箱和密码没有自动填写。只读检查确认该次登录确实启用了自动填写，Google 邮箱输入框实际为 `id="identifierId"`、`name="identifier"`、`type="text"`；原逻辑只查找 `input[type="email"]`，匹配数为 0，因此没有执行邮箱提交。此前的模拟测试未覆盖这种真实页面结构。

修复为同时识别 Google 的输入框 ID、名称及 email 类型，只选可见字段。密码与验证码查找也排除隐藏字段，避免隐藏输入框挡住后面的真实输入框。继续限制为 Google HTTPS 登录来源，每字段只提交一次，遇到验证码或二次验证交由用户处理。

本地与服务器均通过 28 项相关回归；新增 `scripts/verify_youtube_autofill.py` 使用真实 Chromium，拦截全部请求并只加载虚构页面，覆盖文本邮箱框、隐藏字段、后续密码提交、单次提交、验证码交接和来源限制五组场景。未提交真实账号或密码，未变更数据库结构和依赖。

修复镜像为 `youtube-downloader-app:account-login-autofill-20261110-r1`，ID 为 `sha256:8e77d1a166379ca9dd1d452d3f00bcc218c3df6c8f693005063eabb39983c077`；应用容器为 `3cf9eeffe39fde0eb4fa4cb1920d0645125bf6d9040df8a4a41a2f5e4f39f147`。北京时间 2026-11-10 21:30:29 完成切换，恢复原未暂停状态，唯一运行文件变更为 `scripts/login_youtube.py`，部署哈希核验一致。

等待原 14 个任务自然结束，排空约 490 秒；最后一个大视频持续下载并完成后才切换。账号资料、Cookie、关联、加密密钥、实例身份、下载配置、端口与挂载保持一致。再次备份完整表结构和上述十张元数据表，未全量备份任务与媒体数据。发布与核验记录位于 `.runtime/cache-archive-20261111/account-login-autofill-deploy/` 和服务器 `/opt/youtube-downloader/releases/account-login-autofill-20261110-r1`。旧应用以 `youtube-downloader-local-app-1-before-account-login-autofill-20261110-r1` 保留，原镜像回退标记为 `youtube-downloader-app:before-account-login-autofill-20261110-r1`。

上线核验通过：健康检查正常、重启为 0、账号 21 条、Cookie 20 份及 18 个关联保留；正在执行 13 项任务，其中传输 4 项。MySQL 当前行锁等待为 0，启动日志未发现上述程序异常。另用全新无凭据浏览器只读打开真实 Google 登录页，确认新选择器能匹配当前可见的文本类型邮箱框；该检查未填写或提交真实账号，未验证 Google 对真实账号的后续登录放行。
