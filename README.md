# auto-video-download-and-upload

视频下载、分段处理与多平台定时发布工具。提供 Flask Web 工作台和命令行入口，通过 yt-dlp 下载视频及配套资源，使用 FFmpeg 处理媒体，再通过 Playwright 上传到创作者平台。

## 功能

- 下载视频、封面、源站字幕和视频元数据，支持单个视频及播放列表。
- 按时长分段，转码为 H.264/AAC MP4，并生成平台所需封面。
- 扫码登录抖音、快手，填写标题和话题，设置可见范围与定时发布。
- 发布前预览排期，或仅生成分段、封面和任务清单。

| 平台 | 支持情况 |
| --- | --- |
| YouTube | 视频与配套资源下载；按需配置代理和登录 Cookie |
| 抖音 | 上传、横竖封面、话题、可见范围与定时发布 |
| 快手 | 上传、自定义封面、话题、可见范围与定时发布 |
| 西瓜视频 | 网页入口迁移后的发布流程待适配 |

B 站上传及 Whisper 翻译为历史脚本，尚未接入当前主流程。源站已有字幕的下载不依赖 Whisper。

## 安装

需要 Python 3.11+、FFmpeg（含 ffprobe）。YouTube 下载还需要 Deno 或 Node.js 22+；命令行扫码登录和发布需要可用的浏览器。

以下示例使用 Windows PowerShell：

```powershell
git clone https://github.com/Roclp/auto-video-download-and-upload.git
cd auto-video-download-and-upload
.\run.ps1 -Mode setup
.\.venv\Scripts\python.exe -m playwright install chromium
.\run.ps1 -Mode check
```

