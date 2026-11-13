# 目录整理后的回归修复与本地验证

日期：2026-11-11。部署目标为本机 Docker Desktop，Compose 项目为 `youtube-downloader-local`。

## 修复内容

- `web_worker.py`：将 `incomplete_response` 纳入已有的空响应重试等待，按尝试次数使用 60、120、240 秒，最长 300 秒；仍受任务最大尝试次数限制。这是任务重试等待，不会增加 Cookie 冷却。
- `webapp/youtube_auth.py`：启动检测刚得到的 `not_logged_in`、`invalid`、`verification_required` 不再被账号池汇总的 `waiting` 覆盖。健康成员仍可继续检测，HTTP 429 和冷却仍保留等待语义。
- 本机 `.venv` 按 `requirements.lock.txt` 补齐依赖，消除 `redis`、`oss2` 缺失；未修改依赖锁文件。
- `tests/test_polling.cjs`：将双接口独立刷新场景放到任务列表，匹配工作台已使用精简接口的实现；补齐 VM 中的 `URLSearchParams`。
- `tests/test_storage.py`：并行临时下载测试通过配置接口保存测试水位，避免提交任务时配置同步覆盖仅写入运行时的设置。生产磁盘阈值未调整。

## 测试结果与范围

| 检查 | 结果 |
| --- | --- |
| 不完整响应的指数退避与尝试次数限制 | 通过 |
| 账号池启动测试 | 11 项通过 |
| diagnostics、pool_startup、oss_storage、oss_tuning 相关测试组 | 43 项通过 |
| Python 完整回归 | 执行 1147 项，30 项跳过；仅并行存储测试因宿主机空间触发保护而失败 |
| 上述存储失败项修复后单独复测 | 通过 |
| storage 测试组复测 | 8 项，7 项通过、1 项因 Windows 符号链接权限跳过 |
| Node 轮询专项测试 | 通过 |
| pip check、git diff --check | 通过 |

完整回归已确认原有 3 个失败项和 3 个依赖导入错误消除。完整回归后仅修改了测试夹具，随后复测对应测试组；没有再次执行整套 Python 回归。部分集成测试需要额外测试环境，本次跳过项不应记作验证通过。

## 本地部署

```powershell
docker compose -f compose.local.yaml -p youtube-downloader-local up -d --build app
```

- 镜像构建中的 `docker/check_startup.py` 通过。
- 应用、MySQL、Redis 均为 healthy；应用日志确认初始化完成。
- 原容器心跳在重建后短暂触发实例身份等待，心跳过期后自动恢复。
- 容器内 Chromium 打开首页及部署页，均返回 HTTP 200，未捕获页面脚本异常。
- 首页：http://127.0.0.1:8000/；部署设置：http://127.0.0.1:8000/setup。

本次没有执行洛杉矶服务器发布，也没有验证真实 YouTube 账号下载或长期高并发运行。
