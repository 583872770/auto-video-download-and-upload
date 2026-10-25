# YouTube 视频下载服务

这是一个 Flask Web 下载工作台，面向批量 YouTube 视频 ID 清单。支持 yt-dlp、yt5s、yt5s 失败回退、downmp3 API、YouTube Cookie、Clash/Mihomo 节点、并行下载、实时日志、素材库和下载统计。

项目只保留 YouTube 下载流程，正式部署使用远程 MySQL 5.7.9+ 或 MySQL 8.x。

## Docker 部署

项目只有一个 Compose 编排文件，内置 FFmpeg、Node.js、Mihomo、Chromium、Xvfb 和 noVNC，连接远程 MySQL：

```bash
cp .env.remote-mysql.example .env
# 编辑 .env，填写 MYSQL_HOST、MYSQL_DATABASE、MYSQL_USER、MYSQL_PASSWORD
docker compose -f compose.remote-mysql.yaml config --quiet
docker compose -f compose.remote-mysql.yaml up -d --build
```

访问 `http://127.0.0.1:5000`。服务器部署时，可在 `.env` 设置 `WEB_BIND_IP`、`WEB_PORT` 和 `VIDEO_DOWNLOAD_DIR`。完整说明见 [DOCKER.md](DOCKER.md)、[MYSQL.md](MYSQL.md) 和 [YT5S_LINUX.md](YT5S_LINUX.md)。

## 本地启动

需要 Python 3.13+、FFmpeg、Node.js 或 Deno，以及已安装的依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe app.py --port 5000 --config config.yaml
```

本地启动默认使用状态目录中的 SQLite 便携模式，仅用于开发和测试；生产环境请设置 `VIDEO_DATABASE_URL` 或使用 Docker 连接 MySQL。

## 页面功能

- 首页输入固定 YouTube 前缀后的 11 位视频 ID，也支持完整链接和清单导入。
- 首页勾选需要的下载器（yt-dlp、yt5s、downmp3），用上下箭头设置优先级；首页修改自动保存，下载设置页需点击保存。每个视频按顺序尝试，成功即停止，失败则切换到下一项。可以只选一种，不再单独提供固定的“方式三”。
- 导入只保存视频清单，不绑定下载配置，也不要求 Cookie、浏览器或代理已就绪。每个下载任务开始执行时读取页面最新的下载方式、画质、目录、保存模式、代理和重试上限；已运行的任务保持本次启动配置，暂停后继续及自动重试时重新读取。历史待处理任务同样适用，无需重新导入。
- 按下载方式检查所需环境与登录；仅启用 downmp3 时无需浏览器、JavaScript 运行时或 YouTube Cookie，使用 ffprobe 校验成品。
- 支持直连、手动代理、Clash 订阅代理、节点测速和自动轮换。
- 支持 1–16 个下载进程，以及单独的 yt5s 浏览器并发数。
- 支持指定目录保存、测试模式和下载统计；网盘保存暂未实现。
- 任务记录只显示已结束任务，首页只显示当前正在下载任务；素材库支持封面和视频播放。

## downmp3 API

在首页或下载设置中勾选 downmp3，并调整优先级。例如只勾选 downmp3、yt5s，顺序设为 downmp3 → yt5s，失败后就自动尝试 yt5s，无需 Cookie；勾选 yt-dlp 后，开始前还会检查 YouTube Cookie。下载设置可选择 360p、480p、720p、1080p，默认 720p；转换等待上限默认 300 秒，可设为 30–1800 秒。支持视频 ID 清单和 TXT / CSV 批量导入，暂不支持播放列表。

根据 [downmp3 接入文档](https://downmp3.yt/api-documentation) 中嵌入组件的实际调用流程，后端请求 `p.savenow.to/api/v2/download` 创建转换任务，轮询 `/api/progress` 获取 MP4 链接，再使用现有传输模块保存文件、封面和元数据。转换阶段单独显示进度；下载阶段显示实际下载字节数、速度和剩余时间。所有请求使用任务当前代理节点，不向第三方发送 YouTube Cookie；不占用 yt5s 浏览器并发名额。

下载后用 ffprobe 确认文件含音视频，再登记实际字节数。下载统计按 `downmp3` 独立统计成功率、失败数、计费大小和测试大小；测试模式删除成品前保留大小记录。临时网络错误和 429 / 5xx 按队列规则退避重试，转换等待有超时上限。

该站公开文档主要描述嵌入组件，后端接口来自组件当前实现，可能发生变更、限流或要求网站验证。遇到转换或下载失败时，按页面配置继续尝试下一下载器；没有后续下载器时报告失败。停止下载、磁盘已满、权限不足及缺少运行环境不会触发切换。它没有官方批量吞吐保证，建议先验证自己的节点和清单再扩大并发。

## 命令行

```powershell
# 启动 Web 服务
.\run.ps1 -Mode web

# 直接使用 yt-dlp 下载
.\run.ps1 -Mode download -Url "https://www.youtube.com/watch?v=视频ID"

# 打开独立 YouTube 登录浏览器
.\run.ps1 -Mode youtube-login

# 运行自动化测试
.\run.ps1 -Mode tests
```

YouTube Cookie 可在页面上传 Netscape 格式文件，也可使用独立浏览器登录。登录说明见 [YOUTUBE_LOGIN.md](YOUTUBE_LOGIN.md)。

## 数据迁移与容量

旧 SQLite 数据迁移到 MySQL 使用 [MYSQL.md](MYSQL.md) 中的 `scripts/migrate_mysql.py`。大批量导入、并发模型和容量边界见 [SCALING.md](SCALING.md)。

项目仅处理用户有权下载的内容。

下载器优先级也可在 YAML 中配置，例如：

```yaml
download_methods:
  - downmp3
  - yt5s
```

至少选择一个，不能重复。旧的 `download_method` 配置仍可读取；旧的 `yt5s_fallback` 自动对应 `yt5s → yt-dlp`。已导入或排队的任务在开始时读取当前已保存的顺序，运行中的任务使用本次启动配置。各下载器使用各自的画质配置；切换后重新下载，避免拼接不同编码的残留文件。
