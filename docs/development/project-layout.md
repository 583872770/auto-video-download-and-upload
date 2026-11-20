# 项目结构与维护边界

更新时间：2026-11-19。命令从项目根目录运行，文档统一从 [文档导航](../README.md) 查阅。

## 开发测试阶段约定

当前以最新数据结构和流程为准，不为历史测试数据新增兼容分支、旧字段回退或自动补偿。废弃功能应一并移除生产入口、执行逻辑、页面展示和专属测试。结构更新仍需可重复执行；清理兼容代码不包含清空实际数据库或文件。

已移除旧版删除任务的队列计数补偿、独立字幕补下载任务及专用进程、单例会话统计回退、ISO 字符串统计时间回退和根目录任务日志回退。下载完成时间与素材下载时间的结构更新不再回填旧记录。字幕统一使用当前交付流程；任务删除校验、租约、重试和字幕限流继续生效。

## 入口与模块

| 路径 | 职责 |
| --- | --- |
| `app.py` | Flask 工厂与 Web / CLI 入口，导出 `create_app` |
| `web_worker.py` | 下载工作进程入口 |
| `webapp/application.py`、`middleware.py` | 应用装配、启动顺序、认证及请求处理 |
| `webapp/routes/` | HTTP 路由与 Blueprint |
| `webapp/services/` | 任务、配置、账号及工作台业务编排 |
| `webapp/repositories/` | TaskStore 数据访问与工作台快照 |
| `webapp/worker/` | 执行、工作目录、续租、结算与恢复 |
| `webapp/adapters/` | 实例 RPC |
| `webapp/runtime/` | 后台组件监督 |
| `webapp/config/` | 参数定义、默认值、校验和保存 |
| `webapp/delivery/` | 交付要求、产物执行、校验、存储与 API |
| `Utils/` | 路径、配置、存储及工具函数 |
| `frontend/` | Vue 3 + TypeScript + Vue Router + Vite + Pinia + Element Plus；设置、任务中心、工作台、日志及交付文件 |
| `templates/frontend.html` | 当前 Vue 页面入口模板，加载构建资源、初始化信息并提供 `#frontend-app` 挂载位置 |
| `static/` | 静态资源；其中 `static/frontend/` 为 Vue 构建产物 |
| `docker/` | 镜像启动、浏览器桌面、健康检查与打包验证 |
| `scripts/verify/` | 可重复执行的验证脚本 |
| `tests/`、`.github/workflows/` | 自动化测试与 CI |

独立领域模块负责账号池、请求调度、租约和资源保护。数据访问使用 `webapp/store.py`，进程监督使用 `webapp/runtime/supervisor.py`，配置直接引用 `webapp/config/`；不保留 `tasks.py`、`settings.py`、`config_schema.py` 转发层。下载仅支持 yt-dlp，必须经账号池领取账号；单个账号也可组成账号池。

根目录 `app.py` 是有效入口，业务包名为 `webapp`，不再使用根目录 `app/` 包。容器内的 `/app` 是安装路径，与已移除的本地 `app/` 残留目录不同。

## 本地目录与清理范围

| 路径 | 用途与处理方式 |
| --- | --- |
| 源码目录下的 `__pycache__/` | Python 自动生成的字节码缓存，可删除，后续运行会重新生成；Git 和 Docker 已忽略 |
| `static/frontend/` | 前端构建产物，可通过构建重新生成；本机运行页面时需要，清理后必须重新构建 |
| `.venv/`、`.tools/`、`frontend/node_modules/` | 开发依赖与工具，不作为普通缓存批量删除 |
| `.cache/` | 包含实际运行状态和开发缓存，不能整个目录一键清空 |
| `.runtime/` | 本地部署包、诊断结果及验证材料，按具体发布和回滚用途检查后清理 |
| `output/design/` | 界面方案与验证截图，仅供参考，不参与构建或运行；当前行为以现行手册及代码为准 |

整理时只清理已核对的源码缓存与废弃空目录，不移动源码入口或实际配置。依赖目录、运行状态和发布备份不与源码缓存一起递归清理。整理缓存后无需重新部署服务器。

## 前端开发

Vue 工作区统一使用 `components/WorkspaceShell.vue` 和 `router.ts`，覆盖实例设置 `/settings`、任务中心 `/jobs`、任务详情 `/jobs/<任务编号>`、下载工作台 `/workbench`、执行日志 `/logs`、账号 `/accounts`、实例 `/instances`、统计账单 `/statistics`、代理 `/proxy` 及部署 `/deployment`。文件查看并入任务中心，独立素材库前端页面及专属组件已移除。登录与管理账户 `/login`、独立部署恢复 `/setup` 使用独立布局。任务详情使用路径参数，页签使用 `?tab=settings` 等参数。页面按路由分包加载。

