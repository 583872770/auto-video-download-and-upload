# 设置保存与回显排查（2026-10-30）

用户报告：自动调度候选下载方式和多渠道下载策略保存后没有保持，但页面同时显示两处成功提示。

## 已确认的缺陷与修改

- 原页面只检查 PUT 和 GET 是否成功返回，没有核对提交字段与回读字段。隔离测试让 PUT 返回 200 但不写入后，页面仍显示成功并清除草稿。现在逐项核对实际回读结果，只有一致才清除修改、显示成功；不一致时列出字段并保留修改。
- 原页面在 GET 回读前清除下载方式草稿，回读失败会丢失候选选择。现在完整核对前保留草稿。
- 保存与回读期间锁定表单，避免用户在等待过程中继续编辑，然后被较早的保存结果覆盖；PUT 和 GET 固定使用同一个实例标识，回读禁用缓存。
- 下载方式说明现在使用当前候选草稿。更改下载策略等普通字段时，上方状态同步显示未保存，避免仍显示“已保存”。
- 多渠道差异比较使用载入表单时的基准。回读比较兼容服务端为渠道地址补斜杠、将数字文本转整数的正常归一化。

这些修改解决已复现的假成功和草稿丢失问题，尚不能证明它们就是线上后端返回旧值的原因。

## 验证范围

`scripts/settings_roundtrip_checks.py` 由本地和远程实例浏览器脚本共用。检查自动候选增减、三种调度策略、全部普通设置字段（画质、超时、保存目录、布尔选项、资源限制、磁盘预算）及渠道添加、全部字段修改和删除。对比页面、配置文件、GET 接口及实例登记值，并刷新页面验证保持。

另外检查成功响应但没有写入、PUT 成功后 GET 失败、失败后的重试、慢保存期间表单保护。测试使用临时目录和 SQLite，不修改生产数据或进行实际视频下载。

最终结果：以上 4 组浏览器流程全部通过；下面指定的 17 项后端测试全部通过。JavaScript 语法检查和 Git 差异空白检查通过。

```powershell
.venv/Scripts/python.exe scripts/verify_settings_save.py
.venv/Scripts/python.exe scripts/verify_settings_save.py --hybrid
.venv/Scripts/python.exe scripts/verify_hot_policy.py
.venv/Scripts/python.exe scripts/verify_hot_policy.py --hybrid
.venv/Scripts/python.exe -m unittest tests.test_settings_save tests.test_hot_policy tests.test_hybrid.HybridTests.test_node_settings_rpc_is_local_and_cannot_manage_accounts_or_jobs tests.test_hybrid.HybridTests.test_remote_settings_still_require_admin_session_and_csrf -q
```

扩大到整个 `tests.test_hybrid` 的一次运行共 26 项，23 项通过，3 项失败：跨实例 Cookie 上传鉴权、共享任务领取、后台预检次数。这些现有失败未在此次前端改动中处理，因此不能声称完整业务回归通过。

## 线上验证限制

公开的线上 `static/app.js` 与此次排查开始时的本地版本一致。线上 `/api/config` 要求管理登录，HTTP 检查返回 401。浏览器自动化宿主不可用；尝试读取已有 Chrome 窗口时，工具因无法可靠识别当前网址而停止，本次没有继续操作该窗口，也没有修改生产配置。

本次新增修改尚未部署，线上保存回退的实际请求与响应仍需核对。部署后若回读旧值，页面会明确显示“保存结果未确认”及字段名称，避免再次无依据显示成功。容器部署需要更新源码并重建镜像和容器，不能只重启旧容器。


## 后续状态更新

整项目检查已修复前文所列的跨实例 Cookie 上传、共享任务领取和后台预检问题，完整回归结果见 [全项目检查报告](full-project-audit-20261030.md)。用户随后完成部署；线上公开前端文件已与本地修复版逐字节匹配，健康接口正常。配置接口仍需要管理登录，生产环境保存后的读回结果尚待登录验证，因此不能把前端上线等同于原问题已经验收通过。


## 部署后仍出现读回不一致：旧实例覆盖值

用户部署后报告：`保存结果未确认：下载方式与提交值不一致，请检查所选实例及服务版本`。这表示写请求返回成功，但重新 GET 的 `download_methods` 与提交列表不一致；不再属于单纯的成功提示失真。

进一步在本地复现了一个新库测试没有覆盖的升级问题：`instances.limits` 可以残留旧的 `download_methods`、`download_method`、`scheduler_mode` 等策略字段。原实现把这个 JSON 的全部字段合并到 YAML / 实例配置上，因此新设置写入成功后，GET、执行策略和实例列表又被这些旧字段覆盖。重现测试在 alpha 本机和 alpha → beta 远端保存时均出现 PUT 200、GET 仍返回旧列表，修复前两种情况都断言失败。

修正：`Fleet.resource_limits()` 仅允许 `worker_count`、`yt5s_browser_count`、`min_free_gb` 三项实例资源覆盖。配置读取、执行策略、实例列表与辅助读取统一采用该规则；实例重新登记和资源控制写入时清除越界的旧字段。保存文件仍由原来的配置接口完成，不要求人工改数据库。已有三个资源控制值和在途任务快照保留。

补充覆盖：

- 本机及远端 PUT/GET 一致，实例列表与执行策略对应实际配置。
- 重启后剔除旧策略覆盖，保留三项资源控制。
- 共享浏览器验证夹具注入残留策略、渠道、保存方式和超时字段，再验证保存及刷新。
- 如果仍读回不一致，页面同时显示提交与读回的下载方式名称，便于继续核查，草稿继续保留。

62 项后端回归全部通过；独立实例设置、混合实例设置、跨实例策略热更新三组浏览器回归通过；JavaScript 语法及 Git 空白检查通过。后端回归日志：`.cache/settings-legacy-override-tests.log`。

线上是否存在这些旧字段仍需要登录后的接口 / 数据证据确认；本轮已证明该场景可造成相同现象，尚未在生产库上核实。此次是前一轮部署后的新增后端修复，需要部署包含 `app.py`、`webapp/fleet.py`、`webapp/helpers.py` 的新版本；多实例时更新每个可被选中的实例。静态资源的读回差异提示也有更新。
