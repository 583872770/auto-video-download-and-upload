"""Read-only admission diagnostics; no log scans, probes or scheduler mutations."""
import hashlib
import json
import math
from collections import Counter

from sqlalchemy import case, func, select

from webapp.channels import backend_key, group_caps, registry
from webapp.database import download_permits, download_resources, settings, tasks
from webapp.concurrency_policy import (automatic, backend_limits, cookie_pool_start_interval, fragment_connection_budget,
                                      resolver_requirements, resource_capacity, transfer_requirements)
from webapp.throughput import policy_key
from webapp.decision_diagnostics import public_failure
from webapp.config_schema import DEFAULTS
from webapp.cookie_policy import task_rest_until


def scopes_for(config, identity):
    scopes = {'local-transfers:' + identity, 'local-connections:' + identity, 'route-probe:' + identity,
              'youtube-pacing:instance:' + identity, 'youtube-cookie-pool:' + identity,
              'youtube-start:' + identity, 'subtitles:youtube-origin', 'execution:' + identity}
    for channel in registry(config):
        for group in {channel.upstream_group, 'youtube-origin'}:
            scopes.update(('upstream:' + group, 'local-upstream:' + identity + ':' + group))
            if channel.connection_limit is not None:
                scopes.add('connections:' + group)
        scopes.update(('channel:' + channel.id, 'quota:' + channel.quota_group,
                       'resolver:' + channel.resolver_group,
                       'route:' + channel.id, 'backend-route:' + backend_key(channel),
                       'backend-quota:' + backend_key(channel),
                       'backend-resolver:' + backend_key(channel)))
    return scopes


def read_context(db, configurations, stamp):
    """Batch only known resource identities, and count permits with valid leases."""
    scopes = set().union(*(scopes_for(config, identity) for identity, config in configurations))
    keys = {hashlib.sha256(scope.encode()).hexdigest(): scope for scope in scopes}
    local_keys = [key for key, scope in keys.items() if scope.startswith('local-transfers:')]
    holder = case((download_permits.c.resource.in_(local_keys), download_permits.c.task_id), else_='')
    states = {keys[key]: json.loads(value) for key, value in db.execute(
        select(download_resources.c.key, download_resources.c.state).where(download_resources.c.key.in_(keys)))}
    usage, holders = Counter(), {}
    for key, task_id, value in db.execute(
        select(download_permits.c.resource, holder,
               func.sum(download_permits.c.units)).select_from(
            download_permits.join(tasks, (download_permits.c.task_id == tasks.c.id)
                                  & (download_permits.c.owner == tasks.c.worker_id))).where(
            download_permits.c.resource.in_(keys), tasks.c.status == 'running', tasks.c.lease_until > stamp
        ).group_by(download_permits.c.resource, holder)):
        scope = keys[key]
        usage[scope] += int(value)
        if task_id:
            holders.setdefault(scope, {})[task_id] = int(value)
    disk_keys = ['disk:' + identity for identity, _ in configurations]
    disk = {key.removeprefix('disk:'): json.loads(value) for key, value in db.execute(
        select(settings.c.key, settings.c.value).where(settings.c.key.in_(disk_keys)))}
    return states, usage, disk, holders


def transfer_allocation(config, identity, channels, caps, context, effective, current_tasks):
    """Describe actual permits, independently of the last reported task stage."""
    groups = {channel.upstream_group for channel in channels}
    configured = sum(caps['upstream:' + group] for group in groups)
    if not config.get('youtube_cookie_pool'):
        configured = min(configured, int(config.get('worker_count', 1)))
    holders = context[3].get('local-transfers:' + identity, {}) if len(context) > 3 else {}
    tasks_by_id = {task['id']: task for task in current_tasks}
    occupied = []
    for task_id, units in holders.items():
        task = tasks_by_id.get(task_id, {})
        progress = task.get('progress') or {}
        occupied.extend([{'task_id': task_id, 'stage': progress.get('stage', ''),
                          'title': progress.get('title') or task.get('title') or task.get('video_id') or task_id[:8],
                          'cookie_name': progress.get('cookie_profile_name') or ''}] * units)
    order = {'downloading': 0, 'waiting_request': 1, 'extracting': 2}
    occupied.sort(key=lambda task: (order.get(task['stage'], 3), task['task_id']))
    outside = Counter((task.get('progress') or {}).get('stage') or 'preparing'
                      for task in current_tasks if task['id'] not in holders)
    # Existing holders finish normally after a hot reduction of the limit.
    unoccupied = max(0, min(configured, effective) - len(occupied))
    restricted = max(0, configured - len(occupied) - unoccupied)
    return {'configured': configured, 'effective': min(configured, effective),
            'occupied': occupied, 'unoccupied': unoccupied, 'restricted': restricted,
            'outside': dict(outside), 'over_limit': max(0, len(occupied) - configured)}


