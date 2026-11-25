"""Read-only admission diagnostics; no log scans, probes or scheduler mutations."""
import hashlib
import json
import math
from collections import Counter

from sqlalchemy import func, select

from webapp.database import download_permits, download_resources, settings, tasks
from webapp.concurrency_policy import cookie_pool_start_interval, fragment_limit
from webapp.throughput import policy_key
from webapp.decision_diagnostics import public_failure
from webapp.config.schema import DEFAULTS
from webapp.cookie_policy import task_rest_until
from webapp.wait_diagnostics import task_wait


def read_context(db, configurations, stamp):
    """Batch only known resource identities, and count permits with valid leases."""
    scopes = set().union(*(scopes_for(config, identity) for identity, config in configurations))
    keys = {hashlib.sha256(scope.encode()).hexdigest(): scope for scope in scopes}
    states = {keys[key]: json.loads(value) for key, value in db.execute(
        select(download_resources.c.key, download_resources.c.state).where(download_resources.c.key.in_(keys)))}
    usage = Counter()
    for key, value in db.execute(
        select(download_permits.c.resource,
               func.sum(download_permits.c.units)).select_from(
            download_permits.join(tasks, (download_permits.c.task_id == tasks.c.id)
                                  & (download_permits.c.owner == tasks.c.worker_id))).where(
            download_permits.c.resource.in_(keys), tasks.c.status == 'running', tasks.c.lease_until > stamp
        ).group_by(download_permits.c.resource)):
        scope = keys[key]
        usage[scope] += int(value)
    disk_keys = ['disk:' + identity for identity, _ in configurations]
    disk = {key.removeprefix('disk:'): json.loads(value) for key, value in db.execute(
        select(settings.c.key, settings.c.value).where(settings.c.key.in_(disk_keys)))}
    return states, usage, disk, {}