将 FFmpeg 加入 `PATH`，或将 `ffmpeg.exe`、`ffprobe.exe` 放到 `.tools/ffmpeg/bin/`。下载地址见 [FFmpeg 官网](https://ffmpeg.org/download.html)。

已有 Chrome 或 Edge 时，可以跳过 Chromium 安装；程序会尝试使用 Windows 标准安装路径中的浏览器，也可通过 `browser_executable` 指定路径。

## Web 工作台

安装依赖后启动：

```powershell
.\run.ps1 -Mode setup
.\run.ps1 -Mode web
# 指定配置文件和端口：
.\run.ps1 -Mode web -Config settings.yaml -Port 5001
```

浏览器访问 **http://127.0.0.1:5000**。也可以直接运行：

```powershell
.\.venv\Scripts\python.exe app.py --port 5000 --config config.yaml
```

Web 版使用 Flask + Waitress，页面资源随项目提供，不依赖前端构建或 CDN。目前页面专用于 YouTube 视频下载，提供：

- **下载概览**：固定 YouTube 链接前缀 `https://www.youtube.com/watch?v=`，每行只需粘贴一个 11 位视频 ID；也兼容完整视频或播放列表链接。点击“批量开始下载”后，每个输入单独排队，重复输入自动去重。
- **逐条下载进度**：按批次查看每个视频的标题、下载阶段、进度、已下载大小、速度、当前文件预计剩余时间和独立日志。音视频分开下载时按轨道估算进度，合并处理结束后才标记完成；总大小或速度未知时显示占位。某条失败会继续下一条，下载记录中的重复视频显示“已下载过 · 已跳过”。进度存入数据库，刷新页面后仍可查看。
- **大批量下载**：支持 TXT/CSV 流式导入、数据库去重与导入断点，任务记录和素材分页查询，首页进度仅显示各批次中当前正在下载的视频。可在设置页调整 1–16 个下载进程、有限重试、磁盘预留和日志保留，Clash 节点按进程独立轮换。生产使用 PostgreSQL，保留 SQLite 便携模式；迁移、备份恢复和验证范围详见 [大批量下载说明](SCALING.md)。
- **停止与继续**：首页点击“停止全部下载”结束当前下载和清单导入，保留队列、成品和部分文件；点击“继续下载”恢复，源站支持时断点续传。停止状态跨刷新和服务重启保存，手动停止不算下载失败。
- **YouTube Cookie 上传**：左侧 YouTube、首页下载区或设置页点击“上传 Cookie 文件”，上传 Netscape 格式的 `.txt`（最大 1 MB）。自动保存到所选配置目录下的 `Cookies/youtube.txt` 并更新 `youtube_cookie`，后续下载直接使用；校验失败保留原文件。只保存未过期的 YouTube 域记录，文件存在不代表登录仍有效。
- **素材库**：查看下载的视频原片、封面与文件大小；点击卡片播放，支持拖动进度和保存视频。未合并的音视频轨只显示为一条“待合并”素材，合并成品优先展示。
- **YouTube 浏览器登录**：首页、左侧 YouTube 和设置页可打开独立浏览器登录，使用当前下载代理，支持状态查看、取消和重试。确认登录后自动保存 Cookie 并更新配置，已有排队任务也会使用新凭据；有下载任务时先停止下载。浏览器窗口在运行服务的电脑上打开，详情见 [登录说明](YOUTUBE_LOGIN.md)。
- **下载前登录检测**：YouTube 下载开始和队列恢复前，通过当前下载节点验证 Cookie 登录状态；失败会停止队列并保留任务，不消耗重试次数。首页可手动“检测登录”，成功结果按 Cookie 和节点共享缓存最长 5 分钟。
- **任务记录**：仅展示已结束的下载、导入、索引或环境检查任务（成功、失败、中断），支持分页、搜索、结果筛选和日志查看。排队任务不显示在记录中，当前下载进度显示在首页。
- **代理与节点**：在页面保存 Clash / Mihomo 订阅，更新订阅、选择节点、单节点或批量测速、搜索和排序，并测试 YouTube 连通性。支持直连、手动代理和订阅代理三种下载连接方式。
- **自动轮换节点**：在“代理与节点”中勾选至少两个节点，设置每处理多少个视频切换一次（1–1000），可选择失败任务是否计数。按勾选顺序循环，从当前节点开始（如已勾选），在下一个视频开始前切换；已下载而跳过的任务不计数。页面显示轮换顺序、累计次数和下一次使用的节点，任务进度显示实际下载节点。计数跨批次、跨服务和内核重启保存，修改轮换配置或手动切换节点后重新计数。只在 Clash 模式生效；每行输入计一个任务，完整播放列表整体计一次。
- **下载设置**：编辑保存目录、子目录、默认链接、代理，并上传 Cookie 文件。保存到所选 YAML 文件，保留其他配置项；保存会重新格式化 YAML，不保留注释。

抖音、快手登录及发布、排期、分段预处理入口已从页面隐藏，原有命令行和后台接口保留。下载设置的保存及下载任务不受发布配置校验影响。

订阅代理使用项目独立的 Mihomo 内核。Windows x64 首次使用执行 `.\.venv\Scripts\python.exe scripts/install_mihomo.py`，随后在“代理与节点”中粘贴支持 Clash YAML 的订阅链接，点击“保存并更新订阅”。导入成功会启动内核并将下载连接切换为 Clash；已有订阅时，链接框留空即可更新。流量、到期等提示与可选节点分开展示。测速使用 gstatic 连通性地址，YouTube 检测单独执行，延迟不代表下载带宽。

内核仅监听本机，不修改系统代理。内核、私有订阅配置分别保存在 `.tools/mihomo/`、`.cache/web/clash/`，这些目录已被 Git 忽略；接口不回显订阅令牌、控制密钥或节点凭据。下载任务记录所选节点并在执行前核对，队列未结束时禁止修改轮换配置、更新订阅、手动切换节点、改变连接方式或停止内核；自动轮换仅在视频任务之间执行。关闭网页或重启 Flask 不会停止代理，重启后会复用本项目的内核进程和选中的节点。Web 的订阅代理配置不自动应用于独立命令行下载；命令行可用 `--proxy` 显式指定代理地址。

任务由独立下载进程执行，默认 1 个，可配置到 16 个，并为导入和索引保留独立维护进程。生产队列和元数据使用 PostgreSQL；未配置时使用 SQLite，连接配置、日志和会话密钥保存在 `.cache/web/`。关闭网页或停止 Web 服务后，后台进程会继续处理队列；重新启动服务可以继续查看。下载、导入和索引的过期租约可有限恢复，临时网络错误退避重试；发布等任务仍需人工核对后重试。下载使用创建时的配置快照；进程数、磁盘预留及日志清理参数在运行时读取。

缺少 FFmpeg / ffprobe 时，页面仍可使用。需要合并的 YouTube 下载会在开始前提示安装 FFmpeg，避免将分离轨道误报为下载完成。默认优先选择 H.264 视频和 AAC 音频，便于浏览器播放；不受浏览器支持的格式可保存到本机播放。环境卡片检查下载所需工具，点击“检查”可获得下载依赖检查日志，不检查发布用浏览器。

默认仅监听本机，定位为个人本地工作台，未提供用户账号登录。若需要局域网或服务器部署，应在受信任网络中通过带身份验证的反向代理访问，并显式设置允许的主机名，例如 `$env:VIDEO_WEB_HOSTS = 'localhost,127.0.0.1,video.example.com'`，再运行 `app.py --host 0.0.0.0`。上传浏览器代理仍可通过 `VIDEO_BROWSER_PROXY` 设置，或使用 `run.ps1 -Mode web -UseWifi`。

主要 HTTP 接口：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/status` | 工具与本地登录态文件状态 |
| `GET /api/clash` | 代理运行状态、订阅摘要、节点与测速进度，不含凭据 |
| `POST /api/clash/subscription` | 保存并更新订阅；JSON `url` 留空使用已保存链接 |
| `POST /api/clash/select` | 使用 JSON `name` 选择节点 |
| `POST /api/clash/rotation` | 保存轮换：JSON `enabled` 开关、`nodes` 有序节点名列表、`every` 间隔和 `count_failures` 失败是否计数 |
| `POST /api/clash/delay` | 异步测速；JSON `name` 可选，省略则测试全部节点 |
| `POST /api/clash/test` | 通过当前节点测试 YouTube 连通性 |
| `POST /api/clash/start`、`POST /api/clash/stop` | 启动或停止项目代理 |
| `POST /api/youtube/cookie` | 以 multipart 的 `cookie` 字段上传 YouTube Netscape Cookie 文件 |
| `GET /api/config`、`PUT /api/config` | 读取和保存页面支持的配置项 |
| `GET /api/media`、`POST /api/media` | 列出素材；以 multipart 的 `video`、`cover` 字段导入素材 |
| `GET /media/videos/<name>` | 本地视频播放与保存，支持 HTTP Range 分段请求 |
| `GET /api/youtube/login`、`POST /api/youtube/login` | 查看独立浏览器登录状态、启动登录 |
| `POST /api/youtube/auth/check` | 主动检测 YouTube 登录；未通过返回 409 并停止下载队列 |
| `POST /api/youtube/login/cancel` | 取消登录并关闭独立浏览器，保留原 Cookie |
| `GET /api/tasks`、`POST /api/tasks` | 任务记录和总数；创建后台任务 |
| `POST /api/downloads` | JSON `urls` 支持多行视频 ID 或链接，原子创建一个批次及逐条下载任务，返回 `batch_id` 和 `items` |
| `POST /api/downloads/stop`、`POST /api/downloads/resume` | 停止 / 继续整个下载和清单导入队列；保留待处理任务及部分文件 |
| `POST /api/downloads/import` | multipart `file` 上传 TXT/CSV 清单，返回后台导入任务 |
| `GET /api/batches` | 持久化批次分页及每批计数 |
| `POST /api/media/reindex` | 创建后台素材索引同步任务 |
| `GET /api/tasks/<id>` | 状态、执行结果及错误 |
| `GET /api/tasks/<id>/logs?offset=0` | 从字节偏移量增量读取日志，返回下次 `offset` |

页面创建任务时传入 `scope: "download"`，`action` 为 `download` 或 `check`；下载可传 `urls`，检查仅验证下载环境。`GET /api/tasks?scope=download` 返回下载、检查、导入和索引任务，可用 `limit`、`cursor`、`batch_id`、`q`、`status` 分页筛选。加 `view=history` 仅查询已结束的任务；`view=active` 仅查询当前运行的下载任务，跨批次展示。原有接口仍支持 `preview`、`prepare`、`publish`、`login`；使用 `platforms: ["douyin", "kuaishou"]` 选择平台，`login` 一次选择一个平台，`publish` 必须传 `confirmed: true`。写接口需要同一浏览器会话的 Cookie 及页面 `<meta name="csrf-token">` 对应的 `X-CSRF-Token` 请求头。

首页批量下载使用 `POST /api/downloads`，单次最多 100 行。任务接口返回 `batch_id`、`video_id`、`url` 和结构化 `progress`；速度单位为字节/秒、ETA 单位为秒。每个完整播放列表链接属于一个输入任务，该任务的进度显示列表中当前视频。

## 配置

编辑 [config.yaml](config.yaml)。常用配置示例：

```yaml
path: videos
dir: ""
url: "https://www.youtube.com/watch?v=视频ID"

platforms: [douyin, kuaishou]
segment_seconds: 180
time_list: [8, 11, 14, 16, 19, 21]

douyin_cookie: Cookies/douyin.json
kuaishou_cookie: Cookies/kuaishou.json
douyin_visibility: private
kuaishou_visibility: private
douyin_tags: [游戏]
kuaishou_tags: [游戏]

# 按需启用，端口替换为自己的代理端口：
# proxy: "http://127.0.0.1:7890"
# youtube_cookie: Cookies/youtube.txt
```

`path` 与 `dir` 拼成视频目录；所有相对路径均以配置文件所在目录为基准。命令可通过 `-Config settings.yaml` 使用其他配置文件。

可见范围支持 `public`（所有人）、`friends`（好友）和 `private`（仅自己）。仓库默认配置为 `public`，上面的示例使用 `private`，适合首次试运行。

## 使用

### 1. 下载视频

```powershell
.\run.ps1 -Mode download -Url "https://www.youtube.com/watch?v=视频ID"
# 使用配置文件中的 url，也支持 URL 列表：
.\run.ps1 -Mode download
```

视频、同名封面、字幕和 `.info.json` 保存到配置的视频目录；字幕是否可用取决于视频源。播放列表中的视频分别保存，已下载视频通过配置文件目录下的 `videos.txt` 去重。

遇到登录验证时，可配置 Netscape 格式的 YouTube Cookie，详见 [YouTube 登录与 Cookie 配置](YOUTUBE_LOGIN.md)。

### 2. 登录上传平台

```powershell
.\run.ps1 -Mode login -Platform douyin
.\run.ps1 -Mode login -Platform kuaishou
```

在打开的浏览器中扫码或按页面提示登录。确认进入对应上传页后，程序自动保存登录态并关闭窗口。上传平台使用 JSON 登录态，不能与 YouTube 的 Netscape Cookie 文件混用。登录文件保存在本机 `Cookies/` 目录，请勿提交或分享。

### 3. 预览与准备

```powershell
# 预览视频分段数量与排期：
.\.venv\Scripts\python.exe main.py --dry-run --platforms douyin kuaishou

# 生成分段与封面，不提交平台：
.\run.ps1 -Mode prepare -Platform douyin
```

上传本地素材时，每个视频需有同名的 `.webp`、`.jpg`、`.jpeg` 或 `.png` 封面。支持 MP4、MKV、WebM、MOV 视频。

处理结果保存在视频旁的 `*.parts/` 目录，任务清单为视频目录中的 `prepared-jobs.json`。默认运行 `main.py` 只预览任务。

### 4. 定时发布

```powershell
.\run.ps1 -Mode publish -Platform douyin
.\run.ps1 -Mode publish -Platform kuaishou

# 同一批素材发布到两个平台：
.\.venv\Scripts\python.exe main.py --publish --platforms douyin kuaishou
```

`publish` 会实际提交作品。排期使用本机时间，从 `time_list` 选择至少两小时后、七天内的时段；每个分段占用一个时段。发布时会重新生成素材并计算排期，预处理清单仅供检查。

上传没有持久化去重记录。批次中断或结果未确认时，请先核对平台作品列表，再重试未完成的素材。平台接收提交后，审核及最终发布状态以创作者平台为准。

## 常见问题

**YouTube 提示登录或无法下载**

检查网络、`proxy`、JavaScript 运行时及 Cookie 是否有效。更新 yt-dlp 后可重新运行检查和测试；Cookie 导出步骤见 [YOUTUBE_LOGIN.md](YOUTUBE_LOGIN.md)。

**扫码窗口登录后自动关闭**

这是保存登录态后的正常行为。后续发布会读取配置中的登录文件；登录过期时重新运行 `login`。

**创作者页面一直超时**

可通过 `VIDEO_BROWSER_PROXY` 为上传浏览器单独指定代理。Windows 双网卡环境也提供 `-UseWifi` 选项，需预先安装 Xray；相关脚本为 `scripts/start_wlan_proxy.ps1` 和 `scripts/stop_wlan_proxy.ps1`。下载代理仍由 `proxy` 配置控制。

## 开发与测试

```powershell
.\run.ps1 -Mode tests
.\run.ps1 -Mode selftest
.\.venv\Scripts\python.exe -m pip check
# 使用真实浏览器验证 Web 页面（测试配置及素材独立保存，不投稿）：
.\.venv\Scripts\python.exe scripts/verify_web.py
```

回归测试覆盖配置、排期、媒体处理及上传流程；自检使用本机 HTTP 服务验证下载、分段、封面和解码，不需要平台账号。

请仅下载和发布你拥有相应使用权限的内容。
