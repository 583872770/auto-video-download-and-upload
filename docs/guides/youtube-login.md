# YouTube 登录与 Cookie 配置

部分请求需要登录态才能下载。下载器通过 `youtube_cookie` 读取 Netscape 格式的 Cookie 文件；这与抖音、快手使用的 JSON 登录态不同。

## 导出 Cookie

1. 在平时使用的 Chrome 中安装 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)。
2. 为避免 YouTube 在浏览器后台轮换 Cookie，按 yt-dlp 官方建议打开一个单独的无痕窗口（需允许该扩展在无痕模式使用），在其中打开 [YouTube](https://www.youtube.com/) 并正常登录。使用与下载器相同的代理节点，确认目标视频可以播放；若提示验证，先在浏览器完成验证。
3. 在同一个标签页访问 `https://www.youtube.com/robots.txt`，点击浏览器右上角“扩展程序”，打开 **Get cookies.txt LOCALLY**，确认导出范围属于 `youtube.com`。
4. 将 **Export Format** 设为 **Netscape**，点击 **Export As**。不要选择 **Export All Cookies**，以免包含其他网站的凭据。
5. 导出后立即关闭整个无痕窗口，不再用该会话继续浏览。在“账号管理”选择已新增或导入的账号，进入详情的“Cookie 与登录”，点击“手工更新 Cookie”。账号需先绑定下载实例。

其他浏览器及导出方式见 [yt-dlp Cookie FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) 和 [YouTube Cookie 导出说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)。

## 从账号开始登录

1. 在“账号管理”新增或导入账号。
2. 点击该账号的“登录并绑定”，选择所属下载实例，再选择自动填写或手工登录。
3. 在所属实例的独立浏览器中完成登录和必要验证。只有登录邮箱与所选账号一致时才保存 Cookie。
4. 每个账号只保留一份 Cookie。再次登录成功会替换原内容，沿用原 Cookie 标识；失败、取消或资料发生冲突时保留旧凭据。
5. 首次登录后可将该账号加入所属实例的账号池；重新登录保留账号池选择、限流冷却和运行记录。

登录期间可以继续下载。已开始的下载使用私有凭据副本，新任务读取更新后的内容。登录更新会使该账号的登录检测缓存失效，不会清除平台限流冷却。

Web 页面不再提供匿名浏览器登录、独立 Cookie 上传或多份 Cookie 关联。把文件放进 `Cookies` 目录不会自动导入账号库。手工导出的文件也应从指定账号详情更新，并确认其属于该邮箱账号。

账号检测和下载限流分别显示：登录有效不保证每个视频都可访问，也不代表账号不受限流。账号冷却期间不发额外登录检测请求；冷却到期后按恢复策略调度。详细数据升级和清理规则见 [账号管理](accounts.md)。

### Docker 服务器查看登录窗口

容器中的浏览器显示在服务器的 Xvfb 虚拟桌面，不会在访问管理页面的电脑上弹窗。先通过 SSH 转发远程桌面端口（如部署修改过 `YT5S_DESKTOP_PORT`，替换右侧端口）：

```bash
ssh -N -L 6080:127.0.0.1:6080 你的SSH用户@服务器地址
```

保持 SSH 连接，在本机浏览器打开 `http://127.0.0.1:6080/vnc.html?autoconnect=true`，再在管理页面点击 **打开登录窗口**，到远程桌面完成登录。远程桌面端口保持仅绑定服务器本机，通过 SSH 隧道访问。

登录流程会优先使用 `browser_executable` 或容器桌面设置的 `YT5S_BROWSER`，也会查找系统 Chromium。若提示缺少浏览器，需在服务器项目目录更新代码，使用原部署的 Compose 文件及项目名称重新构建并创建容器，例如：

```bash
docker compose -p 原项目名称 -f compose.remote.yaml up -d --build app
```

自带 MySQL 的部署使用 `compose.local.yaml`。仅重启旧容器不会应用代码或安装 Chromium。可以用同一 Compose 项目检查容器环境：

```bash
docker compose -p 原项目名称 -f compose.remote.yaml exec app sh -c 'command -v chromium; printenv YT5S_BROWSER DISPLAY; pgrep -a Xvfb'
```

内置浏览器镜像正常应显示 Chromium 路径、`DISPLAY=:99` 和运行中的 Xvfb。若浏览器能打开但提示代理或网络错误，检查当前下载连接和所选节点。

也可运行以下命令，在项目打开的独立浏览器中手动登录：

```powershell
.\run.ps1 -Mode youtube-login
```

此命令行工具仅导出该会话的 Cookie 文件，不会自动创建 Web 账号或导入账号库；请先在 Web 新增账号，再从账号详情手工更新凭据。如果 Google 提示“此浏览器或应用可能不安全”，请改用正常浏览器手动导出；相关原因见 [Google 登录帮助](https://support.google.com/accounts/answer/7675428?hl=en)。

## 注意事项

- Cookie 是登录凭据，请保留在本机，不要提交到 Git、上传到聊天或发送给他人。项目已忽略 `Cookies/` 目录。
- Netscape 文件通常以 `# Netscape HTTP Cookie File` 或 `# HTTP Cookie File` 开头，不应使用 JSON 文件代替。
- Cookie 可能过期或被网站更新。下载再次要求登录时，可重新导出；账号登录态不保证所有视频都可下载。
- “Cookie 已保存”仅代表文件已经保存，页面显示“YouTube 登录已验证”才表示最近一次服务端登录检测通过。任务提示“Cookie 已失效或被轮换”时需要重新导出；提示“要求登录验证”时还应检查所选代理节点。代理延迟正常或连通性测试通过，也不代表视频下载能够通过 YouTube 的验证。
- 下载期间使用 Cookie 文件的临时副本，yt-dlp 收到的 Cookie 更新不会覆盖上传的原文件。
