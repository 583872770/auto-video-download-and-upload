# 账号登录浏览器

正式镜像包含 Chromium、Xvfb、x11vnc 和 noVNC，只用于账号登录及网页验证。视频、字幕、封面等资源全部通过 yt-dlp 获取。

Docker 构建参数 `WITH_LOGIN_BROWSER=1` 安装浏览器依赖，运行变量 `LOGIN_BROWSER_ENABLED=1` 启用登录桌面。可用 `VIDEO_BROWSER_EXECUTABLE` 指定可执行文件；`LOGIN_DESKTOP_PORT` 配置内部桌面端口。账号登录使用既有的本机认证和实例归属校验。

故障时检查账号登录状态与容器桌面日志；下载并发不受浏览器池限制，系统没有网页下载器池。
