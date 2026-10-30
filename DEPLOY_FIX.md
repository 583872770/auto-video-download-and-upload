# Docker 启动模块导入失败修复

更新日期：2026-10-30

日志在 `app.py:81` 报 `ModuleNotFoundError` 时，数据库已经就绪，失败发生在应用扩展初始化阶段。

## 原因

此前重构增加了与入口 `app.py` 同名的 `app/` 包，但 Dockerfile 没有复制该目录。仅把目录加入 `.dockerignore` 白名单不能解决问题：构建上下文中的文件仍需由 Dockerfile 复制进镜像。直接补复制还会导致 Python 优先导入 `app/__init__.py`，使 `from app import create_app` 失败。

## 修复

- 将扩展、辅助函数和蓝图移入已有的 `webapp/` 包，由现有 Dockerfile 的 COPY 指令完整打包，消除同名冲突。
- 恢复素材库和下载启动控制器的局部绑定、请求参数函数名称、嵌入式应用的实例注册条件，以及初始化和后台回调所需的 Flask 应用上下文。
- 保留 `/api/disk` 路径，并让健康检查返回正确的服务角色。
- 补回任务存储中使用的 `hashlib` 导入。
- 在镜像构建阶段执行 `docker/check_startup.py`，使用临时 SQLite 和配置完成真实应用初始化及关键接口检查，不需要 MySQL，也不启动下载进程。
- 启动诊断增加缺失模块名与重建镜像提示。

## 重新部署

在部署服务器更新到包含本次修复的源码后执行：

```bash
docker compose -f compose.remote-mysql.yaml up -d --build app
docker compose -f compose.remote-mysql.yaml logs -f app
```

必须重建镜像并重建容器，仅重启旧容器不会更新程序。此操作沿用现有配置卷、状态卷和数据库，不需要删除卷或修改密码、实例 ID。

构建日志应包含 `Packaged application startup verified.`；运行日志应最终显示 `初始化完成，服务已就绪。`。

## 本地验证

```bash
python scripts/test_imports.py
python docker/check_startup.py
python -m unittest tests.test_docker tests.test_deployment tests.test_web tests.test_disk_budget tests.test_fleet -q
```

`tests.test_docker` 会按 Dockerfile 的 COPY 指令在临时目录中复制应用，验证脱离宿主源码目录后仍可启动。

## 本次验证结果

- 模块导入检查：24/24 通过。
- 启动和部署测试：46 项全部通过，包括独立打包目录中的应用初始化、素材库、状态、磁盘、下载启动状态和配置保存接口。
- 扩展回归：123 项中 111 项通过、2 项跳过、9 项失败、1 项错误。未通过项涉及路径状态码、任务恢复、统计登记和跨实例鉴权，尚未解决；启动修复的验证不代表完整业务回归通过。详细日志见本地 `.cache/startup-regression.log`。
- 本机 Docker 引擎未运行，未执行真实镜像构建或服务器部署；已通过按 Dockerfile COPY 规则生成的独立目录验证。
