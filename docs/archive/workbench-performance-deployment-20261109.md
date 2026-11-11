# 工作台优化部署与磁盘阻塞现场

## 已发布主版本

- 洛杉矶服务器：`216.227.169.90`。
- 切换完成：2026-11-09 23:15:47（北京时间）。
- 当前镜像：`youtube-downloader-app:workbench-performance-20261109-r1`。
- 当前容器：`4455bf1fac6f405f45042dd0c4f52ad6a0a023752ecbbdff20ad35f21e87e362`。
- 发布目录：`/opt/youtube-downloader/releases/workbench-performance-20261109-r1`。
- 154 个运行文件核对通过；镜像内 124 项 Linux 回归通过。
- 已删除视频处理进度列表，调整工作台布局，启用精简接口、响应压缩、按可见页面轮询及卡片原地更新；包含此前的实例 cgroup I/O 采样和动态进程保护提示。

切换保留配置文件、Cookie 库、实例身份、任务选择、挂载、端口及原来的运行意愿，恢复暂停标记为 0。账号最小间隔为 100 秒、共用间隔 5 秒、传输上限 12、账号并发 1、小时预算 50、日预算 720；小时预算在此前排查之后由线上配置变为 50，本次部署没有修改这些参数。Cookie 池仍为 18 个成员。

MySQL 与 Redis 未替换。异常 MySQL `youtube-downloader-local-mysql-1` 继续停止。它与健康 MySQL 共用数据卷，禁止启动异常容器。已核对当前应用、健康数据库、Redis、异常数据库及两个最近的旧应用容器，重启策略均为 `unless-stopped`；已停止容器不应在主机重启时启动，重启后仍需重新确认。

## 未完成的静态压缩补丁

线上验收发现 Flask 的真实静态文件响应带 `Content-Disposition: inline`，主版本的保守判断将其跳过。JSON 和页面压缩已生效，静态脚本尚未压缩。

已在本地修复 `webapp/compression.py`，允许 inline 响应，同时继续排除附件；`tests/test_compression.py` 加入真实 inline 文件名情形，4 项测试通过。

补丁目标版本为 `workbench-performance-20261109-r2`，相对当前线上仅改变 `webapp/compression.py`。本地清单、打包与部署脚本位于 `.runtime/cache-archive-20261111/workbench-performance-deploy-r2/`。远端同名发布目录已建立并解压文件，但准备流程在 `docker tag` 阶段受磁盘阻塞超时，**未完成构建和最终 Linux 测试，未切换到 r2**。恢复后不能直接重复要求目录不存在的 prepare 脚本，应检查该目录及 Docker 标签后从构建步骤续作，或使用新版本目录。

## 磁盘阻塞经过

第一轮暂停领取后，旧服务多笔事务卡在 `waiting for handler commit` 约 20 秒，随后出现锁等待、心跳停滞。部署检查超时退出，原来的暂停标记后来成功恢复为 0。延长部署辅助脚本等待后再次暂停，确认在途任务为零，完成主版本切换。

主机采样随后记录到 I/O PSI 接近 99%、vmstat I/O wait 约 99%，磁盘一度没有在途请求，多组进程等待 `rq_qos_wait`。为排查阻塞，临时将 `/sys/block/sda/queue/wbt_lat_usec` 从 2000 改为 0，再恢复 2000；其间短时 I/O 恢复，无法将恢复单独归因于该参数。

恢复原值后阻塞再次发生，第二次设为 0 后仍有约 128 个写请求在途且长时间无法完成，不能认定关闭 WBT 解决了问题。再次恢复 2000 的写入也阻塞：现场 bash 进程 `2576010` 等待 `blk_mq_freeze_queue_wait`，文件系统日志、后台写回及应用进程等待 `bio_queue_enter`，另一个读取队列属性的进程等待 `queue_attr_show`。恢复操作没有正常返回，**当前不能确认 sysfs 值已恢复为 2000**。没有写入持久内核配置，也没有调整 MySQL 落盘保障参数。

已向用户申请重启整机以恢复磁盘队列；在获得明确授权前不重启。重启后应核对 sysfs 默认值、磁盘读写、MySQL 恢复、异常数据库继续停止、应用镜像/配置以及任务恢复，再完成静态压缩补丁及真实浏览器验收。此次临时内核调整没有解决问题，并使恢复操作更复杂，已向用户说明并致歉。

