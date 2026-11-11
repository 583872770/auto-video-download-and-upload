# 应用初始化拆分验证

日期：2026-11-11。本批是后端拆分的第一步。

## 职责边界

- `webapp/application.py`：运行环境、Flask 基础配置、任务存储、会话密钥、扩展装配、实例上下文、配置读取和后台组件启动。
- `webapp/extensions.py`：沿用现有素材库、锁、代理、实例、认证和执行控制对象初始化，避免重复构建。
- `app.py`：保留 `create_app` 的公开参数、业务路由、请求中间件、错误处理和命令行入口；在路由及恢复回调注册完成后按 `start_worker` 启动后台组件。
- `ApplicationRuntime` 传递本次创建的应用、配置路径及配置回调，避免使用跨实例全局应用状态。

## 保留的行为

- 模板和静态资源仍从项目根目录读取。
- 状态目录、Cookie 路径、数据库连接规则和实例注册判定不变。
- 已有 `session.key` 不覆盖，重启后浏览器会话继续有效，不同状态目录使用独立密钥。
- `settings_func` 等现有扩展键不变，配置读取仍使用实例自己的锁与文件，并合并实例资源限制。
- `start_worker=False` 不创建任务调度器和代理恢复线程。
- `webapp.extensions.init_extensions` 继续在初始化时读取，兼容现有 MySQL 测试夹具的替换方式。

## 验证结果

- 模块导入：25/25 通过，包含新模块。
- `docker/check_startup.py`：本机和 Docker 构建中均通过，覆盖嵌入式与已注册实例的启动和接口。
- 现有相关测试 98 项通过：web、bootstrap、console_auth、compression、fleet、deployment。
- 新增生命周期测试 3 项通过：重启后会话与实例隔离、配置热读取与文件隔离、后台组件开关及配置回调。
- 新增会话测试最初使用了默认 Cookie 名，修正为读取应用实际的 Cookie 名后，3 项新增测试整组复测通过。
- Python 编译与 `git diff --check` 通过。
- 本批没有重跑整个 Python 测试集，也没有验证真实 YouTube 下载或长期高并发。

## 本机部署

使用 `compose.local-mysql.yaml`、项目名 `youtube-downloader-local` 构建并更新应用容器，保留 MySQL、Redis 与全部持久卷。

重建后按现有实例身份保护等待旧心跳过期，再自动启动；无需修改实例 ID。

应用、MySQL、Redis 健康检查通过。容器内 Chromium 访问首页及部署页均返回 HTTP 200，未捕获页面脚本异常。

访问地址：<http://127.0.0.1:8000/>。本次部署目标是本机测试环境。
