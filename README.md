# auto-video-download-and-upload

视频下载、分段处理与多平台定时发布工具。通过 yt-dlp 下载视频及配套资源，使用 FFmpeg 处理媒体，再通过 Playwright 上传到创作者平台。

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

需要 Python 3.11+、FFmpeg（含 ffprobe）和可用的浏览器。YouTube 下载还需要 Deno 或 Node.js 22+。

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
```

回归测试覆盖配置、排期、媒体处理及上传流程；自检使用本机 HTTP 服务验证下载、分段、封面和解码，不需要平台账号。

请仅下载和发布你拥有相应使用权限的内容。
