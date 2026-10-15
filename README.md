# auto-video-download-and-upload

下载视频、封面、字幕和元数据，再将本地视频分段并通过浏览器上传到抖音、快手或西瓜。

当前维护版已实际跑通 YouTube 登录态下载、本地 HTTP 下载、字幕与元数据保存、视频分段、封面生成，并通过 49 项回归测试。快手、抖音均已用真实账号完成上传、话题、封面、私密设置、定时提交，并在管理页复核对应测试作品。西瓜合并后的分发能力仍未验证。B 站与 Whisper 翻译旧脚本的限制见下文。

2026-10-15 用同一份约 60 秒的 YouTube 实际下载素材完成两平台完整链路：H.264/AAC 转码、封面、话题、私密定时提交，管理页找到对应作品和当天 19:00 排期。另有此前各一条 7 秒自生成素材测试，总计四条测试投稿。尚未确认到时发布或审核通过，请勿重复运行这批测试配置。

## Windows 快速启动

在项目目录执行：

```powershell
.\run.ps1 -Mode setup     # 首次安装 Python 依赖
.\run.ps1 -Mode check     # 检查 FFmpeg、浏览器、Python 依赖和 JS 运行时
.\run.ps1 -Mode selftest  # 本机 HTTP 实际下载，再分段、生成封面并逐段解码
.\run.ps1 -Mode tests     # 回归测试
```

自检不需要账号，会在 `.cache/validation/pipeline-*/` 生成素材、处理结果和 `report.json`。FFmpeg 和浏览器安装方式见下一节；YouTube 还需 Deno 或 Node.js 22+。运行器不会自动安装这些系统工具。

业务命令支持 `-Config` 指定配置文件：

```powershell
.\run.ps1 -Mode download -Url "https://www.youtube.com/watch?v=你的视频ID"
.\run.ps1 -Mode youtube-login  # 在独立浏览器手动登录，使用配置中的下载代理
.\run.ps1 -Mode prepare -Platform kuaishou
.\run.ps1 -Mode login -Platform kuaishou
.\run.ps1 -Mode publish -Platform kuaishou
```

`download` 读取配置中的下载代理与 YouTube Cookie；`prepare` 只生成素材。`login` 打开独立浏览器并等待扫码，`publish` 才实际提交平台。双网卡网络问题可对 `login` / `publish` 加 `-UseWifi`，需要本机已有 Xray，详见文末。

## 安装与检查

需要 Python 3.11+、FFmpeg（包含 ffmpeg 和 ffprobe）及 Playwright。浏览器优先使用 Playwright Chromium；未安装时尝试 Windows 标准路径中的 Edge/Chrome，也可设置 `browser_executable`。本次在 Windows、Python 3.13.5 上验证，并实际启动本机 Chrome 完成浏览器测试。

