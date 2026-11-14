"""Public explanations of a task's observed wait, without changing admission."""
import math

from webapp.diagnostics import safe_text


RESOURCES = {
    'local-transfers': '本机传输名额',
    'local-upstream': '本机上游传输名额',
    'upstream': '共享上游传输名额',
    'local-connections': '本机下载连接',
    'connections': '共享下载连接',
    'channel': '下载渠道名额',
    'quota': '共享渠道配额',
    'backend-quota': '后端共享配额',
    'resolver': '解析名额',
    'backend-resolver': '后端解析名额',
}


def task_wait(progress, *, states=None, identity='local', stamp=0):
    stage = progress.get('stage', '')
    if not (stage.startswith('waiting_') or stage in ('disk_wait', 'scheduling_wait', 'retrying')):
        return None
    until = None
    if stage == 'waiting_transfer':
        resource = progress.get('transfer_wait_resource') or ''
        prefix = resource.split(':', 1)[0]
        name = RESOURCES.get(prefix, '传输名额')
        # The channel health gate reports channel:<id>, whereas capacity
        # gates report just the resource prefix (or prefix:cooldown).
        cooling = resource.endswith(':cooldown') or (prefix == 'channel' and ':' in resource)
        code = 'transfer_' + (prefix if prefix in RESOURCES else 'unknown') + ('_cooldown' if cooling else '')
        label = name + ('冷却中' if cooling else '暂不可用')
        detail = '冷却结束后重新检查额度' if cooling else '等待其他任务释放名额或额度恢复'
        if cooling:
            scope = (resource if resource in (states or {}) else
                     'channel:' + str(progress.get('channel_id', '')) if prefix == 'channel' else '')
            until = (states or {}).get(scope, {}).get('cooldown_until')
    elif stage == 'waiting_request':
        if progress.get('request_wait_reason') in ('subtitle_session', 'subtitle_queue'):
            warming = progress['request_wait_reason'] == 'subtitle_session'
            code = 'subtitle_session' if warming else 'subtitle_queue'
            label = '翻译字幕会话预热' if warming else '字幕请求间隔'
            detail = '复用当前 YouTube 会话，预热后请求自动翻译字幕' if warming else '保留当前字幕会话，等待共享字幕请求名额'
            until = progress.get('request_wait_until')
        elif progress.get('request_wait_reason') == 'file_start':
            code, label, detail = 'file_interval', '下载前请求间隔', '按下载请求节奏等待后发起传输'
            until = progress.get('request_wait_until')
        else:
            code, label, detail = 'request_interval', '视频启动请求间隔', '等待下一次允许发起请求的时间'
            until = (states or {}).get('youtube-start:' + identity, {}).get('next_at')
    else:
        code = stage
        label, detail = {
            'waiting_cookie': ('可用下载账号', '等待账号可用并满足冷却、并发和启动间隔要求'),
            'waiting_resolver': ('解析名额', '等待正在解析的任务释放名额'),
            'waiting_browser': ('浏览器名额', '等待浏览器实例空闲'),
            'waiting_verification': ('网页验证', '需要完成下载页面的验证'),
            'waiting_processing': ('本机后处理名额', '等待合并或转换任务结束，释放处理名额'),
            'disk_wait': ('磁盘空间恢复', '磁盘满足空间和预算要求后重新检查'),
            'scheduling_wait': ('调度条件恢复', '资源或上游额度满足条件后重新调度'),
            'retrying': ('重试间隔', '到检查时间后重新检查执行条件'),
        }.get(stage, ('等待执行条件', '具体条件尚未上报，请查看任务日志'))
        if stage in ('scheduling_wait', 'disk_wait', 'retrying'):
            detail = safe_text(str(progress.get('wait_reason') or detail))[:300]
            until = progress.get('retry_at')
    result = {'code': code, 'label': label, 'detail': detail}
    if isinstance(until, (int, float)) and not isinstance(until, bool) and math.isfinite(until) and until > stamp:
        result['until'] = until
    return result
