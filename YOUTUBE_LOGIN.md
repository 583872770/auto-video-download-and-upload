# YouTube 登录与 Cookie 配置

部分请求需要登录态才能下载。下载器通过 `youtube_cookie` 读取 Netscape 格式的 Cookie 文件；这与抖音、快手使用的 JSON 登录态不同。

## 导出 Cookie

1. 在平时使用的 Chrome 中安装 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)。
2. 打开 [YouTube](https://www.youtube.com/)，正常登录并停留在该网站标签页。
3. 点击浏览器右上角“扩展程序”，打开 **Get cookies.txt LOCALLY**，确认当前网址属于 `youtube.com`。
4. 将 **Export Format** 设为 **Netscape**，点击 **Export As**。不要选择 **Export All Cookies**，以免包含其他网站的凭据。
5. 将文件保存为项目目录下的 `Cookies/youtube.txt`。

其他浏览器及导出方式见 [yt-dlp Cookie FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) 和 [YouTube Cookie 导出说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)。

## 配置与下载

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