在项目目录执行 PowerShell 命令：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
# 若没有可用的浏览器，再执行下面这行：
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe main.py --check
```

FFmpeg 可从[官方列出的 Windows 构建](https://ffmpeg.org/download.html#build-windows)获取，将 ffmpeg.exe 与 ffprobe.exe 放入 PATH，或放到项目的 `.tools/ffmpeg/bin/`。程序自动识别该项目目录，不修改系统 PATH。可选的项目内 Chromium 路径为 `.tools/ms-playwright/`；安装到此处需设置 `PLAYWRIGHT_BROWSERS_PATH`。

`requirements.lock.txt` 固定本次验证的 Python 依赖；`requirements.txt` 定义允许升级的范围，包含 `yt-dlp[default]` 所需 EJS 组件。下载器自动启用 PATH 中的 Deno 或 Node.js，也可在配置中设置 `js_runtimes`。YouTube 支持可能需要更新 yt-dlp，升级后请重新运行测试。JavaScript 运行时与 EJS 要求以 [yt-dlp 文档](https://github.com/yt-dlp/yt-dlp#dependencies)为准。

## 下载

编辑 `config.yaml`。`path` 与 `dir` 拼成视频目录，下载与上传共用该目录；所有相对路径均相对于配置文件。播放列表的各个视频分别保存到此目录，不再自动拼接成一个大视频。文件名包含视频 ID，降低同名覆盖风险。

```powershell
.\.venv\Scripts\python.exe Download.py "https://www.youtube.com/watch?v=你的视频ID"
# 不传 URL 时读取配置中的 url；支持 URL 列表。
.\.venv\Scripts\python.exe Download.py
# 联网读取信息，不下载媒体。
.\.venv\Scripts\python.exe Download.py --simulate "https://www.youtube.com/watch?v=你的视频ID"
```

需要代理时设置配置中的 `proxy` 或传入 `--proxy`。YouTube Cookie 使用 Netscape 文本格式，通过 `youtube_cookie` 配置；上传平台使用 Playwright storage-state JSON，两者格式不同。下载进度通过项目根目录的 `videos.txt` 去重。

YouTube 要求登录时，可执行 `run.ps1 -Mode youtube-login -Config 你的配置文件.yaml`。在独立浏览器中手动完成正常登录，程序确认账号头像出现后，仅保存本次会话的 YouTube 域 Cookie 到 `Cookies/youtube.txt`，不读取个人浏览器资料或导出 Google 域凭据。随后在下载配置中设置 `youtube_cookie` 指向该文件（相对路径按配置文件目录解析），再重试下载。若网页拒绝登录或仍要求验证，命令不能保证下载成功。

本机自动化浏览器登录遇到 Google 的“此浏览器或应用可能不安全”拒绝。辅助脚本识别后停止；用户改用正常浏览器手动导出 YouTube 站点 Cookie 后，实际下载成功：`MmB9b5njVbA`，约 60 秒、720p，封面、英文 VTT 字幕与元数据齐全，FFmpeg 完整解码通过。导出步骤见 [YOUTUBE_LOGIN.md](YOUTUBE_LOGIN.md)。这次结果不能保证其他视频或过期登录态仍可下载。

下载会请求封面、人工字幕、自动字幕和 `.info.json`；是否有字幕取决于视频源。可通过 `--output` 覆盖目标目录；相对路径仍相对于配置文件。此工具需要可用网络，未实现免代理访问 YouTube。

## 登录、预览与上传

```powershell
.\.venv\Scripts\python.exe main.py --login douyin
.\.venv\Scripts\python.exe main.py --login kuaishou
# 可选平台：xigua
```

登录命令打开独立浏览器会话，不使用个人浏览器配置。使用对应平台 App 扫码或按网页提示登录，程序最多等待 10 分钟，确认进入上传页面后自动保存登录态并关闭浏览器，无需调试器操作。未完成登录时不会覆盖旧文件。Cookie 与本地工具目录均已加入 `.gitignore`。

确保每个视频有同名 `.webp`、`.jpg`、`.jpeg` 或 `.png` 封面。主程序识别 mp4、mkv、webm、mov（扩展名不区分大小写）。先预览任务：

```powershell
.\.venv\Scripts\python.exe main.py --dry-run
.\.venv\Scripts\python.exe main.py --dry-run --platforms douyin
```

默认运行 `main.py` 也只预览；预览会探测时长，但不分段、不写封面、不登录、不发布。需要先生成可检查的真实素材时执行：

```powershell
.\.venv\Scripts\python.exe main.py --prepare --platforms douyin kuaishou
```

该命令生成分段、平台封面和视频目录中的 `prepared-jobs.json`，不登录或提交平台。清单记录文件、封面与本次排期，仅供检查；实际发布会重新计算排期和生成素材。需要真实上传时执行：

```powershell
.\.venv\Scripts\python.exe main.py --publish --platforms douyin kuaishou
```

`time_list` 使用本机本地时间；只安排两小时后、严格小于七天的时段。排期不足时，在分段与发布前报错。`segment_seconds` 默认 180 秒，实际平均分段并转码为 H.264/AAC MP4，避免直接复制流造成关键帧边界缺失或容器不兼容。转码会使用 CPU、增加耗时，并产生一次有损编码。

快手、抖音分别通过 `kuaishou_visibility` / `douyin_visibility` 设置 `public`（默认）、`friends` 或 `private`。快手封面尺寸为 1280×960，上传器等待“封面应用成功”再继续。抖音生成 1080×1440 竖封面和 1280×960 横封面，并在当前双封面编辑器中分别上传。两者都核对网页接受的排期值，避免表面填入后被组件恢复。

分段和平台封面位于 `<原视频文件名>.parts/`。分段先使用短文件名写入目标盘上的临时目录，全部成功后才供上传使用；每次发布重新生成分段。Windows 上的媒体处理支持超过传统 260 字符限制的路径。单次平台上传最多等待 `upload_timeout_seconds`（默认 1800 秒），超时退出；上传失败最多重试两次，页面关闭或重试过程报错立即停止。任务按顺序执行，错误会停止当前批次。

主程序只接受带成功确认的上传结果：抖音、快手等待作品管理页，快手还处理发布确认弹窗，西瓜等待成功通知。没有确认时报告“结果未确认”，不会继续后面的任务。登录文件使用临时文件写入后替换；发布已确认后的 Cookie 刷新失败会单独提示，不把已确认的发布标记为失败。

**目前没有持久化的上传去重记录。** 若批次中途失败，请先核对平台已发布内容，再调整本地待上传文件重试，避免重复投稿。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/verify_pipeline.py
# 可选的真实浏览器测试，所有页面请求均由本地 HTML 拦截，不登录或发布：
.\.venv\Scripts\python.exe scripts/smoke_browser.py
```

