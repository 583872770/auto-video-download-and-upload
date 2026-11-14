# 项目结构与维护边界

更新时间：2026-11-14。命令从项目根目录运行，文档统一从 [文档导航](../README.md) 查阅。

## 开发测试阶段约定

当前以最新数据结构和流程为准，不为历史测试数据新增兼容分支、旧字段回退或自动补偿。废弃功能应一并移除生产入口、执行逻辑、页面展示和专属测试。结构更新仍需可重复执行；清理兼容代码不包含清空实际数据库或文件。

已移除旧版删除任务的队列计数补偿、独立字幕补下载任务及专用进程、单例会话统计回退、ISO 字符串统计时间回退和根目录任务日志回退。下载完成时间与素材下载时间的结构更新不再回填旧记录。字幕统一使用当前交付流程；任务删除校验、租约、重试和字幕限流继续生效。

## 入口与模块

| 路径 | 职责 |
| --- | --- |
| `app.py` | Flask 工厂与 Web / CLI 兼容入口，继续导出 `create_app` |
| `web_worker.py` | 下载工作进程入口 |
| `webapp/application.py`、`middleware.py` | 应用装配、启动顺序、认证及请求处理 |
| `webapp/routes/` | HTTP 路由与 Blueprint |
| `webapp/services/` | 任务、配置、账号及工作台业务编排 |
| `webapp/repositories/` | TaskStore 数据访问与工作台快照 |
| `webapp/worker/` | 执行、工作目录、续租、结算与恢复 |
| `webapp/adapters/` | 下载器进度、渠道执行与实例 RPC |
| `webapp/runtime/` | 后台组件监督 |
| `webapp/config/` | 参数定义、默认值、校验和保存 |
| `webapp/delivery/` | 交付要求、产物执行、校验、存储与 API |
| `Utils/` | 兼容工具及下载实现 |
| `frontend/` | Vue 3 + TypeScript + Vue Router + Vite + Pinia + Element Plus；设置、任务中心、工作台、素材、日志及交付文件 |
| `templates/`、`static/` | Flask 模板、兼容页面与打包后的静态资源 |
| `docker/` | 镜像启动、浏览器桌面、健康检查与打包验证 |
| `scripts/verify/` | 可重复执行的验证脚本，`scripts/verify_*.py` 为原路径兼容入口 |
| `tests/`、`.github/workflows/` | 自动化测试与 CI |

既有独立领域模块继续负责账号池、调度、租约和资源保护等逻辑；不为目录外观再次移动运行模块。`webapp/store.py`、`tasks.py`、`settings.py`、`config_schema.py` 继续保留公开兼容接口。

## 前端开发

Vue 工作区统一使用 `components/WorkspaceShell.vue` 和 `router.ts`，覆盖系统设置 `/settings`、任务中心 `/jobs`、任务详情 `/jobs/<任务编号>`、下载工作台 `/workbench`、素材库 `/media`、执行日志 `/logs`、交付文件 `/delivery-files`、账号 `/accounts`、实例 `/instances`、统计账单 `/statistics`、代理 `/proxy` 及部署 `/deployment`。登录与管理账户 `/login`、独立部署恢复 `/setup` 使用独立布局。旧首页哈希自动进入对应页面，旧 `/jobs#<任务编号>` 自动定位到详情，保留 `?tab=settings` 等参数。页面按路由分包加载。

系统设置组件按文件保存、下载、并发渠道、空间日志、诊断和交付默认划分；任务组件按列表、详情、表单、视频清单、导入及确认操作划分。交付要求编辑器由任务表单与全局默认共用，字幕和媒体预览、执行日志、对话框及离页草稿保护复用公共组件。任务的创建请求编号在失败重试时保持不变；编辑继续携带 `edit_version`，冲突时保留输入，明确核对后才采用新版本。

请求层集中处理 CSRF、权限过期、超时及实例切换。系统设置草稿由 Pinia 管理，交付默认是全局独立草稿；客户任务和交付清单请求不携带实例选择头。列表查询取消旧请求并忽略过期返回，轮询在隐藏页面、编辑或组件卸载时暂停。上传分步提交，粘贴已成功时只重试未成功的文件上传。

工作台和素材操作绑定当前实例；工作台进度与容量各自轮询，容量请求缓慢不会阻塞执行状态。选择任务只预览，启停后重新读取状态，暂停领取和立即停止分别处理。素材清空需核对实例、当前目录并输入确认文字。执行记录列表保持全局汇总，日志按任务来源实例读取，增量追加并限制浏览器保留量；关闭播放器会释放媒体连接。

