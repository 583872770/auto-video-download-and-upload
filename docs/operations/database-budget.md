# 数据库连接容量

页面不再提供手动数据库预算模式。系统按所选账号数、每账号任务并发、本机任务上限及后处理进程数自动计算连接需求；账号暂时冷却不改变部署连接预留。

默认每下载或后处理进程保留 2 个连接，主服务为 5 + 2，辅助进程为 4。默认 2 个后处理进程时，固定预留为 15；N 个下载进程总需求为 15 + 2N。

部署环境的 `VIDEO_DB_CONNECTION_BUDGET` 和 `VIDEO_WORKER_DB_CONNECTION_BUDGET` 仍是硬边界，多个实例共享 MySQL 时需要合计设置。主服务及进程池规格由 `VIDEO_DB_POOL_SIZE`、`VIDEO_DB_MAX_OVERFLOW`、`VIDEO_WORKER_DB_POOL_SIZE`、`VIDEO_WORKER_DB_MAX_OVERFLOW` 决定。

实际任务上限还受内存与资源保护约束。数据库连接容量与媒体下载连接没有关联，不会削减文件分片数。
