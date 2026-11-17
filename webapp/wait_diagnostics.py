"""Public explanations of a task's observed wait, without changing admission."""
import math

from webapp.diagnostics import safe_text


def task_wait(progress, *, states=None, identity='local', stamp=0):
    stage = progress.get('stage', '')
    if not (stage.startswith('waiting_') or stage in ('disk_wait', 'scheduling_wait', 'retrying')):
        return None
    until = None
    if stage == 'waiting_transfer':
        resource = progress.get('transfer_wait_resource') or ''
        if resource == 'source:youtube':
            code, label, detail = 'source_cooldown', '来源服务暂时故障', '冷却结束后重新检查来源服务'
            until = (states or {}).get(resource, {}).get('cooldown_until')
        else:
            code, label, detail = 'resource_wait', '等待服务器资源', '资源保护恢复后继续执行'
    elif stage == 'waiting_request':
        code, label, detail = 'file_interval', '下载前请求间隔', '按下载请求节奏等待后发起传输'
        until = progress.get('request_wait_until')
    else:
        code = stage
        label, detail = {
            'waiting_cookie': ('可用下载账号', '等待账号可用并满足冷却、并发和启动间隔要求'),
            'waiting_processing': ('本机后处理名额', '等待合并或转换任务结束，释放处理名额'),
            'disk_wait': ('磁盘空间恢复', '磁盘满足空间和预算要求后重新检查'),
            'scheduling_wait': ('调度条件恢复', '资源或上游额度满足条件后重新调度'),
            'retrying': ('重试间隔', '到检查时间后重新检查执行条件'),
        }.get(stage, ('等待执行条件', '具体条件尚未上报，请查看任务日志'))
        if stage in ('scheduling_wait', 'disk_wait', 'retrying'):
            detail = safe_text(str(progress.get('wait_reason') or detail))[:300]
            until = progress.get('retry_at')
        if stage == 'disk_wait' and progress.get('disk_pressure'):
            pressure = progress['disk_pressure']
            label = ('等待临时预算' if pressure.get('kind') == 'budget' else
                     '等待预留空间' if pressure.get('kind') == 'reservation' else '等待磁盘空间')
            detail = (f'需新增预留 {pressure.get("requested_bytes", 0)/1024**3:.1f} GiB；'
                      f'当时可分配 {pressure.get("allocatable_bytes", 0)/1024**3:.1f} GiB。')
    result = {'code': code, 'label': label, 'detail': detail}
    if isinstance(until, (int, float)) and not isinstance(until, bool) and math.isfinite(until) and until > stamp:
        result['until'] = until
    return result