def cookie_allocation(config, identity, pool, allocation, context, stamp, current_tasks, profiles, *,
                      paused=False, online=True, disk_blocked=False, worker_limit=None, worker_used=None,
                      worker_protected=False, start_gate=None, waiting_tasks=(), disk_diagnostic=None):
    """Explain observed admission gates without assigning idle permits to accounts."""
    if not config.get('youtube_cookie_pool'):
        return None
    states = context[0]
    sampled = {p['id']: p for p in (pool or {}).get('profiles', [])}
    names = {p['id']: p['name'] for p in profiles}
    account_metadata = {p['id']: {k:p.get(k) for k in ('account_id','email')} for p in profiles}
    selected = list(dict.fromkeys(config['youtube_cookie_pool']))
    holders = {p['task_id'] for p in allocation['occupied']}
    grouped, unassigned = {}, []
    waiting_ids = {task['id'] for task in waiting_tasks}
    for task in (*current_tasks, *waiting_tasks):
        lease_until = task.get('lease_until', stamp + 1)
        if task['id'] not in waiting_ids and (lease_until is None or lease_until <= stamp):
            continue
        progress = task.get('progress') or {}
        view = {'task_id': task['id'], 'title': progress.get('title') or task.get('title') or
                task.get('video_id') or task['id'][:8], 'stage': progress.get('stage', 'preparing'),
                'speed': progress.get('speed') if progress.get('stage') == 'downloading' and
                all(not progress.get(key) or stamp - progress[key] <= 10
                    for key in ('_reported_at', 'last_bytes_at')) else None,
                'transfer_reserved': task['id'] in holders,
                'wait': task_wait(progress, states=states, identity=identity, stamp=stamp)}
        profile_id = progress.get('cookie_profile_id')
        if profile_id:
            grouped.setdefault(profile_id, []).append(view)
            names.setdefault(profile_id, progress.get('cookie_profile_name') or profile_id)
        else:
            unassigned.append(view)
    # Tasks already admitted may finish after their Cookie was removed from the pool.
    selected.extend(profile_id for profile_id in grouped if profile_id not in selected)
    gate = states.get('youtube-cookie-pool:' + identity, {})
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
            add('disk', (disk_diagnostic or {}).get('headline') or '磁盘空间限制新任务')
        occupied_workers = len(current_tasks) if worker_used is None else worker_used
        if online and worker_limit and occupied_workers >= worker_limit and not grouped.get(profile_id):
            add('worker_limit', f'本机任务进程 {occupied_workers} / {worker_limit}；' +
                ('资源保护已降低上限，恢复条件见上方' if worker_protected else '等待当前任务完成后释放名额'))
        if (start_gate or {}).get('next_at', 0) > stamp:
            add('dispatch_interval', '本机正在等待下一次任务领取', start_gate['next_at'])
        elif (start_gate or {}).get('preparing_until', 0) > stamp:
            # Lease expiry bounds stale hints; it is not an estimated duration.
            add('dispatch_preparing', '本机正在为已领取的视频分配账号')
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
                if gate.get('next_at', 0) > stamp:
                    add('pool_interval', '等待账号池启动间隔', gate['next_at'])
        if not known and online:
            add('unknown', '账号状态尚未取得新鲜采样，原因待确认')
        if profile_id not in config['youtube_cookie_pool']:
            add('removed', '已移出账号池，当前任务结束后不再分配')
        if not reasons and not grouped.get(profile_id):
            add('unassigned', '当前未分配任务；仍需等待队列调度')
        accounts.append({'id': profile_id, 'name': names.get(profile_id) or sample.get('name') or profile_id,
                         **account_metadata.get(profile_id, {}),
                         'known': known, 'state': sample.get('state', 'unknown'),
                         'shared_with': shared, 'shared_name': names.get(shared) or sampled.get(shared, {}).get('name'),
                         'limit': sample.get('limit'), 'active': sample.get('active'),
                         'interval': sample.get('interval'), 'cooldown_kind': sample.get('cooldown_kind'),
                         **{key: sample.get(key) for key in ('hourly_used', 'daily_used', 'hourly_limit', 'daily_limit')},
                         'tasks': grouped.get(profile_id, []), 'reasons': reasons})
    return {'accounts': accounts, 'unassigned_tasks': unassigned,
            'sample_time': (pool or {}).get('sampled_at'),
            'pool_start_interval': cookie_pool_start_interval(config)}


def scopes_for(config, identity):
    return {'source:youtube', 'youtube-cookie-pool:' + identity, 'execution:' + identity}


