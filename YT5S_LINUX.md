# yt5s 的 Linux 服务器验证环境

页面首页下载框上方和“下载设置”中均可选择“方式一：yt-dlp”和“方式二：yt5s（实验）”。方式选择保存后对新任务生效；现有排队任务保留创建时的方式。方式二不读取或提交用户的 YouTube 登录 Cookie，目前支持逐行输入视频 ID 或视频链接，暂不支持播放列表和字幕。

方式二使用服务器上的原生 Chromium、Xvfb 虚拟显示、noVNC 网页远程桌面。服务器不需要实体显示器。解析成功后复用现有下载进度、节点代理、去重、保存方式和素材库逻辑。浏览器解析限制为一个，已经取得链接的文件传输可以并行。

目前的实际结果：在 Windows 本机，Playwright 直接启动的 Chrome 出现验证失败；改为原生启动 Chrome 后再通过本地调试连接操作，连续三次成功解析同一个公开视频，并通过网站的 360p 链接完整下载了 25,861,548 字节。FFprobe 确认视频为 H.264、音频为 AAC、时长约 623 秒。这是小样本成功，不能推断 Linux、其他网络出口或长期批量任务一定成功。

当前工作区没有运行中的 Linux Docker 引擎，尚未在 Linux 中完成镜像构建和实际下载；本机完成了真实下载和集成验证，服务器上的可用性仍需实测。

## 启用正式 Web 服务中的方式二

已有项目的 `.env` 和数据库配置时，执行：

```bash
docker compose -f compose.yaml -f compose.yt5s.yaml up -d --build
```

连接远程 MySQL 时，使用远程数据库编排：

```bash
docker compose -f compose.remote-mysql.yaml -f compose.yt5s.yaml up -d --build
```

如果通过 PyCharm 部署，在“运行 → 编辑配置”中修改现有 Docker Compose 配置，将 Compose 文件按顺序设为 `compose.remote-mysql.yaml`、`compose.yt5s.yaml`，保留原 Docker 服务器、项目名称、环境文件及 `--build` 选项，然后重新部署。这样会在原项目中重建镜像并替换容器；仅重启旧容器不会安装浏览器。使用命令行更新已有 PyCharm 部署时，需用 `-p 原项目名称` 保持项目名一致，避免创建另一套配置和状态卷。

出现“没有找到 Chrome/Chromium”通常表示部署时遗漏了 `compose.yt5s.yaml`，或仍在使用之前的镜像。可在部署服务器检查：

```bash
docker compose -f compose.remote-mysql.yaml -f compose.yt5s.yaml exec app sh -c 'command -v chromium; printenv YT5S_BROWSER_ENABLED DISPLAY'
```

浏览器版应输出 Chromium 路径、`1` 和 `:99`。方式二在提交、导入和继续任务前检查浏览器与显示环境；后台执行时若环境缺失，会暂停下载并将当前任务放回队列。修复部署后点击“继续下载”；此前已经失败的记录需重新提交视频 ID。

打开工作台，在首页下载输入框上方选择“方式二：yt5s（实验）”，输入视频 ID 后开始下载。“下载设置”可设置方式二的 360p 或 720p 画质。360p 已验证，720p 可能需要网站转换，尚未验证完整下载。

首次启动应先测试少量视频。页面会显示等待浏览器、网站验证或网站转换的阶段；网站验证无法自动完成时，在等待期间通过下面的 SSH 隧道进入远程桌面操作。

后续重建和更新服务时也要同时传入这两份 Compose 文件，否则默认镜像不包含 Chromium 和虚拟显示环境。Windows 本机需安装 Chrome；Linux 不使用 Docker 时，需自行安装 Chromium 并提供 DISPLAY。

## 独立验证，不启动正式服务

服务器需安装 Docker 和 Docker Compose。在项目目录执行：

```bash
docker compose -f compose.yt5s-probe.yaml up --build --abort-on-container-exit --exit-code-from probe
```

默认验证视频 `npNnbj46_ik`、360p、单个阶段最多等待 180 秒、文件上限 128 MiB。浏览器可能自行完成验证；需要人工操作时，可在等待期间进入远程桌面。本工具不伪造验证令牌、不修改浏览器指纹，也不使用 YouTube 登录 Cookie。

它使用独立的 Compose 项目，不启动或挂载正式服务、MySQL、Cookie、订阅配置、任务库。输出写入 `docker-data/yt5s-probe/<本次运行目录>/`：

- `report.json`：解析响应、下载字节数、FFprobe 校验、成功或失败原因；不保存验证令牌和签名下载链接。
- `page.png`：测试结束前的网页截图。
- `<视频ID>.mp4`：完整下载并通过音视频校验后保留的视频。未完成的 `.part` 文件会清理。

完成后容器退出。查看结果：

```bash
find docker-data/yt5s-probe -name report.json -print -exec cat {} \;
docker compose -f compose.yt5s-probe.yaml down
```

## 从本机操作服务器浏览器

在自己电脑上另开终端，建立 SSH 隧道：

```bash
ssh -N -L 6080:127.0.0.1:6080 你的SSH用户@服务器地址
```

然后用本机浏览器打开：

```text
http://127.0.0.1:6080/vnc.html?autoconnect=true
```

noVNC 端口只绑定服务器的 `127.0.0.1`，没有设置远程桌面密码，应通过 SSH 隧道访问，不要将端口直接开放到公网。浏览器调试端口不对宿主机发布。浏览器运行在独立测试容器中，容器内使用 `--no-sandbox` 兼容 Chromium 启动；不要在该临时浏览器中登录账号或输入其他凭据。

## 更换视频、画质与等待时间

```bash
YT5S_VIDEO_ID=2Gu7q-KUXCk YT5S_QUALITY=360 YT5S_TIMEOUT=300 \
  docker compose -f compose.yt5s-probe.yaml up --abort-on-container-exit --exit-code-from probe
```

可选画质为 360、720。720p 可能需要网站后端转换，目前没有验证它的完整下载链路。网站没有对应画质时，工具报告失败，不会悄悄降级。某些视频可能没有音频或受到网站限制，也会校验失败。

独立实验可设置 `YT5S_PROXY=http://可访问的代理地址:端口`，浏览器与文件下载使用同一个代理。方式二目前仅支持没有账号密码的 HTTP(S) 代理。容器中的 `127.0.0.1` 指向本容器，不能用它直接访问宿主机的 Clash。正式 Web 服务中的方式二会使用任务原有的 Clash 节点和轮换设置；独立实验不加载项目的订阅与节点配置。

## 部署验证范围

先在目标服务器测试少量不同视频，分别验证实际使用的网络出口，并区分自动通过和人工操作后的成功。方式二的网站验证失败按普通任务失败处理，不会误报 YouTube Cookie 失效或要求上传 Cookie。当前没有独立的来源熔断与全队列验证暂停机制。

该方法没有百万级吞吐保证。第三方网站可能改变验证、接口、画质或限流规则；浏览器环境只提供正常浏览器执行条件，不能保证持续自动通过。

参考：

- [Cloudflare：自动化测试与 Turnstile](https://developers.cloudflare.com/turnstile/troubleshooting/testing/)
- [Cloudflare：令牌验证、有效期与单次使用](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/)
