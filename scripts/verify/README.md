# 验证脚本

这里保存独立的功能和页面回归脚本，直接使用本目录路径运行；不再保留根目录的转发入口。Vue 浏览器回归使用 verify_frontend_*_ui.py，测试临时数据和截图保存在 .runtime/verify/。

`python scripts/verify/verify_failed_tests_ui.py` 使用隔离数据库、本地 HTTP 媒体和真实 FFmpeg，验证失败测试列表、取消确认、清理、本地重试、重新下载以及桌面和手机布局。运行前先构建前端；截图输出到 `.runtime/verify/failed-tests/`。

`python scripts/verify/verify_task_results_ui.py` 用隔离数据库和真实本地媒体验证任务中心统一文件入口、字幕全文及下载、人工验收、留样设置、观察页和 390 像素手机布局。截图输出到 `.runtime/verify/task-results/`。百万级查询验证运行 `python scripts/verify/verify_job_search_scale.py`，覆盖任务内精确 ID、跨任务 ID 和保留样本筛选。