def diagnose(config, identity, progress, rate, context, stamp, *, global_limit=0, global_running=0,
             paused=False, online=True, current_tasks=(), cookie_profiles=(), pending_count=0, waiting_tasks=()):
    states, usage, disks = context[:3]
    fresh = bool(rate and rate.get('policy') == policy_key(config) and 0 <= stamp - rate.get('time', 0) < 20)
    pool = (rate or {}).get('cookie_pool') if fresh else None
    pool = pool or {'known': False, 'capacity': 0, 'configured_files': len(config.get('youtube_cookie_pool', [])),
                    'reason': '账号池容量尚未取得新鲜采样'}
    requested = pool['capacity']
    workers = int(rate['workers']) if fresh else 0
    upper = min(requested, workers)
    if global_limit:
        upper = min(upper, max(0, global_limit - max(0, global_running - len(progress))))
    disk = disks.get(identity, {})
    disk_fresh = 0 <= stamp - disk.get('time', 0) < 90
    if disk_fresh and disk.get('blocked') or not online or paused:
        upper = 0
    stages = Counter(p.get('stage', 'preparing') for p in progress)
    holders = {task['id']: 1 for task in current_tasks if
        (task.get('progress') or {}).get('transfer_admitted') and
        task.get('lease_until', stamp + 1) and task.get('lease_until', stamp + 1) > stamp}
    by_id = {task['id']: task for task in current_tasks}
    occupied = []
    for task_id in holders:
        task = by_id.get(task_id, {})
        item = task.get('progress') or {}
        occupied.append(dict(task_id=task_id, stage=item.get('stage', ''),
            title=item.get('title') or task.get('title') or task.get('video_id') or task_id[:8],
            cookie_name=item.get('cookie_profile_name') or ''))
    allocation = dict(configured=requested, effective=upper, occupied=occupied,
        unoccupied=max(0, upper - len(occupied)), restricted=max(0, requested - max(upper, len(occupied))),
        outside=dict(Counter((task.get('progress') or {}).get('stage', 'preparing')
                             for task in current_tasks if task['id'] not in holders)),
        over_limit=max(0, len(occupied) - upper))
    gate = (rate.get('download_start_gate') or {}) if fresh and online else {}
    control = (rate or {}).get('worker_control') if fresh and online else None
    if control:
        control = {**control, 'used': len(progress), 'stages': {
            'transferring': stages.get('downloading', 0),
            'processing': stages.get('processing', 0),
            'preparing': max(0, len(progress) - stages.get('downloading', 0) - stages.get('processing', 0))}}
    reason = (rate or {}).get('reason') or pool['reason']
    if not online:
        reason = '实例离线，无法确认当前下载能力'
    elif paused:
        reason = '实例已停止领取新任务'
    elif disk_fresh and disk.get('blocked'):
        reason = disk.get('message') or '磁盘空间限制新任务'
    elif not pool.get('known') or not requested:
        reason = pool['reason']
    elif gate.get('next_at', 0) > stamp:
        reason = '等待下一次任务启动，剩余时间见账号卡片'
    from webapp.decision_diagnostics import explain
    own_states = {key: value for key, value in states.items() if key in scopes_for(config, identity)}
    return dict(time=stamp, online=online, paused=bool(paused), pending_tasks=max(0, int(pending_count)),
        requested=requested, mode='cookie_pool', sample_fresh=fresh, sample_time=(rate or {}).get('time'),
        local_health=(rate or {}).get('risk_local'), transfer_limit=workers, effective_limit=upper,
        global_limit=global_limit, global_running=global_running, transfer_allocation=allocation,
        worker_control=control, database_budget=rate.get('database_budget') if fresh else None,
        fragment_limit=fragment_limit(config), cookie_pool=pool, bottleneck=reason, adjustment_reason=reason,
        cookie_allocation=cookie_allocation(config, identity, pool, allocation, context, stamp,
            current_tasks, cookie_profiles, paused=paused, online=online, waiting_tasks=waiting_tasks,
            disk_diagnostic=disk.get('diagnostic') if disk_fresh else None,
            disk_blocked=bool(disk_fresh and disk.get('blocked')), worker_limit=workers,
            worker_used=len(progress), worker_protected=bool(control and control['resource_limited']), start_gate=gate),
        decisions=explain(own_states, pool, stamp, paused=paused, running=len(progress)),
        running=len(progress), transferring=stages.get('downloading', 0),
        processing=sum(stages.get(s, 0) for s in ('processing', 'waiting_processing', 'cleaning', 'uploading_oss')),
        preparing=sum(v for s, v in stages.items() if s not in ('downloading', 'waiting_transfer', 'processing', 'waiting_processing', 'cleaning', 'uploading_oss')),
        waiting_transfer=stages.get('waiting_transfer', 0), stages=dict(stages),
        wait_resources=dict(Counter(p.get('transfer_wait_resource') or 'resources' for p in progress if p.get('stage') == 'waiting_transfer')),
        youtube_start_interval=int(config.get('youtube_start_interval', 120)),
        youtube_task_rest_interval=int(config.get('youtube_task_rest_interval', 0)),
        youtube_cookie_pool_start_interval=cookie_pool_start_interval(config),
        youtube_waiting=stages.get('waiting_request', 0),
        connection_waiting=stages.get('waiting_connection', 0), cookie_waiting=stages.get('waiting_cookie', 0),
        youtube_source_address=config.get('youtube_source_address', '') if config.get('proxy_mode') == 'direct' else '',
        disk_sample_fresh=disk_fresh, disk_blocked=bool(disk_fresh and disk.get('blocked')),
        disk_message=disk.get('message', '') if disk_fresh else '')