账号清单与统计账本请求不携带实例头；账号浏览器登录和检测由后端按所属实例转发，取消登录携带会话 ID。密钥按需显示，导入必须使用服务端签名预览；编辑发生版本冲突时保留输入。账号池成员和节奏分别提交与读回，不覆盖另一份草稿。代理、部署与 Cookie 文件导出携带实例范围；切换后忽略旧响应，保存后核对状态。金额使用服务端字符串，字节展示通过 BigInt 保留精度。实例名称与全局并发草稿不被状态轮询覆盖。

`/setup` 由部署网关独立提供，不依赖业务数据库或管理账户登录；使用本机独立访问码。恢复页只保存实例设置，MySQL / Redis 连接仍来自启动配置。管理端 `/deployment` 可以配置所选实例的连接，密码不回显，保存后等待重启。构建产物使用相对资源路径，同时适配 `/static/frontend/` 与 `/setup-static/frontend/`。未构建 Vue 资源时保留原页面回退，因此原模板与脚本继续保留。

需要 Node.js 22.12+。首次启动前从根目录执行：

```powershell
python scripts/generate_frontend_contract.py --check
cd frontend
npm.cmd ci --cache ../.cache/npm
npm.cmd run build
cd ..
```

然后启动 Flask。开发时在 `frontend/` 运行 `npm.cmd run dev`，监听源码并重新构建，浏览器刷新对应页面查看；不需要额外的 Node 服务或跨域代理。Linux 可使用 `npm`。

`frontend/src/generated/config.ts` 由后端参数定义生成。修改 `webapp/config/schema.py` 后运行 `python scripts/generate_frontend_contract.py` 并提交生成文件；CI 使用 `--check` 防止前后端字段漂移。运行 `npm.cmd test` 检查请求与保存合同，`npm.cmd run build` 包含严格类型检查；`npm.cmd run format` 统一代码格式，CI 使用 `format:check` 检查。浏览器回归入口为 `python scripts/verify/verify_frontend_settings_ui.py`。生产资源输出到 `static/frontend/`，不提交 Git，由 Docker 构建阶段生成。依赖版本由 `package-lock.json` 固定，`.npmrc` 兼容 npm 10 对 Vitest 可选浏览器依赖的解析问题。

## 部署与文档目录

```text
项目根目录/
├─ README.md                     项目介绍与最短启动路径
├─ Dockerfile、.dockerignore      镜像构建
├─ compose.local.yaml            应用 + MySQL + Redis
├─ compose.remote.yaml           应用连接远程 MySQL、Redis
├─ .env.local.example            本地一起部署的环境模板
├─ .env.remote.example           远程服务环境模板
├─ docs/
│  ├─ README.md                  当前文档导航与维护规则
│  ├─ deployment/                部署、迁移和浏览器环境
│  ├─ guides/                    页面与功能使用
│  ├─ operations/                调度、账号池、资源和恢复
│  ├─ development/               实现、配置与验证
│  └─ archive/                   少量近期发布、回滚与检查记录
└─ scripts/verify/README.md       验证脚本约定
```

当前说明采用小写连字符文件名，近期发布记录使用日期前缀；已由真实页面取代的原型不再保留。

## 持久化与变更验证

`.env`、根配置、`.cache/web`、视频、账号凭据、数据库和部署密钥属于实际配置或运行状态，不随代码和文档整理移动、覆盖或清理。Docker 配置卷、状态卷、MySQL 卷及下载绑定目录保留现有部署的项目名和路径。

后端变更按影响运行模块导入、`docker/check_startup.py` 和相应业务回归；前端变更检查 JavaScript 与页面行为；部署变更检查 Compose 解析及打包。单纯文档移动检查本地链接、标题锚点、原型路径和仓库引用，无需启动下载或连接生产服务。

前端浏览器回归使用隔离 SQLite 和临时目录：`python scripts/verify/verify_jobs.py` 检查任务生命周期、账单、冲突和权限；`python scripts/verify/verify_frontend_workspace_ui.py` 检查路由、未确认写入重试、部分导入失败、文件分页、字幕预览和手机布局；`python scripts/verify/verify_frontend_execution_ui.py` 检查两个实例的工作台启停、过期返回、慢容量请求、日志来源、素材分页、预览及操作确认；`python scripts/verify/verify_frontend_settings_ui.py` 检查设置保存与实例隔离；`python scripts/verify/verify_frontend_management_ui.py` 检查账号冲突、导入预览、密钥、Cookie 导出、账号池与节奏独立保存、代理、实例、账单、部署和独立恢复页。无需启动真实下载或连接业务数据库。原页面回归通过 `python scripts/verify/verify_source_ui.py scripts/verify/verify_<suite>.py` 显式选择未构建回退模式，独立于构建产物存在与否。

模块拆分完成后直接维护本手册，不再保留逐批迁移计划和重复验收报告。
