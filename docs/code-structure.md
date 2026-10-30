# 代码结构与功能调用链

更新：2026-10-30。以实际调用关系说明当前实现；性能建议另见规划文档，不视为已测结论。

## 模块职责

| 入口 / 模块 | 当前职责 |
| --- | --- |
| `app.py` | Flask 应用工厂、配置读写、素材及任务接口、注册各组路由 |
| `webapp/extensions.py` | 初始化素材索引、代理、实例、认证与执行控制服务 |
| `webapp/helpers.py`、`webapp/routes/` | 共享请求辅助函数、磁盘及健康检查 Blueprint |
| `webapp/deployment.py`、`deployment_errors.py`、`migrations.py` | 部署设置、分阶段启动诊断、数据库升级 |
| `webapp/console_auth.py` | 人员登录、角色权限、实例本地 RPC 接口范围 |
| `webapp/fleet.py`、`fleet_routes.py` | 实例注册与心跳、领取准入、所选实例请求转发 |
| `webapp/jobs.py`、`workbench.py`、`execution.py` | 客户任务、所选任务启停、系统停止/恢复 |
| `webapp/store.py`、`database.py`、`tasks.py` | 事务化队列、表结构、计数器、租约、任务运行器 |
| `web_worker.py` | 单任务执行入口、下载器切换、下载与清理；命令行工作进程 |
| `webapp/channels.py`、`channel_scheduler.py`、`throughput.py` | 渠道注册、共享准入与退避、解析/传输容量控制 |
| `webapp/downloader.py`、`yt5s.py`、`downmp3.py`、`yt1d.py`、`bergung.py` | 下载选项与各渠道解析适配 |
| `webapp/clash.py`、`youtube_auth.py`、`youtube_login.py` | 代理进程及订阅、Cookie 验证、浏览器登录 |
| `webapp/statistics.py`、`statistics_methods.py` | 执行会话、交付文件登记、实际下载方式统计 |
| `webapp/catalog.py`、`disk_budget.py`、`Utils/download_storage.py` | 素材索引、容量预算与清理、测试下载工作区 |
| `templates/`、`static/` | 页面、草稿与配置回显、任务和实例实时状态 |

应用内部模块统一位于 `webapp/`。不要新建与 `app.py` 同名的 `app/` 包，否则 `from app import create_app` 可能导入错误对象，造成启动失败。

## 配置保存与生效

1. `static/app.js` 收集所选实例的表单草稿，冻结保存中的表单。
2. `PUT /api/config` 使用固定的 `X-Instance-ID`；远端实例经 `fleet_routes` 转发。
3. 服务端校验、保存文件或实例配置，更新运行时策略版本。
4. 浏览器以同一实例重新 `GET /api/config`，比对提交字段后才显示保存成功；读回失败或不一致时保留草稿。
5. 待领取任务在 `TaskStore._claim()` 读取最新有效策略并生成不可变执行快照；正在执行的视频继续使用已领取配置。

配置闭包每次读取当前配置，并只合并实例的传输并发、浏览器并发和磁盘预留三项资源限制；历史限制 JSON 中的策略字段不会覆盖新设置，也不是全局 YAML 缓存。下载暂停标志直接读取数据库，不使用各进程互不一致的五秒缓存。

## 创建、启动与完成

任务中心创建客户任务并导入视频，创建不等于启动。下载工作台选择任务和实例后，经预检启动；实例暂停停止后续领取，停止还请求终止当前执行。系统恢复与实例/任务启动有不同范围，`execution.py` 与 `jobs.py` 负责检查。

领取事务检查系统、任务及实例权限、磁盘、共享并发预算，再绑定执行配置、会话和租约。MySQL 预算行从一开始使用独占锁，因为准入需要修改共享额度；不能先取共享锁再升级。失败时整个领取事务回滚，死锁按有限次数重试。

运行器使用单任务 `heartbeat()` 续期。`batch_heartbeat()` 是可用接口，目前没有接入运行器；两者均需匹配任务和执行者。不能把批量接口存在当作已获得实际延迟收益。

自动策略在实际分配适配器时登记方式统计，顺序策略按配置顺序登记并在失败切换时更新。交付大小在最终文件校验后登记，测试文件与计费文件分开，重复回调不重复计数。客户费用归属于客户任务，执行会话不生成另一份账单。

## 跨实例操作

管理页面先检查人员会话、角色和 CSRF，再使用目标实例凭据转发本地资源接口。`LOCAL_RPC_METHODS` 必须与真实路由的方法对应；更新接口时应同步运行协议契约测试。实例凭据不允许管理共享客户任务、账户、账单或执行全局停止。

素材播放既检查路径分量，也检查 `resolve()` 后的路径是否仍在素材根目录内。文件名中间合法的两个点不等于父目录。清空素材库要求全部下载暂停且没有在途下载，仅清理接受该操作的实例本地素材。

## 验证入口与边界

- `python -m unittest discover -s tests -v`：后端完整回归；MySQL 测试需独立测试库配置。
- `python scripts/test_imports.py`、`python docker/check_startup.py`：导入及打包启动检查。
- `.github/workflows/tests.yml`：当前持续集成的浏览器检查清单。
- `python scripts/verify_p0_fixes.py`：有真实断言和失败退出码的定向回归入口。

本地 SQLite、模拟第三方网站及浏览器检查不能替代线上 MySQL、真实代理内核与第三方服务可用性测试。具体检查范围及结果见 [全项目检查报告](full-project-audit-20261030.md)。
