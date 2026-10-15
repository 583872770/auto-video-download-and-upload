# YouTube 登录被拒后的接续步骤

如果 Google 提示“此浏览器或应用可能不安全”，请停止在项目自动化窗口重试。[Google 说明](https://support.google.com/accounts/answer/7675428?hl=en)列出了自动化控制等可能被拒的浏览器情形。本项目不会通过隐藏自动化标记、关闭安全检查或修改浏览器指纹来规避它。

1. 关闭报错窗口，在你平时使用的正常 Chrome、Edge 或 Firefox 中手动登录 YouTube。保持与下载配置相同的代理网络。
2. 按 [yt-dlp 的 YouTube Cookie 导出说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)导出当前 YouTube 会话的 Netscape Cookie。若需要导出工具，官方 [FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)列出了 Chrome 的“Get cookies.txt LOCALLY”和 Firefox 的“cookies.txt”。只导出 youtube.com 当前网站，不导出所有网站。
3. 保存为本机“下载”目录下的 `youtube-cookies.txt`。不要将 Cookie 内容粘贴到聊天或提交 Git。
4. 告诉维护助手“已保存”，助手即可检查并导入本机文件，继续下载。自行运行时，将它复制为项目 `Cookies/youtube.txt`，在下载配置中设置 `youtube_cookie` 指向该文件，再执行下载命令。

Cookie 文件第一行应为 `# Netscape HTTP Cookie File` 或 `# HTTP Cookie File`。它与快手、抖音的 Playwright JSON 登录态格式不同。账号 Cookie 也不能保证所有视频都可下载；应以实际下载结果为准。

## Chrome 具体导出步骤

1. 用平时使用的 Chrome 打开 [Get cookies.txt LOCALLY 商店页面](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)，确认扩展名称，点击“添加至 Chrome”，查看权限后确认安装。
2. 在这个 Chrome 中打开 https://www.youtube.com/ 并正常登录，确认右上角显示账号头像。停留在 YouTube 标签页。
3. 点击 Chrome 右上角拼图形状的“扩展程序”按钮，再点 **Get cookies.txt LOCALLY**。扩展顶部显示的当前网址应属于 `youtube.com`。
4. 在 **Export Format** 中选择 **Netscape**，点击 **Export As**。不要点击 **Export All Cookies**，后者会导出其他网站的 Cookie。按钮名称与行为已核对[扩展源码](https://github.com/kairi003/Get-cookies.txt-LOCALLY/blob/master/src/popup.mjs)。
5. 在保存窗口选择 `C:\Users\31493\Downloads`，文件名填 `youtube-cookies.txt`，点击“保存”。如果浏览器自动下载了默认文件名，按 `Ctrl+J` 找到文件，再在资源管理器中改成上述文件名，避免重复 `.txt` 扩展名。
6. 文件留在本机，不上传到聊天。保存后告知维护助手“已保存”，即可检查格式并继续真实下载测试。

```yaml
# config.yaml 位于项目根目录时：
youtube_cookie: Cookies/youtube.txt
proxy: http://127.0.0.1:10808
```

```powershell
.\run.ps1 -Mode download -Config config.yaml -Url "https://www.youtube.com/watch?v=视频ID"
```