实例设置组件按下载、并发保护、空间日志和诊断划分；任务组件按列表、详情、表单、视频清单、导入及确认操作划分。交付要求与文件保存由任务表单维护，字幕和媒体预览、执行日志、对话框及离页草稿保护复用公共组件。任务的创建请求编号在失败重试时保持不变；编辑继续携带 `edit_version`，冲突时保留输入，明确核对后才采用新版本。

请求层集中处理 CSRF、权限过期、超时及实例切换。实例设置草稿由 Pinia 管理，任务表单独立管理交付与保存草稿；客户任务和交付清单请求不携带实例选择头。列表查询取消旧请求并忽略过期返回，轮询在隐藏页面、编辑或组件卸载时暂停。上传分步提交，粘贴已成功时只重试未成功的文件上传。

工作台操作绑定当前实例；工作台进度与容量各自轮询，容量请求缓慢不会阻塞执行状态。选择任务只预览，启停后重新读取状态，暂停领取和立即停止分别处理。测试样本和正式文件在任务中心跨实例查看，预览和清理按文件所属实例转发；底层素材索引接口继续供内部存储和交付流程使用。执行记录列表保持全局汇总，日志按任务来源实例读取，增量追加并限制浏览器保留量；关闭播放器会释放媒体连接。

账号清单与统计账本请求不携带实例头；账号浏览器登录和检测由后端按所属实例转发，取消登录携带会话 ID。密钥按需显示，导入必须使用服务端签名预览；编辑发生版本冲突时保留输入。账号归属在全局账号清单维护，请求节奏按实例提交与读回。代理、部署与 Cookie 文件导出携带实例范围；切换后忽略旧响应，保存后核对状态。金额使用服务端字符串，字节展示通过 BigInt 保留精度。实例名称与全局并发草稿不被状态轮询覆盖。

`/setup` 由部署网关独立提供，不依赖业务数据库或管理账户登录；使用本机独立访问码。恢复页只保存实例设置，MySQL / Redis 连接仍来自启动配置。管理端 `/deployment` 可以配置所选实例的连接，密码不回显，保存后等待重启。构建产物使用相对资源路径，同时适配 `/static/frontend/` 与 `/setup-static/frontend/`。未构建 Vue 资源时显示构建提示，不运行旧界面。

需要 Node.js 22.12+。首次启动前从根目录执行：

```powershell
python scripts/generate_frontend_contract.py --check
cd frontend
npm.cmd ci --cache ../.cache/npm
npm.cmd run build
cd ..
```

然后启动 Flask。开发时在 `frontend/` 运行 `npm.cmd run dev`，监听源码并重新构建，浏览器刷新对应页面查看；不需要额外的 Node 服务或跨域代理。Linux 可使用 `npm`。

`frontend/src/generated/config.ts` 由后端参数定义生成，实例配置与任务保存字段分别导出。`ScopePanel.vue` 统一表达查看、配置及操作范围。任务保存位于版本化交付合同中，产物以合同版本读取原保存目标，不从实例配置或环境变量回退。修改 `webapp/config/schema.py` 后运行 `python scripts/generate_frontend_contract.py` 并提交生成文件；CI 使用 `--check` 防止前后端字段漂移。运行 `npm.cmd test` 检查请求与保存合同，`npm.cmd run build` 包含严格类型检查；`npm.cmd run format` 统一代码格式，CI 使用 `format:check` 检查。浏览器回归入口为 `python scripts/verify/verify_frontend_settings_ui.py`。生产资源输出到 `static/frontend/`，不提交 Git，由 Docker 构建阶段生成。依赖版本由 `package-lock.json` 固定，`.npmrc` 兼容 npm 10 对 Vitest 可选浏览器依赖的解析问题。

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

前端浏览器回归使用隔离 SQLite 和临时目录：`python scripts/verify/verify_frontend_workspace_ui.py` 检查路由、未确认写入重试、部分导入失败、任务中心文件入口和手机布局；`python scripts/verify/verify_frontend_execution_ui.py` 检查两个实例的工作台启停、过期返回、慢容量请求、日志来源及操作确认；`python scripts/verify/verify_task_results_ui.py` 检查真实媒体和字幕预览、验收、留样策略与手机布局；`python scripts/verify/verify_frontend_settings_ui.py` 检查设置保存与实例隔离；`python scripts/verify/verify_frontend_management_ui.py` 检查账号冲突、导入预览、密钥、Cookie 导出、账号池与节奏独立保存、代理、实例、账单、部署和独立恢复页。无需启动真实下载或连接业务数据库。

模块拆分完成后直接维护本手册，不再保留逐批迁移计划和重复验收报告。