当前主版本曾通过切换后约 78 秒的健康、挂载、服务容器和日志检查，但随后整机阻塞复发；**不能将该短时检查当成当前服务已稳定或最终验收通过**。真实浏览器验收因未取得账号卡片超时，不应报告通过。

原始输出及辅助脚本：`.runtime/cache-archive-20261111/workbench-performance-deploy/`、`.runtime/cache-archive-20261111/workbench-performance-deploy-r2/`。目录中可能包含部署检查材料，不应公开分发。

## 后续只读复核

用户继续询问整机 I/O 阻塞的原因，未授权整机重启。新增只读辅助脚本 `io_root_cause_readonly.py`，计划读取 PSI、磁盘计数、阻塞线程和内核栈；SSH 认证后打开命令通道超时（`Timeout opening channel`），未取得新现场，不能据此确认当前压力值或已恢复。

早先 22:15 左右的 8.1 秒采样中，应用容器写入约 68.3 MiB/s、MySQL 约 0.65 MiB/s，同时整机 PSI some avg10 仅 1.77%，磁盘写等待约 35 ms。这说明下载是该窗口主要写入来源，但不能将后续接近 99% 等待、磁盘完成量为零的异常，直接解释为这些顺序写入超过磁盘带宽。PSI 是等待时间比例，不是带宽使用率。

目前原始根因仍需在内核块层/WBT 的等待异常与虚拟磁盘或下层存储停顿之间进一步取证；既未证明物理磁盘损坏，也未证明 WBT 是最初根因。恢复 WBT 参数时出现的队列冻结是另一个已观测到的后续问题，应明确区分。

## 11 月 10 日 00:12 只读现场更新（北京时间）

新一轮 SSH 只读检查已部分成功；未重启、未写内核参数、未压测。

- 00:12:30：整机 PSI some avg10=99.93%、full avg10=98.33%。
- 00:12:33：some avg10=99.22%、full avg10=97.36%。两次相隔约 2.565 秒的磁盘计数中，sda 已完成读、写、flush 数量和扇区数均不变，inflight=4。这个短窗口确认无完成进展，不能单凭它判定永久死锁。
- 原先冻结参数写入的 PID 2576010 已不存在；当前不能继续声称它仍在冻结队列，也不能据此认定参数已恢复。未读取 sysfs 队列属性，避免可能阻塞的属性锁。
- MySQL PID 504909 的 TID 505085 调用栈为 `fdatasync -> ext4_sync_file -> blkdev_issue_flush -> submit_bio_wait -> blk_mq_submit_bio -> __rq_qos_throttle -> wbt_wait -> rq_qos_wait`，明确有数据库持久化请求在 WBT 入口等待。
- jbd2 PID 254 为 `jbd2_journal_commit_transaction -> jbd2_journal_wait_updates`；后台写回 PID 2553944 等待 ext4/jbd2 事务额度：`ext4_writepages -> ext4_do_writepages -> jbd2__journal_start -> start_this_handle -> add_transaction_credits -> wait_transaction_locked`。
- MySQL 其他连接、容器 shim、应用线程、日志服务也出现文件系统事务等待；线程状态会变化，不能将旧的线程状态与新的调用栈强行视作同一瞬间。
- `uptime` 和 `dmesg` 部分检查超时；使用直接读取 `/proc` 的后续检查成功。不把未成功的内核日志读取当成“没有错误”。

这些证据已把当前阻塞位置缩小到块层 WBT 与 ext4/jbd2 刷盘等待链。它仍不能区分是内核等待/记账异常，还是下层虚拟存储长延迟使 WBT 等待不释放；也不能重建临时参数操作之前的全部因果关系。

建议的归因方法：保留现场后恢复服务，在不构建镜像的情况下先观察无新下载时的磁盘完成量/延迟，再按 1、2、4 路逐步恢复下载，并同步采集容器 I/O、MySQL 提交等待、PSI、磁盘完成量及内核栈。低负载也复发则不能只归因于下载并发；达到相近负载才稳定复现则进一步区分磁盘限额与应用写放大。服务商需核查北京时间 11 月 9 日 23:05 至 11 月 10 日 00:13（UTC 11 月 9 日 15:05–16:13）的宿主机存储延迟、IOPS/吞吐限制及突发额度。重启后恢复本身不能证明根因。