当前 49 项测试覆盖排期边界、配置路径、yt-dlp 参数、文件筛选、异常清理、登录判断、上传超时、失败重试、发布确认、预处理清单、Windows 长路径、快手及抖音排期确认、YouTube Cookie 格式与域过滤、Google 登录拒绝处理、代理隔离、跨平台跳转识别及整批分发。检测到 FFmpeg 时，还会生成带音频的视频，实际分段、逐段解码、检查时长、生成封面并执行任务预览，再将真实分段交给模拟上传器验证批次顺序。未安装 FFmpeg 时，该媒体测试会标记为跳过。

`verify_pipeline.py` 另行启动仅监听本机的 HTTP 服务，通过真实 yt-dlp 下载视频、封面、字幕和元数据，核对文件 SHA-256 后调用真实预处理入口，检查三个分段可解码及封面尺寸。报告分别标明本地结果、YouTube 和平台账号验证状态，避免混淆。

## 尚需验证或恢复的旧功能

- 快手已根据 2026-10-15 的真实页面修复新手引导遮挡、话题填写、封面和只读日期选择器，并成功提交一条 7 秒自生成测试视频：仅自己可见，2026-10-15 17:00 定时发布。作品管理页确认“待发布”及指定时间；这证明定时投稿已被接受，不代表已到时发布或审核通过。没有重复提交此测试作品。
- 抖音已在用户重新开放站点权限后完成扫码登录。按真实页面恢复横、竖双自定义封面，使用 label 控件设置可见范围，并验证标题、话题与排期。通过主程序提交一条 7 秒 720p 自生成测试视频，在管理页找到对应标题、“私密”及 2026-10-15 18:00 的排期。没有宣称审核通过、已经到时发布或已同步西瓜。
- 西瓜[官方升级说明](https://studio.ixigua.com/creator_upgrade)确认网页版与抖音创作者中心合并，升级后使用抖音账号管理。实际登录测试中用户看到抖音页面，旧独立西瓜流程未得到有效登录态。新增对跳往抖音创作者中心的识别：明确停止旧流程，不将抖音输入框误判成独立西瓜登录成功，也不自动进行账号升级或跨平台投稿。合并后的视频分发仍需联调。
- 当前抖音发布页没有显示西瓜同步选项。西瓜独立旧流程没有取得登录态；合并账号的绑定、能力开通与分发效果需要按实际账号进一步确认，不能把抖音投稿成功当作西瓜同步成功。
- `Uploaders/BiliUpload.py` 只修正了两个项目内导入路径，仍有硬编码目录、旧 biliup API 和登录数据未接入等问题，未纳入可运行主流程；本次未安装 biliup 或声称 B 站可用。
- `Utils/GenSubtitles.py` 引用了仓库没有提供的 `script/translate_tool`、`audio_tool`、`whisper_tool`。该 Whisper/翻译旧流程未恢复；下载源站已有字幕不依赖它。
- 已通过本机代理实际请求 YouTube 视频 `MmB9b5njVbA` 和短视频 `jNQXAC9IVRw`，并补齐 EJS 和 Node.js 运行环境；两条均返回“Sign in to confirm you're not a bot”，未下载成功。当前 yt-dlp 与官方最新稳定版一致。这不代表所有公开视频都必须登录，也尚未完全确定触发验证的原因；不能把本地 HTTP 自检视作 YouTube 测试通过。
- 快手、抖音扫码登录态已由用户完成并保存到本机，源码包不包含 Cookie。抖音曾被工具权限拒绝，用户后来明确重新允许后才继续实测。网络 HTTP 200 本身仅证明连接可达。

使用者应拥有下载与发布相应内容的权限。

## 双网卡下创作者页面连接超时

如果以太网的 IPv4 出口不可用、Wi-Fi 正常，而 Windows 仍优先选以太网，创作者页面可能一直超时。可只让项目浏览器通过 Wi-Fi 连接，不调整系统路由或 v2rayN 配置：

```powershell
# 使用本机已经安装的 Xray，启动只监听本机的 HTTP CONNECT 代理。
.\scripts\start_wlan_proxy.ps1
$env:VIDEO_BROWSER_PROXY = 'http://127.0.0.1:21931'
.\.venv\Scripts\python.exe main.py --login douyin
# 扫码成功后，保持同一 PowerShell 会话，可继续 --dry-run / --publish。
# 测试全部完成后停止项目代理：
.\scripts\stop_wlan_proxy.ps1
Remove-Item Env:VIDEO_BROWSER_PROXY -ErrorAction SilentlyContinue
```

`start_wlan_proxy.ps1` 支持 `-InterfaceAlias`、`-Port` 和 `-XrayPath`。默认使用 WLAN、21931 端口，以及当前用户桌面 v2rayN 目录下的 Xray。它创建一个独立的临时进程和项目内配置，不改已运行的 v2rayN。HTTPS 仍由浏览器与目标站点直接协商和校验。Wi-Fi 的 IPv4 地址变化时，需要停止代理后重新启动。

此代理是 Wi-Fi 直连出口，不提供 YouTube 所需的外网访问能力。YouTube 下载仍使用独立的 `--proxy` / `proxy` 配置。