def cookie_allocation(config, identity, pool, allocation, context, stamp, current_tasks, profiles, *,
                      paused=False, online=True, disk_blocked=False, worker_limit=None, worker_used=None,
                      worker_protected=False, start_gate=None):
    """Explain observed admission gates without assigning idle permits to accounts."""
    if not config.get('youtube_cookie_pool'):
        return None
    states = context[0]
    sampled = {p['id']: p for p in (pool or {}).get('profiles', [])}
    names = {p['id']: p['name'] for p in profiles}
    selected = list(dict.fromkeys(config['youtube_cookie_pool']))
    holders = {p['task_id'] for p in allocation['occupied']}
    grouped, unassigned = {}, []
    for task in current_tasks:
        lease_until = task.get('lease_until', stamp + 1)
        if lease_until is None or lease_until <= stamp:
            continue
        progress = task.get('progress') or {}
        view = {'task_id': task['id'], 'title': progress.get('title') or task.get('title') or
                task.get('video_id') or task['id'][:8], 'stage': progress.get('stage', 'preparing'),
                'speed': progress.get('speed') if progress.get('stage') == 'downloading' and
                all(not progress.get(key) or stamp - progress[key] <= 10
                    for key in ('_reported_at', 'last_bytes_at')) else None,
                'transfer_reserved': task['id'] in holders}
        profile_id = progress.get('cookie_profile_id')
        if profile_id:
            grouped.setdefault(profile_id, []).append(view)
            names.setdefault(profile_id, progress.get('cookie_profile_name') or profile_id)
        else:
            unassigned.append(view)
    # Tasks already admitted may finish after their Cookie was removed from the pool.
    selected.extend(profile_id for profile_id in grouped if profile_id not in selected)
    gate = states.get('youtube-cookie-pool:' + identity, {})
    start = states.get('youtube-start:' + identity, {})
    accounts = []
    for profile_id in selected:
        sample = sampled.get(profile_id, {})
        shared = sample.get('shared_with')
        known = bool(online and pool and pool.get('known') and sample)
        reasons = []

        def add(code, label, until=None):
            reasons.append({'code': code, 'label': label, 'until': until})

        if not online:
            add('offline', '实例离线，当前原因待确认')
        elif paused:
            add('paused', '实例已停止领取新任务')
        if disk_blocked:
            add('disk', '磁盘空间限制新任务')
        occupied_workers = len(current_tasks) if worker_used is None else worker_used
        if online and worker_limit and occupied_workers >= worker_limit and not grouped.get(profile_id):
            add('worker_limit', f'本机任务进程 {occupied_workers} / {worker_limit}；' +
                ('资源保护已降低上限，恢复条件见上方' if worker_protected else '等待当前任务完成后释放名额'))
        if (start_gate or {}).get('next_at', 0) > stamp:
            add('dispatch_interval', '本机正在等待下一次任务领取', start_gate['next_at'])
        elif (start_gate or {}).get('preparing_until', 0) > stamp:
            # Lease expiry bounds stale hints; it is not an estimated duration.
            add('dispatch_preparing', '本机正在为已领取的视频分配账号')
        if gate.get('halted'):
            add('pool_halted', '账号池已拦截，需人工检查后恢复')
        if gate.get('cooldown_until', 0) > stamp:
            add('pool_cooldown', '账号池冷却中', gate['cooldown_until'])
        if known:
            if sample.get('state') == 'unavailable':
                add('unavailable', 'Cookie 文件缺失或无效，请到账号管理中检查')
            elif shared:
                add('shared', '与其他 Cookie 共用账号额度，重复导出不会增加并发')
            else:
                if sample.get('state') == 'manual':
                    add('account_halted', '该账号需人工处理登录或验证，处理后在账号管理检测登录')
                elif sample.get('cooldown_until', 0) > stamp:
                    add('account_cooldown', '账号冷却中', sample['cooldown_until'])
                elif sample.get('state') == 'cooldown':
                    add('account_refresh', '冷却时间已到，等待账号状态更新')
                elif sample.get('limit', 1) > 0 and sample.get('active', 0) >= sample.get('limit', 1):
                    add('account_busy', '账号并发已满，等待当前任务结束')
                if sample.get('budget_until', 0) > stamp:
                    add('account_budget', '账号滚动启动预算已用完', sample['budget_until'])
                elif sample.get('rest_until', 0) > stamp:
                    add('account_rest', '等待任务结束后休息时间', sample['rest_until'])
                    if sample.get('next_start_at', 0) > sample['rest_until']:
                        add('account_interval', '等待两次任务启动最小间隔', sample['next_start_at'])
                elif sample.get('next_start_at', 0) > stamp:
                    add('account_interval', '等待两次任务启动最小间隔', sample['next_start_at'])
                pool_next, start_next = gate.get('next_at', 0), start.get('next_at', 0)
                if pool_next > stamp and start_next > stamp:
                    add('pool_interval', '等待共用启动间隔', max(pool_next, start_next))
                elif pool_next > stamp:
                    add('pool_interval', '等待账号池启动间隔', pool_next)
                elif start_next > stamp:
                    add('start_interval', '等待实例视频启动间隔', start_next)
                if len(allocation['occupied']) >= allocation['effective']:
                    add('transfer_limit', '当前传输额度已用完，等待释放或恢复')
        if not known and online:
            add('unknown', '账号状态尚未取得新鲜采样，原因待确认')
        if profile_id not in config['youtube_cookie_pool']:
            add('removed', '已移出账号池，当前任务结束后不再分配')
        if not reasons:
            add('unassigned', '当前未分配任务；仍需等待队列调度')
        accounts.append({'id': profile_id, 'name': names.get(profile_id) or sample.get('name') or profile_id,
                         'known': known, 'state': sample.get('state', 'unknown'),
                         'shared_with': shared, 'shared_name': names.get(shared) or sampled.get(shared, {}).get('name'),
                         'limit': sample.get('limit'), 'active': sample.get('active'),
                         'interval': sample.get('interval'), 'cooldown_kind': sample.get('cooldown_kind'),
                         **{key: sample.get(key) for key in ('hourly_used', 'daily_used', 'hourly_limit', 'daily_limit')},
                         'tasks': grouped.get(profile_id, []), 'reasons': reasons})
    return {'accounts': accounts, 'unassigned_tasks': unassigned,
            'sample_time': (pool or {}).get('sampled_at'),
            'pool_start_interval': cookie_pool_start_interval(config)}


