# YouTube 视频下载服务

这是一个 Flask Web 下载工作台，面向批量 YouTube 视频 ID 清单。支持 yt-dlp、yt5s、yt5s 失败回退、YouTube Cookie、Clash/Mihomo 节点、并行下载、实时日志、素材库和下载统计。

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
- 方式一使用 yt-dlp；方式二使用 yt5s；方式三优先 yt5s，失败后自动切换 yt-dlp。
- 下载前检查 YouTube Cookie、FFmpeg、JavaScript 运行时和浏览器环境。
- 支持直连、手动代理、Clash 订阅代理、节点测速和自动轮换。
- 支持 1–16 个下载进程，以及单独的 yt5s 浏览器并发数。
- 支持指定目录保存、测试模式和下载统计；网盘保存暂未实现。
- 任务记录只显示已结束任务，首页只显示当前正在下载任务；素材库支持封面和视频播放。

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
