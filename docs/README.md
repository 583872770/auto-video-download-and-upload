# 文档导航

日常查阅从这里开始。文档按部署、使用、运维和开发分类；只额外保留近期仍有价值的[发布与检查记录](archive/README.md)。

## 部署

| 文档 | 内容 |
| --- | --- |
| [Docker 部署](deployment/docker.md) | 应用与 MySQL、Redis 一起部署，或连接远程服务；更新、密钥与备份 |
| [MySQL 与迁移](deployment/mysql.md) | 数据库版本、权限、连接、结构升级及旧 SQLite 迁移 |
| [多实例](deployment/instances.md) | 共享数据库和缓存，独立实例身份、文件与状态目录 |
| [浏览器环境](deployment/browser.md) | Chromium、虚拟显示、远程桌面与排错 |

## 使用

| 文档 | 内容 |
| --- | --- |
| [页面与流程](guides/workspace.md) | 页面职责、任务与实例控制、设置保存 |
| [任务操作](guides/tasks.md) | 新建、添加视频、启停、归档、删除和历史数据 |
| [账号管理](guides/accounts.md) | 导入资料、登录绑定、唯一 Cookie 和加密密钥 |
| [YouTube 登录](guides/youtube-login.md) | 浏览器登录、外部 Cookie 导出与手工更新 |
| [交付要求](guides/delivery.md) | 四类产物、15 种组合、要求版本、缺项与补交 |
| [账单与统计](guides/billing.md) | 客户任务计费、精确字节、测试模式和导出 |
| [下载器](guides/downloaders.md) | yt-dlp、网页渠道、画质及兼容任务 |
| [网络与代理](guides/proxy.md) | 多订阅、节点选择、连接与实例隔离 |
| [磁盘管理](guides/disk.md) | 空间预算、临时文件、日志、等待与清理 |
| [OSS 保存](guides/oss.md) | Endpoint、Bucket、凭据、上传与本地副本 |

## 运维

| 文档 | 内容 |
| --- | --- |
| [调度与渠道](operations/scheduler.md) | 下载顺序、解析、传输及多渠道共享许可 |
| [自动吞吐](operations/throughput.md) | 资源采样、探索并发、代理冷却与集群限制 |
| [账号池与节奏](operations/cookie-pool.md) | 账号准入、启动间隔、休息、滚动预算及 0 值语义 |
| [错误与恢复](operations/recovery.md) | 403、429、登录验证、网络错误及人工恢复 |
| [资源保护](operations/resources.md) | CPU、内存、I/O 压力、进程缩容和恢复 |
| [数据库预算](operations/database-budget.md) | 进程池、部署上限和多实例连接预算 |
| [容量与扩容](operations/capacity.md) | 大清单、实例扩容、观测指标和验证边界 |

## 开发

| 文档 | 内容 |
| --- | --- |
| [项目结构](development/project-layout.md) | 入口、模块、目录职责与持久化边界 |
| [配置维护](development/configuration.md) | 默认值、参数范围、校验、保存和兼容字段 |
| [容量测试](development/capacity-testing.md) | 使用隔离数据库模拟多实例负载 |
| [媒体传输](development/media-transfers.md) | HTTP Range 并行、恢复及限定 HLS 兼容 |
| [验证脚本](../scripts/verify/README.md) | 测试入口、兼容脚本和临时产物位置 |

## 文档维护

- 根目录只保留项目 `README.md`；常用说明放入上述四类，新增前先检查能否并入同主题文档。
- 当前说明使用小写英文和连字符命名，不在文件名中加入“最终版”“完成报告”或发布日期。
- 只保留与当前版本有关的发布、回滚及尚有排查价值的检查记录，放入 `archive/`，使用 `YYYY-MM-DD-topic.md`；被新版替代后删除，不持续累积历史文档。
- 实现变更直接更新对应手册。已完成的设计、重复总结和旧测试报告无需另存；有价值的规则与限制并入现行说明。
- 移动或合并文档时同步相对链接、图片、原型和代码引用；旧主题合并到现行手册，不保留重复的跳转占位文件。

文件中的启动命令均从项目根目录执行。