def diagnose(config, identity, progress, rate, context, stamp, *, global_limit=0, global_running=0, paused=False, online=True, current_tasks=(), cookie_profiles=()):
    states, usage, disks = context[:3]
    auto = automatic(config)
    fresh = bool(rate and rate.get('policy') == policy_key(config) and 0 <= stamp - rate.get('time', 0) < 20)
    pooled = bool(config.get('youtube_cookie_pool'))
    cookie_pool = (rate or {}).get('cookie_pool') if pooled and fresh else None
    if pooled and (not cookie_pool or not cookie_pool.get('known')):
        cookie_pool = {'known': False, 'capacity': 0,
                       'configured_files': len(config['youtube_cookie_pool']),
                       'reason': 'Cookie 会话容量尚未取得新鲜采样，无法确认当前额度'}
    pool_gate = states.get('youtube-cookie-pool:' + identity, {}) if pooled else {}
    if pooled and (pool_gate.get('halted') or pool_gate.get('cooldown_until', 0) > stamp):
        cookie_pool = {**cookie_pool, 'capacity': 0, 'halted': bool(pool_gate.get('halted')),
            'cooling': pool_gate.get('cooldown_until', 0) > stamp,
            'cooldown_until': pool_gate.get('cooldown_until', 0), 'halted_at': pool_gate.get('halted_at'),
            'halt_kind': pool_gate.get('halt_kind'), 'decisions': pool_gate.get('decisions', []),
            'halt_decision': pool_gate.get('halt_decision'),
            'limited_profile_id': pool_gate.get('limited_profile_id'),
            'limited_profile_name': pool_gate.get('limited_profile_name'),
            'reason': 'Cookie 池沿用已保存的停止状态，需检查后人工恢复；本次读取未向源站发起请求。'
                      if pool_gate.get('halted') else 'Cookie 池正在临时冷却，到期后重新检查准入。'}
    requested = cookie_pool['capacity'] if pooled else int(config.get('worker_count', 1))
    transfers = int(rate['transfers']) if auto and fresh else min(requested, 2) if auto else requested
    channels = registry(config)
    caps = group_caps(channels)
    backend_caps = backend_limits(channels)

    def resource(scope, cap):
        state = states.get(scope, {})
        capacity = resource_capacity(cap, state, stamp)
        return {'scope': scope.split(':', 1)[0], 'group': scope.split(':', 1)[1], 'limit': capacity.limit,
                'effective': capacity.effective,
                'used': usage.get(scope, 0), 'cooldown_seconds': math.ceil(capacity.cooling),
                'recovery_seconds': math.ceil(capacity.recovering),
                'last_throttle_status': state.get('cooldown_status') if capacity.cooling or capacity.recovering else None,
                'last_throttle_kind': state.get('cooldown_kind') if capacity.cooling or capacity.recovering else None,
                'last_throttle_at': state.get('last_throttle_at'),
                'last_failure': public_failure(state.get('last_decision', {}).get('failure'))}

    rows, group_limits = [], {}
    local = {'transfers': transfers, 'groups': rate.get('groups', {}) if auto and fresh else {}}
    for channel in channels:
        group = channel.upstream_group
        # Native HTTP ranges and manifest fragments share the same connection budget.
        connection_units = (fragment_connection_budget(config, channel.connection_limit, transfers)
                            if channel.adapter == 'yt_dlp' else None)
        requirements = transfer_requirements(config, identity, channel, caps, local, connection_units or 1)
        by_scope = {scope.split(':', 1)[0]: resource(scope,
                    min(cap, transfers) if scope.startswith('local-upstream:') else cap)
                    for scope, cap, _ in requirements}
        resources = [resource('channel:' + channel.id, channel.task_limit if config.get('scheduler_mode') == 'auto' else 4096),
                     by_scope['upstream'], by_scope['quota'], by_scope['local-upstream'], by_scope['local-transfers']]
        if config.get('scheduler_mode') == 'auto':
            resources.append(resource('backend-quota:' + backend_key(channel), backend_caps[backend_key(channel)][2]))
        effective = min(transfers, *(r['effective'] for r in resources))
        connection = by_scope.get('connections')
        local_connection = by_scope['local-connections']
        if connection_units:
            effective = min(effective, local_connection['effective'] // connection_units)
            if connection is not None:
                effective = min(effective, connection['effective'] // connection_units)
        else:
            effective = min(effective, local_connection['effective'])
            if connection is not None:
                effective = min(effective, connection['effective'])
        resources.extend(([connection] if connection is not None else []) + [local_connection])
        separate_resolver = channel.adapter != 'yt_dlp' or auto or pooled or config.get('youtube_pacing_mode') == 'adaptive'
        resolving = ([resource(scope, cap) for scope, cap, _ in resolver_requirements(channel, caps)
                      if scope.startswith('resolver:')]
                     if separate_resolver else [])
        if config.get('scheduler_mode') == 'auto':
            resolving.extend((resource('backend-resolver:' + backend_key(channel), backend_caps[backend_key(channel)][1]),
                              resource('route:' + channel.id, channel.task_limit),
                              resource('backend-route:' + backend_key(channel), backend_caps[backend_key(channel)][0])))
            if not (states.get('channel:' + channel.id, {}).get('rate', 0) > 0
                    or states.get('channel:' + channel.id, {}).get('successes', 0) > 0):
                resolving.append(resource('route-probe:' + identity, 1))
        cooldown = max(r['cooldown_seconds'] for r in resources)
        recovery = max(r['recovery_seconds'] for r in resources)
        channel_state = states.get('channel:' + channel.id, {})
        rows.append({'id': channel.id, 'label': channel.label, 'group': group,
                     'transfer_limit': channel.transfer_limit, 'resolver_limit': channel.resolver_limit,
                     'resolver_separate': separate_resolver,
                     'connection_limit': connection['limit'] if connection is not None else None,
                     'connection_used': connection['used'] if connection is not None else None,
                     'connection_units': connection_units, 'shared_limit': resources[1]['limit'],
                     'effective_limit': effective, 'occupied': resources[1]['used'],
                     'cooldown_seconds': cooldown, 'recovery_seconds': recovery,
                     'resolver_cooldown_seconds': max((r['cooldown_seconds'] for r in resolving), default=0),
                     'resources': [{**r, 'phase': phase} for phase, items in (('transfer', resources), ('resolve', resolving)) for r in items],
                     'failures': channel_state.get('failures', 0),
                     'status': 'cooldown' if cooldown else 'recovering' if recovery else 'available',
                     'throttle_status': next((r['last_throttle_status'] for r in resources if r['last_throttle_status']), None),
                     'throttle_kind': next((r['last_throttle_kind'] for r in resources if r['last_throttle_kind']), None)})
        group_limits[group] = max(group_limits.get(group, 0), effective)
    upper = min(transfers, sum(group_limits.values()))
    if global_limit:
        upper = min(upper, max(0, global_limit - max(0, global_running - len(progress))))
    stages = Counter(p.get('stage', 'preparing') for p in progress)
    waits = Counter(p.get('transfer_wait_resource') or 'local-transfers' for p in progress if p.get('stage') == 'waiting_transfer')
    disk = disks.get(identity, {})
    disk_fresh = 0 <= stamp - disk.get('time', 0) < 90
    start_gate = (rate.get('download_start_gate') or {}) if pooled and fresh and online else {}
    worker_status = (rate or {}).get('worker_control') if pooled and fresh and online else None
    if pooled:
        upper = min(upper, cookie_pool['capacity'])
        if fresh and rate.get('workers'):
            upper = min(upper, int(rate['workers']))
    if disk_fresh and disk.get('blocked'):
        upper = 0
    if not online:
        bottleneck = '实例离线，无法确认当前下载能力'
    elif cookie_pool and (not cookie_pool.get('known') or cookie_pool.get('halted') or cookie_pool.get('cooling')):
        bottleneck = cookie_pool['reason']
    elif paused:
        bottleneck = '实例已停止领取新任务'
    elif disk_fresh and disk.get('blocked'):
        bottleneck = disk.get('message') or '磁盘空间限制新任务'
    elif start_gate.get('next_at', 0) > stamp:
        bottleneck = '本机正在等待下一次任务领取，剩余时间见账号卡片'
    elif start_gate.get('preparing_until', 0) > stamp:
        bottleneck = '本机正在为已领取的视频分配账号，完成后继续领取'
    elif cookie_pool and cookie_pool['capacity'] <= upper:
        bottleneck = cookie_pool['reason']
    elif global_limit and upper < transfers:
        bottleneck = '渠道、连接预算或全部实例共享上限限制当前额度'
    elif pooled and fresh and rate.get('workers') and upper == int(rate['workers']) and upper < transfers:
        bottleneck = (f"资源保护下本机任务进程动态上限为 {int(rate['workers'])}，恢复条件见进程状态"
                      if worker_status and worker_status['resource_limited'] else
                      f"本机任务进程上限 {int(rate['workers'])} 限制当前额度，仍受配置、内存和数据库预算约束")
    elif upper < transfers:
        bottleneck = '渠道或连接额度低于实例设置'
    elif auto and transfers < requested:
        bottleneck = (rate or {}).get('reason') or '自动调节正在观察下载吞吐与服务器资源'
    else:
        bottleneck = '额度允许执行；实际任务数还取决于队列、启动间隔和视频处理阶段'
    from webapp.youtube_pacing import summary as pacing_summary
    pacing = (pacing_summary(config, states.get('youtube-pacing:instance:' + identity, {}), stamp)
              if config.get('youtube_pacing_mode') == 'adaptive' and not pooled else None)
    from webapp.decision_diagnostics import explain
    own_scopes = scopes_for(config, identity)
    decision_states = {key: value for key, value in states.items()
                       if key in own_scopes and (pooled or not key.startswith('youtube-cookie-pool:'))}
    decisions = explain(decision_states, rows, cookie_pool, pacing, stamp, paused=paused, running=len(progress))
    allocation = transfer_allocation(config, identity, channels, caps, context, upper, current_tasks)
    if worker_status:
        worker_status = {**worker_status, 'used': len(progress),
                         'stages': {'transferring': stages.get('downloading', 0),
                             'processing': sum(stages.get(name, 0) for name in
                                 ('processing', 'waiting_processing', 'cleaning', 'uploading_oss'))}}
        worker_status['stages']['preparing'] = max(0, len(progress) - sum(worker_status['stages'].values()))
    return {'time': stamp, 'online': online, 'paused': bool(paused), 'requested': requested,
            'mode': 'cookie_pool' if pooled else 'auto' if auto else 'manual', 'sample_fresh': fresh,
            'sample_time': rate.get('time') if rate else None,
            'local_health': (rate or {}).get('risk_local'),
            'transfer_limit': transfers, 'effective_limit': upper, 'global_limit': global_limit,
            'transfer_allocation': allocation,
            'worker_control': worker_status,
            'cookie_allocation': cookie_allocation(config, identity, cookie_pool, allocation, context, stamp,
                current_tasks, cookie_profiles, paused=paused, online=online,
                disk_blocked=bool(disk_fresh and disk.get('blocked')),
                worker_limit=int(rate['workers']) if fresh and rate.get('workers') else None,
                worker_used=len(progress), worker_protected=bool(worker_status and worker_status['resource_limited']),
                start_gate=start_gate),
            'global_running': global_running,
            'adjustment_reason': rate.get('reason') if fresh else None,
            'cookie_pool': cookie_pool, 'bottleneck': bottleneck,
            'decisions': decisions,
            'connection_used': usage.get('local-connections:' + identity, 0),
            'connection_budget': int(config.get('download_connection_limit', 32)),
            'running': len(progress), 'transferring': stages.get('downloading', 0),
            'preparing': sum(value for stage, value in stages.items() if stage not in ('downloading', 'waiting_transfer', 'processing', 'waiting_processing', 'cleaning', 'uploading_oss')),
            'processing': sum(stages.get(name, 0) for name in ('processing', 'waiting_processing', 'cleaning', 'uploading_oss')), 'waiting_transfer': stages.get('waiting_transfer', 0),
            'wait_resources': dict(waits), 'stages': dict(stages), 'channels': rows,
            'subtitle_mode': config.get('subtitle_mode', 'optional'),
            'youtube_start_interval': int(config.get('youtube_start_interval', DEFAULTS['youtube_start_interval'])),
            'youtube_task_rest_interval': int(config.get('youtube_task_rest_interval', DEFAULTS['youtube_task_rest_interval'])),
            'youtube_task_rest_until': task_rest_until(config, states.get('youtube-start:' + identity, {})) if not pooled else 0,
            'youtube_cookie_pool_start_interval': cookie_pool_start_interval(config),
            'youtube_pacing': pacing,
            'youtube_waiting': stages.get('waiting_request', 0),
            'cookie_waiting': stages.get('waiting_cookie', 0),
            'youtube_source_address': config.get('youtube_source_address', '') if config.get('proxy_mode') == 'direct' else '',
            'browser_limit': int(rate.get('browsers', config.get('yt5s_browser_count', 2))) if fresh else int(config.get('yt5s_browser_count', 2)),
            'disk_sample_fresh': disk_fresh,
            'disk_blocked': bool(disk_fresh and disk.get('blocked')),
            'disk_message': disk.get('message', '') if disk_fresh else ''}
