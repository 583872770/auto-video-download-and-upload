"""Read-only, bounded explanations of persisted recovery decisions."""
from webapp.diagnostics import safe_text


SCOPE_NAMES = {
    'channel': '渠道健康', 'resolver': '共享解析', 'upstream': '共享传输',
    'quota': '共享渠道配额', 'backend-quota': '后端共享配额',
    'backend-resolver': '后端解析', 'subtitles': '字幕补下载',
    'youtube-pacing': '实例启动节奏', 'youtube-cookie-pool': '当前实例 Cookie 池',
    'youtube-start': '实例固定启动间隔',
    'execution': '当前实例停止领取',
}


def public_failure(value):
    if not isinstance(value, dict):
        return None
    result = {k: value.get(k) for k in ('incident_id', 'observed_at', 'source', 'phase', 'kind',
                                       'http_status', 'evidence', 'retry_after', 'retryable', 'task_id', 'profile_id')}
    result['profile_name'] = safe_text(value['profile_name'])[:80] if value.get('profile_name') else None
    result['message'] = safe_text(value.get('message', ''))[:1000]
    return result


def explain(states, channels, pool, pacing, stamp, *, paused=False, running=0):
    restrictions, incidents = [], {}
    active_halt = None
    relevant = {r['scope'] + ':' + r['group'] for channel in channels for r in channel['resources']}
    relevant.update(scope for scope in states if scope.startswith((
        'youtube-pacing:instance:', 'youtube-cookie-pool:', 'youtube-start:', 'subtitles:', 'execution:')))
    for scope in sorted(relevant):
        state = states.get(scope, {})
        prefix = scope.split(':', 1)[0]
        label = SCOPE_NAMES.get(prefix, prefix)
        manual = (prefix == 'youtube-cookie-pool' and bool(state.get('halted')) or
                  prefix == 'execution' and bool(state.get('active')))
        until = state.get('cooldown_until', 0)
        if manual or until > stamp:
            restrictions.append(dict(scope=label, action='halt' if manual else 'cooldown',
                until=until, manual=manual, phase=(state.get('last_decision', {}).get('failure') or {}).get('phase'),
                legacy=not bool(state.get('decisions')), reason=state.get('halt_kind') if manual else None))
            if manual and state.get('halt_decision'):
                active_halt = state['halt_decision']
                _incident(incidents, active_halt)
        elif state.get('recover_until', 0) > stamp:
            restrictions.append(dict(scope=label, action='observe', until=state['recover_until'], manual=False))
        for record in state.get('decisions', [])[-8:]:
            _incident(incidents, record)
    if pool:
        if pool.get('halted') and pool.get('halt_decision'):
            active_halt = pool['halt_decision']
            _incident(incidents, active_halt)
        restrictions.extend(pool.get('session_restrictions', []))
        for record in pool.get('decisions', [])[-8:]:
            _incident(incidents, record)
        if (pool.get('halted') or pool.get('known') and pool.get('cooldown_until', 0) > stamp) and not any(
                r['scope'] == SCOPE_NAMES['youtube-cookie-pool'] for r in restrictions):
            restrictions.append(dict(scope=SCOPE_NAMES['youtube-cookie-pool'], action='halt' if pool.get('halted') else 'cooldown',
                until=pool['cooldown_until'], manual=bool(pool.get('halted'))))
    if pacing and pacing['next_start_at'] > stamp:
        restrictions.append(dict(scope='实例与会话启动预算', action='wait',
            until=pacing['next_start_at'], manual=False, reason=pacing.get('reason') or '等待下一次视频启动'))
    for scope, state in states.items():
        if scope.startswith('youtube-start:') and state.get('next_at', 0) > stamp:
            restrictions.append(dict(scope='实例固定启动间隔', action='wait', until=state['next_at'], manual=False,
                                     reason='按已保存的固定间隔等待'))
    history = sorted(incidents.values(), key=lambda r: r['time'], reverse=True)[:8]
    if active_halt and not any(r['incident_id'] == active_halt['incident_id'] for r in history):
        history = [*history[:7], incidents[active_halt['incident_id']]]
    manual = any(r['manual'] for r in restrictions)
    waits = [r for r in restrictions if r['until'] > stamp and r['action'] != 'observe']
    return dict(restrictions=restrictions, history=history, manual_required=manual,
        longest_wait_until=max((r['until'] for r in waits), default=0),
        combination='各层截止时间取较晚者，不将冷却时长相加；不同渠道和阶段按各自资源检查。',
        recovery=dict(instance_stopped=bool(paused), downloads_idle=running == 0,
            cooldown_complete=not any(r['until'] > stamp and r['action'] in ('halt', 'cooldown') for r in restrictions),
            instructions='先停止实例并等待当前任务结束；冷却到期后检测全部池成员并解除拦截，再手动恢复。'
                         if manual and pool else '处理记录中的故障并等待冷却结束，再检查并手动恢复实例。'
                         if manual else '仅冷却受影响账号，其他健康账号继续；全部账号冷却则自动等待。到期自动单任务低频恢复，无需手动恢复；仍受共享预算约束，到期不保证源站已恢复。'
                         if pool and pool.get('cooling_sessions') else '临时冷却到期后重新检查资源准入；到期不保证源站已恢复。'))


def _incident(incidents, record):
    failure = public_failure(record.get('failure'))
    identity = record.get('incident_id')
    if not failure or not identity:
        return
    item = incidents.setdefault(identity, dict(incident_id=identity, time=record.get('time', 0),
                                               failure=failure, effects=[]))
    effect = {k: record.get(k) for k in ('layer', 'action', 'until', 'manual')}
    effect['scope'] = safe_text(record.get('scope', ''))[:100]
    if effect not in item['effects']:
        item['effects'].append(effect)
    item['time'] = min(item['time'], record.get('time', 0))
