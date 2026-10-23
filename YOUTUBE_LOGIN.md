# YouTube 登录与 Cookie 配置

部分请求需要登录态才能下载。下载器通过 `youtube_cookie` 读取 Netscape 格式的 Cookie 文件；这与抖音、快手使用的 JSON 登录态不同。

## 导出 Cookie

1. 在平时使用的 Chrome 中安装 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)。
2. 为避免 YouTube 在浏览器后台轮换 Cookie，按 yt-dlp 官方建议打开一个单独的无痕窗口（需允许该扩展在无痕模式使用），在其中打开 [YouTube](https://www.youtube.com/) 并正常登录。使用与下载器相同的代理节点，确认目标视频可以播放；若提示验证，先在浏览器完成验证。
3. 在同一个标签页访问 `https://www.youtube.com/robots.txt`，点击浏览器右上角“扩展程序”，打开 **Get cookies.txt LOCALLY**，确认导出范围属于 `youtube.com`。
4. 将 **Export Format** 设为 **Netscape**，点击 **Export As**。不要选择 **Export All Cookies**，以免包含其他网站的凭据。
5. 导出后立即关闭整个无痕窗口，不再用该会话继续浏览。将文件在 Web 页面“上传 Cookie 文件”中上传，或保存为项目目录下的 `Cookies/youtube.txt`。

其他浏览器及导出方式见 [yt-dlp Cookie FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) 和 [YouTube Cookie 导出说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)。

## 配置与下载

使用 Web 工作台时，在左侧 YouTube 或首页下载区点击 **上传 Cookie 文件**，选择导出的 `.txt` 后点击 **上传并保存**。程序会校验文件并自动保存到配置目录下的 `Cookies/youtube.txt`，同时更新下载配置，无需手动填写路径。设置页也提供相同入口。上传文件最大 1 MB，已有 Cookie 将被替换；格式错误或没有未过期 YouTube 记录时保留已有文件。

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

也可运行以下命令，在项目打开的独立浏览器中手动登录：

```powershell
.\run.ps1 -Mode youtube-login
```

程序确认登录成功后，仅导出该会话的 YouTube Cookie 到项目 `Cookies/youtube.txt`。下载时仍需在配置中设置 `youtube_cookie`。如果 Google 提示“此浏览器或应用可能不安全”，请改用正常浏览器手动导出；相关原因见 [Google 登录帮助](https://support.google.com/accounts/answer/7675428?hl=en)。

## 注意事项

- Cookie 是登录凭据，请保留在本机，不要提交到 Git、上传到聊天或发送给他人。项目已忽略 `Cookies/` 目录。
- Netscape 文件通常以 `# Netscape HTTP Cookie File` 或 `# HTTP Cookie File` 开头，不应使用 JSON 文件代替。
- Cookie 可能过期或被网站更新。下载再次要求登录时，可重新导出；账号登录态不保证所有视频都可下载。
- “Cookie 已保存”仅代表文件已经保存，不代表 YouTube 接受该登录态。任务提示“Cookie 已失效或被轮换”时需要重新导出；提示“要求登录验证”时还应检查所选代理节点。代理延迟正常或连通性测试通过，也不代表视频下载能够通过 YouTube 的验证。
- 下载期间使用 Cookie 文件的临时副本，yt-dlp 收到的 Cookie 更新不会覆盖上传的原文件。
