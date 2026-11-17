# 容量与准入测试

运行 `python -m unittest tests.test_shared_output_admission tests.test_database_budget tests.test_cookie_pool tests.test_resource_pressure tests.test_lease_ownership tests.test_download_start_gate`，验证任务准入、账号容量、数据库连接、资源保护和租约隔离。

交付回归使用 `tests.test_delivery` 和 `tests.test_delivery_files`：通过本地 HTTP 与真实 FFmpeg 验证下载、转换、避免重复复制及独立处理队列，OSS 使用可控模拟，不依赖外部 YouTube。

性能观察应记录公网接收带宽、CPU、可用内存、I/O 压力、磁盘吞吐、账号等待及处理队列。压力升高时只减少新任务，不强制中断已准入的媒体传输。
