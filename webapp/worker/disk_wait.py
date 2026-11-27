"""已领取的视频在原进程等待磁盘恢复。"""
import time

from webapp.diagnostics import event, safe_text
from webapp.disk_budget import DiskCoordinationBusy, DiskPressure, is_disk_write_pressure
from webapp.downloads import DownloadLeaseLost
from webapp.throughput import PipelineBusy
from webapp.wait_diagnostics import start_wait


DISK_RECHECK_SECONDS = 5


def run_with_disk_wait(execute, heartbeat, report, *, start_ticket=None):
    disk_interrupted = False
    pressure = None
    space_ready = None

    def resumed_progress(value):
        # 预检通过后仍需正式预留。准备目录和解析的进度不应提前清除磁盘等待。
        if space_ready and heartbeat.wait_state == 'disk_wait' and not value.get('space_plan'):
            return
        if heartbeat.wait_state:
            value = {**value, 'wait_reason': None, 'retry_at': None, 'disk_pressure': None}
            report(value)
            heartbeat.set_wait()
        else:
            report(value)

    while not heartbeat.stop.is_set():
        try:
            if space_ready:
                if not space_ready():
                    heartbeat.stop.wait(DISK_RECHECK_SECONDS)
                    continue
                if heartbeat.stop.is_set():
                    break
            result = execute(resumed_progress)
            if heartbeat.wait_state:
                heartbeat.set_wait()
            return result
        except DiskCoordinationBusy as error:
            if not disk_interrupted:
                raise
            # 账本繁忙时仍保留原视频，保留上次确认的空间等待详情直到申请成功。
            delay = DISK_RECHECK_SECONDS
            if not heartbeat.wait_state:
                wait = start_wait('disk_coordination', detail=str(error))
                heartbeat.set_wait('waiting_start', wait)
        except PipelineBusy as error:
            if not disk_interrupted:
                raise
            # 磁盘恢复后重新分配账号，临时调度限制也不能把这个已领取的视频退回队列。
            delay = max(1, error.retry_after)
            until = time.time() + delay
            wait = start_wait(error.wait_code, detail=str(error), until=until)
            heartbeat.set_wait('waiting_start', wait)
            report(dict(stage='scheduling_wait', wait_reason=wait['detail'], retry_at=until,
                        disk_pressure=None, speed=None, eta=None, network_phase=None))
        except Exception as error:
            if not isinstance(error, DiskPressure) and not is_disk_write_pressure(error):
                raise
            # 异常已退出下载或转换的上下文。此时账号、文件句柄和未用完的预留已释放。
            # 保留任务租约和进程名额，给本地处理腾出空间的机会，不再领取另一条视频。
            if start_ticket:
                start_ticket.finish()
            delay = DISK_RECHECK_SECONDS
            detail = getattr(error, 'detail', None)
            space_ready = getattr(error, 'download_space_ready', None)
            wait = dict(code='disk_wait', label='等待磁盘空间', detail=safe_text(error)[:350])
            if not disk_interrupted or wait != heartbeat.wait or detail != pressure:
                if heartbeat.wait_state != 'disk_wait':
                    event('disk_wait_entered', **wait)
                heartbeat.set_wait('disk_wait', wait)
                report(dict(stage='disk_wait', speed=None, eta=None, network_phase=None,
                            cookie_profile_id=None, cookie_profile_name=None,
                            wait_reason=wait['detail'], retry_at=None, disk_pressure=detail))
            disk_interrupted = True
            pressure = detail
        # 停止时由原有终止流程中断进程。心跳失去租约后，同样不能继续写入文件。
        heartbeat.stop.wait(delay)
    raise DownloadLeaseLost('任务已停止或租约已失效，结束磁盘等待。')
