"""Public explanations of a task's observed wait, without changing admission."""
import math

from webapp.diagnostics import safe_text


START_WAITS = {
    'start_interval': ('实例启动间隔', '本实例按设置错开每个新任务的启动时间。'),
    'account_interval': ('账号启动间隔', '有空闲账号，但距离该账号上次开工的间隔还未结束。'),
    'account_rest': ('账号任务后休息', '有空闲账号，但该账号上一条任务结束后的休息时间还未到。'),
    'account_quota': ('账号启动次数已用完', '最早可用账号的每小时或每日启动次数已达上限，等待额度恢复。'),
    'account_capacity': ('账号并发名额已满', '当前可用账号的并发名额已占满，等待正在执行的任务释放名额。'),
    'account_cooldown': ('下载账号冷却中', '当前需等待账号冷却到期，可在账号管理查看触发原因和到期时间。'),
    'account_review': ('下载账号需要处理', '当前没有可用于开工的账号，请检查账号分配、登录或验证状态。'),
    'manual_retry_quota': ('手动重试启动额度受限', '手动重试正在等待启动份额，普通任务继续调度。'),
    'source_cooldown': ('YouTube 来源服务冷却', '来源服务触发了临时保护，调度器将在冷却后重新检查。'),
    'redis_unavailable': ('调度 Redis 暂不可用', '请求调度无法写入 Redis，请检查 Redis 服务和实例到 Redis 的连接。'),
    'disk_coordination': ('磁盘额度账本繁忙', '多个进程正在核对或更新临时空间额度，调度器会短暂等待后重试。'),
    'scheduler_retry': ('调度暂缓领取', '等本次调度重试。'),
}


def start_wait(code, *, detail=None, until=None):
    label, default = START_WAITS[code]
    wait = dict(code=code, label=label, detail=safe_text(detail or default)[:300])
    if until is not None:
        wait['until'] = until
    return wait


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
    elif stage == 'waiting_connection':
        code, label, detail = 'first_byte', '连接 / 首字节等待', '下载前间隔已结束，等待连接建立或首批文件数据；当前没有有效传输速度'
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