## 已授权执行正常重启

用户明确要求“帮我重启服务器”。北京时间 11 月 10 日 00:22:02 左右，先读取 boot ID `38b4e663-35f2-43c9-9b9f-71e30bf4f6ad` 和运行时间约 309814 秒，随后执行 `systemctl reboot --no-block`，返回码 0。未执行强制 reboot、SysRq 或再次写入磁盘内核参数。

命令后 SSH 握手断开，HTTP 请求也被关闭，数分钟内多次重新连接均未取得 SSH banner。系统接受重启请求不等于已完成重启；尚未观察到新的 boot ID，尚未验证容器或磁盘恢复。已向用户请求 VPS 服务商及管理后台地址，以便在关机持续阻塞时通过宿主机控制台处理。

辅助脚本 `.runtime/cache-archive-20261111/workbench-performance-deploy/reboot_host.py`；本地状态为 `reboot-state.json`，后续观测记录为 `reboot-monitor.jsonl`。这些文件不含连接密码。恢复后需核对健康 MySQL、Redis、应用和所有停止容器，尤其禁止启动共用卷的异常 MySQL 容器。

用户提供 FDC 管理后台 `https://portal.fdcservers.net/index.php?rp=/user/accounts`。尝试 computer-use 技能及浏览器入口：浏览器访问宿主不可用，原生桌面管道报 `Computer Use native pipe is unavailable`，备用 `@oai/sky` 也报同一管道缺失，无法登录或操作后台；没有通过其他方式读取浏览器凭据。已请求用户在后台查看该 IP 的控制台，若关机卡住则执行 Reset/强制重启。约 00:29 的 SSH 探测仍未取得服务端 banner，不能报告重启成功。

## 重启完成及业务恢复

00:30:59 取得新 boot ID `33128261-5e49-456a-9960-5d3375e48383`，运行时间约 130 秒，确认整机已完成重启。随后已明确告知用户不要再重复后台重启。无法仅根据外部连接过程确认期间用户是否另在后台操作；本助手仅执行过一次正常系统重启。

开机初期 PSI 曾接近 100%、Docker 查询超时；00:31:54 磁盘计数已继续推进，00:33:42 some/full avg10 均降至 0%。不能将开机时的高等待直接当成原有永久阻塞复发。只读核对 WBT 已回到默认 2000。当前内核为启动日志所示 `6.8.0-147-generic`，此前为 `6.8.0-139-generic`；本次没有执行内核安装或升级，重启使用了服务器已有的内核。

内核在开机约 60–71 秒记录 RCU CPU stall，调用栈涉及启动阶段的 `free_initrd_mem`、跨 CPU TLB 同步等；该日志发生于容器业务启动前，是新增的系统层调查线索，不能直接证明它与此前 WBT 阻塞同源或确定服务商责任。未进行进一步内核参数修改或磁盘压力测试。

应用、健康 MySQL 和 Redis 自动启动，旧应用及异常 MySQL 保持停止。但业务检查揭示启动时序问题：应用与 MySQL 同时启动，MySQL 连接失败（2003）后应用停在 setup 页，`/health` 仍返回 HTTP 200，Docker 因此误判 healthy。此前 `verify_host.py` 的严格日志检查失败，随后已用实际 `/health` JSON 和工作台接口确定这一问题。

确认健康数据库已就绪、异常 MySQL 停止后，仅执行 `docker restart -t 30 youtube-downloader-local-app-1`。重启约 4 秒后 `/health` 为 `status=ok, role=hybrid`。00:36 左右工作台认证接口 HTTP 200，耗时约 0.091 秒，实例 online=true、paused=false、stop_requested=false、running=2、error 为空；当时 transferring=0，不能把两个处理中任务表述成两个实际传输。MySQL 当前行锁等待数为 0、Threads_running=2；整机 PSI some avg10 2.65%、full avg10 1.29%。这是恢复后的短时采样，不代表原始根因已解决。

自动启动遇到数据库暂未就绪后不会自动恢复，以及健康检查不区分 setup/starting 与业务 ready，是已识别但尚未修改的可靠性问题。r2 静态压缩补丁和真实浏览器验收也仍待完成；本轮优先完成用户要求的重启与业务恢复，未构建或切换应用镜像。
