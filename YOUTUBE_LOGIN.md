# YouTube 登录与 Cookie 配置

部分请求需要登录态才能下载。下载器通过 `youtube_cookie` 读取 Netscape 格式的 Cookie 文件；这与抖音、快手使用的 JSON 登录态不同。

## 导出 Cookie

1. 在平时使用的 Chrome 中安装 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)。
2. 为避免 YouTube 在浏览器后台轮换 Cookie，按 yt-dlp 官方建议打开一个单独的无痕窗口（需允许该扩展在无痕模式使用），在其中打开 [YouTube](https://www.youtube.com/) 并正常登录。使用与下载器相同的代理节点，确认目标视频可以播放；若提示验证，先在浏览器完成验证。
3. 在同一个标签页访问 `https://www.youtube.com/robots.txt`，点击浏览器右上角“扩展程序”，打开 **Get cookies.txt LOCALLY**，确认导出范围属于 `youtube.com`。
4. 将 **Export Format** 设为 **Netscape**，点击 **Export As**。不要选择 **Export All Cookies**，以免包含其他网站的凭据。
5. 导出后立即关闭整个无痕窗口，不再用该会话继续浏览。将文件在 Web 页面“上传 Cookie 文件”中上传，或保存为项目目录下的 `Cookies/youtube.txt`。

其他浏览器及导出方式见 [yt-dlp Cookie FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) 和 [YouTube Cookie 导出说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)。

## 下载前登录检测

Web 工作台提交 YouTube 下载、点击“继续下载”时先检测登录；后台任务在真正调用下载器前也会检查，因此 TXT/CSV 导入和服务重启后的排队任务同样受保护。首页 YouTube 登录区可点击 **检测登录** 手动检查。

检查使用当前下载代理及实际节点访问 YouTube，必须得到服务端明确的登录标志才算通过。文件存在、Cookie 格式正确或包含 SID 均不能单独证明已登录。缺少 Cookie、过期、未登录、需要额外验证或网络不通时会停止队列，原任务保留在排队状态，不记失败、不消耗重试次数；页面区分登录问题和网络/节点问题。

为避免百万级任务重复请求，成功结果按 Cookie 内容、代理和节点共享缓存，最长 5 分钟，且不超过登录 Cookie 的有效期。更新 Cookie、改变连接或切换节点后重新验证；“检测登录”和“继续下载”会主动请求最新状态。下载过程中如 YouTube 报告 Cookie 失效或要求机器人验证，同样停止队列并保留当前任务。

检测通过表示当前登录有效，不保证所有视频可访问。登录或重新上传 Cookie 后，点击“检测登录”确认，再按需继续下载；任务不会自动恢复。普通 HTTP 文件下载不要求 YouTube 登录。

## 配置与下载

使用 Web 工作台时，在左侧 YouTube 或首页下载区点击 **上传 Cookie 文件**，选择导出的 `.txt` 后点击 **上传并保存**。程序会校验文件并自动保存到配置目录下的 `Cookies/youtube.txt`，同时更新下载配置，无需手动填写路径。设置页也提供相同入口。上传文件最大 1 MB；请先停止下载并等待当前任务结束，已停止的排队任务可以保留。已有 Cookie 将被替换；格式错误或没有未过期 YouTube 记录时保留已有文件。

在项目根目录的 `config.yaml` 中设置：

```yaml
youtube_cookie: Cookies/youtube.txt
# 按需启用，端口替换为自己的代理端口：
# proxy: http://127.0.0.1:7890
```

然后运行：

```powershell
.\run.ps1 -Mode download -Url "https://www.youtube.com/watch?v=视频ID"
```

使用其他位置的配置文件时，Cookie 的相对路径以配置文件所在目录为基准。

## 独立浏览器登录

Web 页面首页顶部、下载区和设置页均提供 **浏览器登录** 入口，左侧 YouTube 也可进入：

1. 如果有下载任务，先点击 **停止全部下载** 并等待当前下载结束。排队任务会保留，无需删除。
2. 打开 **浏览器登录** 面板，点击 **打开登录窗口**。独立浏览器会在运行 Flask 服务的电脑上打开；远程访问页面时，窗口仍在服务器电脑上。
3. 浏览器使用当前下载连接方式：直连、手动代理或 Clash 当前所选节点。请在 10 分钟内手动登录 YouTube；网页会显示登录状态，可以取消、重试，刷新页面也可继续查看。
4. 检测到 YouTube 登录成功后，程序访问 `youtube.com/robots.txt`，仅导出该独立会话的 YouTube Cookie，并关闭浏览器。Cookie 自动保存到配置目录的 `Cookies/youtube.txt`，同时更新 `youtube_cookie`。
5. 登录成功后按需点击 **继续下载**。已有排队任务在开始执行时也会使用新的 Cookie，不必重新导入；程序不会自动恢复队列。

登录期间暂时不能恢复下载、创建下载任务、替换 Cookie 或修改代理节点。取消、超时、关闭浏览器或登录失败时保留原有 Cookie；Google 拒绝登录时仍可使用 **上传 Cookie 文件**。这里确认的是浏览器登录成功，具体视频能否下载仍以下载结果为准。

也可运行以下命令，在项目打开的独立浏览器中手动登录：

```powershell
.\run.ps1 -Mode youtube-login
```

程序确认登录成功后，仅导出该会话的 YouTube Cookie 到项目 `Cookies/youtube.txt`。下载时仍需在配置中设置 `youtube_cookie`。如果 Google 提示“此浏览器或应用可能不安全”，请改用正常浏览器手动导出；相关原因见 [Google 登录帮助](https://support.google.com/accounts/answer/7675428?hl=en)。

## 注意事项

- Cookie 是登录凭据，请保留在本机，不要提交到 Git、上传到聊天或发送给他人。项目已忽略 `Cookies/` 目录。
- Netscape 文件通常以 `# Netscape HTTP Cookie File` 或 `# HTTP Cookie File` 开头，不应使用 JSON 文件代替。
- Cookie 可能过期或被网站更新。下载再次要求登录时，可重新导出；账号登录态不保证所有视频都可下载。
- “Cookie 已保存”仅代表文件已经保存，页面显示“YouTube 登录已验证”才表示最近一次服务端登录检测通过。任务提示“Cookie 已失效或被轮换”时需要重新导出；提示“要求登录验证”时还应检查所选代理节点。代理延迟正常或连通性测试通过，也不代表视频下载能够通过 YouTube 的验证。
- 下载期间使用 Cookie 文件的临时副本，yt-dlp 收到的 Cookie 更新不会覆盖上传的原文件。
