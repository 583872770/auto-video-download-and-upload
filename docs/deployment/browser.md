# 浏览器环境与远程桌面

两套正式 Compose 均包含 Chromium、Xvfb、x11vnc 和 noVNC，用于 yt5s、yt1d、Bergung 等网页下载流程及账号登录窗口。新建的多类型交付任务当前使用 yt-dlp；浏览器渠道继续服务于兼容任务，下载方式说明见 [下载器](../guides/downloaders.md)。

## 部署与访问

先按 [部署手册](docker.md) 选择 `compose.local.yaml` 或 `compose.remote.yaml`，两者都设置 `WITH_YT5S_BROWSER=1`。仅重启旧容器不会安装浏览器，环境缺失时需重新构建应用镜像。

以下以本地方案为例，远程方案替换编排文件名；旧部署继续使用原项目名和环境文件。

```bash
docker compose -f compose.local.yaml exec app sh -c 'command -v chromium; printenv YT5S_BROWSER_ENABLED DISPLAY'
docker compose -f compose.local.yaml logs --tail=100 app
```

浏览器版应显示 Chromium 路径、`YT5S_BROWSER_ENABLED=1` 和 `DISPLAY=:99`。应用环境缺失时先修复部署，再恢复实例领取，不需删除数据库或状态卷。

noVNC 默认仅绑定宿主机 `127.0.0.1:6080`。通过 SSH 隧道打开：

```bash
ssh -N -L 16080:127.0.0.1:6080 用户@服务器
```

保持隧道连接，访问 `http://127.0.0.1:16080/vnc.html?autoconnect=true`。若修改 `YT5S_DESKTOP_PORT`，同时修改隧道目标端口。账号登录必须从已有账号发起，流程见 [YouTube 登录](../guides/youtube-login.md)。

## 并发与出口

网页下载器共享浏览器并发限制，解析和转换完成后的媒体传输使用现有下载进程及传输预算。浏览器并发不等于任务总并发；提高上限仍受内存、CPU、磁盘和渠道许可约束。

浏览器沿用当前任务的连接配置。人工验证、Google 拒绝浏览器、第三方网站转换失败均需查看实际页面和日志，不通过切换部署文件规避。浏览器可以打开不代表实际下载或账号验证一定成功。

## 失败诊断

yt5s 不保存失败网页截图；每个进程保留最近一次 `last-failure.json`，包含阶段、耗时和接口状态，不包含响应正文或签名链接。首个进程的记录可导出：

```bash
docker compose -f compose.local.yaml cp app:/app/.cache/web/yt5s/worker-0/last-failure.json ./yt5s-worker-0-last-failure.json
```

其他进程替换 worker 编号。浏览器桌面日志位于状态目录，启动失败可同时检查 Xvfb 和 VNC 组件日志。非 Docker 的 Linux 环境需自行安装 Chromium 并提供显示环境；Windows 本机需安装可用浏览器。
