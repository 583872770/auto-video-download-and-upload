# 验证脚本

`python scripts/verify/verify_dialog_ui.py` 检查弹窗外部点击、内部点击与拖动、多层弹窗、确认取消、未保存草稿、提交保护、通知抽屉、顶部浮层和手机布局；仅使用隔离数据，不执行真实下载或修改业务配置。

这里保存独立的功能和页面回归脚本，直接使用本目录路径运行；不再保留根目录的转发入口。Vue 浏览器回归使用 verify_frontend_*_ui.py，测试临时数据和截图保存在 .runtime/verify/。

`python scripts/verify/verify_auto_reduce_ui.py` 验证自动降低下载进程上限开关的默认状态、布尔提交、保存读回、刷新保留、键盘操作、桌面/手机布局以及工作台关闭状态。使用隔离数据，运行前先构建前端，截图保存在 `.runtime/verify/auto-reduce/`。

`python scripts/verify/verify_failed_tests_ui.py` 使用隔离数据库、本地 HTTP 媒体和真实 FFmpeg，验证失败测试列表、取消确认、清理、本地重试、重新下载以及桌面和手机布局。运行前先构建前端；截图输出到 `.runtime/verify/failed-tests/`。

`python scripts/verify/verify_task_results_ui.py` 用隔离数据库和真实本地媒体验证任务中心统一文件入口、字幕全文及下载、人工验收、留样设置、观察页和 390 像素手机布局。截图输出到 `.runtime/verify/task-results/`。百万级查询验证运行 `python scripts/verify/verify_job_search_scale.py`，覆盖任务内精确 ID、跨任务 ID 和保留样本筛选。
