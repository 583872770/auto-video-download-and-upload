"""Durable, grouped download incidents, written by fenced task settlements."""
import hashlib
import json
import time

from flask import abort, jsonify, request
from sqlalchemy import and_, func, or_, select

from webapp.database import (notifications as groups, notification_tasks as members,
    tasks, download_lists, instances, counters, settings,
    delivery_runs, delivery_requirements)
from webapp.diagnostics import safe_text
from webapp.delivery.contracts import LABELS

ESCALATE_SECONDS = 1800
PHASES = {'extracting': '解析视频', 'auth': '检查登录', 'checking_login': '检查登录',
    'checking_environment': '检查下载环境', 'preparing_workspace': '准备临时空间',
    'downloading': '下载传输', 'download': '下载传输', 'subtitle': '字幕下载',
    'processing': '本地处理', 'merging': '合并文件', 'uploading': '上传交付',
    'upload': '上传交付', 'extract': '解析视频', 'transfer': '下载传输',
    'local_processing': '本地处理', 'disk_wait': '检查磁盘空间', 'scheduling_wait': '等待执行条件'}


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def group_id(instance, job, code):
    return hashlib.sha256(packed([instance, job, code]).encode()).hexdigest()


def delivery_detail(db, task_id):
    rows = db.execute(select(delivery_requirements.c.kind, delivery_requirements.c.language,
        delivery_requirements.c.status).where(delivery_requirements.c.task_id == task_id)).mappings().all()
    completed, missing = [], []
    for row in rows:
        label = LABELS.get(row['kind'], row['kind']) + (f' · {row["language"]}' if row['language'] else '')
        if row['status'] == 'done':
            completed.append(label)
        elif row['status'] != 'waived':
            missing.append(label)
    run = db.execute(select(delivery_runs.c.spec, delivery_runs.c.status).where(
        delivery_runs.c.task_id == task_id)).mappings().first()
    if run:
        recorded = {row['kind'] for row in rows}
        missing.extend(LABELS[k] for k in json.loads(run['spec'])['outputs'] if k not in recorded)
    return dict(completed=completed, missing=missing,
                delivery_status=run['status'] if run else None)


def _group(db, store, identity):
    return db.execute(store._locked(select(groups).where(groups.c.id == identity))).mappings().one()


def _remove(store, db, member, now):
    old = _group(db, store, member['group_id'])
    active = max(0, old['active_count'] - 1)
    action = max(0, old['action_count'] - int(member['state'] == 'action'))
    db.execute(members.delete().where(members.c.task_id == member['task_id']))
    values = dict(active_count=active, action_count=action, updated_at=now,
                  status=('action' if action else 'recovering') if active else 'resolved',
                  resolved_at=None if active else now)
    if active:
        latest = db.execute(select(members.c.detail).where(members.c.group_id == old['id'])
            .order_by(members.c.updated_at.desc(), members.c.task_id).limit(1)).scalar_one()
        values['detail'] = latest
    db.execute(groups.update().where(groups.c.id == old['id']).values(**values))


def resolve_task(store, db, task_id, *, code=None):
    member = db.execute(select(members).where(members.c.task_id == task_id)).mappings().first()
    if member and (code is None or json.loads(member['detail'])['code'] == code):
        _remove(store, db, member, time.time())


def observe_task(store, db, row, code, state, detail, retry_at=None):
    """Caller already owns the task row. One active membership per source."""
    now = time.time()
    instance = row.get('instance_id') or row.get('target_instance_id') or 'local'
    identity = group_id(instance, row.get('batch_id'), code)
    old = db.execute(select(members).where(members.c.task_id == row['id'])).mappings().first()
    # Keep group lock order stable when a task moves between causes.
    affected = sorted({identity, *([old['group_id']] if old else [])})
    for key in affected:
        if key == identity:
            store.db.ensure(db, groups, dict(id=identity, instance_id=instance, job_id=row.get('batch_id'),
                code=code, status='resolved', active_count=0, action_count=0, first_seen=now,
                updated_at=now, detail='{}'))
        _group(db, store, key)
    if old and old['group_id'] != identity:
        _remove(store, db, old, now)
        old = None
    current = _group(db, store, identity)
    first = old['first_seen'] if old else now
    escalate = first + ESCALATE_SECONDS if state == 'recovering' and retry_at else None
    if escalate and escalate <= now:
        state, escalate = 'action', None
        detail['prolonged'] = True
    detail = {**detail, 'code': code, 'task_id': row['id'], 'video_id': row.get('video_id'),
              'title': safe_text(row.get('title') or row.get('video_id') or '未命名视频')[:300],
              'retry_at': retry_at, 'task_status': row['status']}
    values = dict(group_id=identity, state=state, first_seen=first, updated_at=now,
                  retry_at=retry_at, escalate_at=escalate, detail=packed(detail))
    if old:
        db.execute(members.update().where(members.c.task_id == row['id']).values(**values))
    else:
        db.execute(members.insert().values(task_id=row['id'], **values))
    count = current['active_count'] + int(not old)
    action = current['action_count'] - int(bool(old) and old['state'] == 'action') + int(state == 'action')
    db.execute(groups.update().where(groups.c.id == identity).values(active_count=count,
        action_count=action, status='action' if action else 'recovering',
        first_seen=current['first_seen'] if current['active_count'] else now,
        updated_at=now, resolved_at=None, detail=packed(detail)))


def retry_task(store, db, row):
    old = db.execute(select(members).where(members.c.task_id == row['id'])).mappings().first()
    if old:
        detail = json.loads(old['detail'])
        running = row['status'] == 'running'
        blocked = ''
        if not running:
            if store.fleet:
                node_id = row.get('target_instance_id') or row.get('instance_id')
                node = db.execute(select(instances).where(instances.c.id == node_id)).mappings().first() if node_id else None
                selected = db.execute(select(settings.c.value).where(
                    settings.c.key == 'workbench_job:' + node_id)).scalar_one_or_none() if node_id else None
                if not node or node['paused'] or node['stop_requested'] or node['error'] or selected != row.get('batch_id'):
                    blocked = '已加入重试队列；请在对应实例选择此任务并开启任务启动。'
            else:
                released = db.execute(select(download_lists.c.released).where(
                    download_lists.c.id == row.get('batch_id'))).scalar_one_or_none()
                selected = store.selected_download_job(db)
                if store.downloads_paused(db) or released == 0 or (selected and selected != row.get('batch_id')):
                    blocked = '已加入重试队列；请选中此任务并开启任务启动。'
        detail.update(activity=blocked or ('正在重新执行，完成后自动归档。' if running else '已加入重试队列，等待执行。'))
        observe_task(store, db, {**row, 'status': 'running' if running else 'queued'}, detail['code'],
                     'action' if blocked else 'recovering', detail)


def forget_tasks(store, db, task_ids):
    """Material reset removes only incident members belonging to deleted work."""
    rows = db.execute(select(members).where(members.c.task_id.in_(task_ids))
        .order_by(members.c.group_id, members.c.task_id)).mappings().all()
    for row in rows:
        _remove(store, db, row, time.time())
    identities = {row['group_id'] for row in rows}
    if identities:
        db.execute(groups.delete().where(groups.c.id.in_(identities), groups.c.active_count == 0))


def settle_task(store, db, row, *, error=None, result=None, progress=None, retry_at=None,
                resource=False, phase=None):
    if row['action'] != 'download':
        return
    progress, result = progress or {}, result or {}
    failure = progress.get('last_failure') or {}
    if error and not failure:
        from webapp.download_policy import failure_evidence
        failure = failure_evidence(RuntimeError(str(error))).public()
    kind = failure.get('kind', '')
    raw = safe_text(str(error or result.get('message') or ''))[:1500]
    outputs = delivery_detail(db, row['id'])
    if not error and row['status'] == 'succeeded' and not outputs['missing']:
        # Dedup skips are expected; source restrictions/missing outputs are not.
        if not result.get('skipped') or result.get('skip_reason') in ('archive', 'deduplicated'):
            resolve_task(store, db, row['id'])
            return
        if '去重' in raw or '已下载' in raw or 'archive' in raw.lower():
            resolve_task(store, db, row['id'])
            return
    if not error and not outputs['missing'] and not result.get('skipped'):
        return
    state = 'recovering' if retry_at and not resource else 'action'
    phase = failure.get('phase') if failure.get('phase') not in (None, 'unknown') else phase or progress.get('stage')
    info = dict(phase=PHASES.get(phase, '阶段未记录'), raw=raw, attempts=row.get('attempts', 0),
                max_attempts=row.get('max_attempts', 0), http_status=failure.get('http_status'), **outputs)
    if resource:
        code, headline, reason, next_step = 'disk', '磁盘空间或临时预算不足', raw, '查看磁盘用量，释放空间或调整预算；满足条件后自动重试。'
        info['disk_pressure'] = progress.get('disk_pressure')
        if (info['disk_pressure'] or {}).get('kind') == 'budget':
            headline = '临时文件预算不足，等待资源'
    elif kind in ('authentication_required', 'verification_required'):
        state = 'action'
        code, headline, reason, next_step = 'account_auth', '账号登录或验证需要处理', '当前账号登录检查未通过或需要网页验证。', '到账号管理完成登录或验证，再检测登录状态。'
    elif kind in ('account_rate_limit', 'request_rate_limit'):
        code, headline, reason, next_step = 'rate_limit', '上游请求受限', '账号限额或请求过于频繁。', '等待冷却后自动重试；重试用尽时需检查账号和请求节奏。'
    elif kind == 'access_denied':
        code, headline, reason, next_step = 'access_denied', '下载请求被拒绝', '上游拒绝了本次下载请求。', '结合出错环节检查账号、网络和下载器状态，再重试该视频。'
    elif kind == 'content_restriction':
        code, headline, reason, next_step = 'content_restriction', '来源内容或格式不可用', '来源存在访问限制、已删除或不提供所选格式。', '查看原始信息，核对来源访问权限与任务交付要求。'
    elif kind == 'storage_failure':
        code, headline, reason, next_step = 'storage', '文件上传未完成', '上传目标暂时不可用或上传请求失败。', '检查任务保存配置和上传目标后重试，保留原交付要求。'
    elif kind == 'transient_failure':
        code, headline, reason, next_step = 'network', '网络请求未完成', '请求超时、连接中断或上游暂时不可用。', '等待自动重试；持续失败时检查网络与代理。'
    elif not error and (outputs['missing'] or result.get('skipped')):
        code, headline, reason, next_step = 'delivery', '所选内容尚未完整交付', '来源缺少所选内容，或执行结束后仍有未完成产物。', '核对缺失项；可重试补交，或在任务中心确认接受来源缺失。'
    elif progress.get('stage') == 'scheduling_wait':
        # Routine start pacing is not an error notification.
        if not any(word in raw for word in ('冷却', '限流', '不可用', '登录', '异常')):
            return
        code, headline, reason, next_step = 'waiting', '执行条件暂未满足', raw, '等待执行条件恢复；持续受阻超过 30 分钟会升级为需处理。'
    else:
        code, headline, reason, next_step = 'download', '下载执行未完成', '本次执行未能完成，请查看原始信息定位原因。', '查看执行日志和任务配置；修复原因后重试。'
    if outputs['completed'] and outputs['missing']:
        headline = '部分内容已交付，仍有缺项'
    if code == 'delivery' and info['phase'] == '阶段未记录':
        info['phase'] = '交付完整性核对'
    info.update(headline=headline, reason=reason, next_step=next_step,
                activity='等待到期后重新检查执行条件。' if state == 'recovering' else '')
    observe_task(store, db, row, code, state, info, retry_at)


def resolve_job(store, db, job, *, removed=False):
    # This index contains incidents only, not the potentially huge task list.
    now = time.time()
    ids = select(groups.c.id).where(groups.c.job_id == job)
    db.execute(members.delete().where(members.c.group_id.in_(ids)))
    db.execute(groups.update().where(groups.c.job_id == job, groups.c.status != 'resolved').values(
        status='resolved', active_count=0, action_count=0, resolved_at=now, updated_at=now))
    if removed:
        db.execute(groups.delete().where(groups.c.job_id == job))


def system_issue(store, db, instance, code, detail=None, job=None):
    """A local resource blocker may prevent every task from being claimed."""
    identity, now = group_id(instance, None, code), time.time()
    old = db.execute(select(groups).where(groups.c.id == identity)).mappings().first()
    if detail is None:
        if old and old['status'] != 'resolved':
            db.execute(groups.update().where(groups.c.id == identity).values(status='resolved',
                active_count=0, action_count=0, resolved_at=now, updated_at=now))
        return
    detail = {**detail, 'code': code, 'task_id': None, 'completed': [], 'missing': []}
    encoded = packed(detail)
    values = dict(id=identity, instance_id=instance, job_id=job, code=code,
        status='action', active_count=1, action_count=1, first_seen=now, updated_at=now, detail=encoded)
    if not old:
        store.db.ensure(db, groups, values)
    elif old['status'] != 'action' or old['detail'] != encoded or old['job_id'] != job:
        db.execute(groups.update().where(groups.c.id == identity).values(**{k: v for k, v in values.items()
            if k not in ('id', 'first_seen')}, resolved_at=None,
            first_seen=old['first_seen'] if old['status'] != 'resolved' else now))


def reconcile(store, db):
    """Bounded timer checks on incident/instance indexes, never the task queue."""
    now = time.time()
    overdue = db.execute(select(members.c.task_id).where(members.c.escalate_at <= now)
        .order_by(members.c.escalate_at, members.c.task_id).limit(100)).scalars().all()
    # Acquire every task before any group; otherwise multiple overdue members
    # of one group could deadlock with a concurrent settlement of the next task.
    due_rows = list(db.execute(store._locked(select(tasks).where(tasks.c.id.in_(overdue))
        .order_by(tasks.c.id))).mappings()) if overdue else []
    due_members = list(db.execute(select(members).where(members.c.task_id.in_(overdue))).mappings()) if overdue else []
    for key in sorted({m['group_id'] for m in due_members}):
        _group(db, store, key)
    known_rows = {r['id']: r for r in due_rows}
    for task_id in sorted(overdue):
        row = known_rows.get(task_id)
        member = db.execute(select(members).where(members.c.task_id == task_id)).mappings().first()
        if not member or not member['escalate_at'] or member['escalate_at'] > now:
            continue
        if not row or row['status'] in ('removed', 'cancelled'):
            resolve_task(store, db, task_id)
        else:
            detail = json.loads(member['detail'])
            detail['prolonged'] = True
            observe_task(store, db, row, detail['code'], 'action', detail, member['retry_at'])
    from webapp.fleet import DOWNLOAD_ROLES, ONLINE_SECONDS
    for node in db.execute(select(instances.c.id, instances.c.heartbeat, instances.c.paused,
            instances.c.error).where(instances.c.role.in_(DOWNLOAD_ROLES))).mappings():
        job = db.execute(select(settings.c.value).where(settings.c.key == 'workbench_job:' + node['id'])).scalar_one_or_none()
        pending = 0
        if job:
            pending = db.execute(select(func.sum(counters.c.value)).where(counters.c.scope == 'batch:' + job,
                counters.c.status.in_(('queued', 'running')))).scalar_one() or 0
        offline = node['heartbeat'] < now - ONLINE_SECONDS
        blocked = bool(pending and offline and not node['paused'])
        if blocked:
            detail = dict(headline='下载实例离线',
                code='instance', phase='实例执行', reason='实例心跳已过期，未确认实例在线。',
                next_step='检查实例状态、运行日志和下载环境；恢复后再启动任务。',
                affected_tasks=pending, task_id=None, completed=[], missing=[])
            system_issue(store, db, node['id'], 'instance', detail, job)
        else:
            system_issue(store, db, node['id'], 'instance')
        if pending and node['error']:
            system_issue(store, db, node['id'], 'execution', dict(headline='实例停止领取，执行条件需要处理',
                phase='执行环境与登录检查', reason=safe_text(node['error'])[:500],
                next_step='检查账号、实例下载环境与运行日志；处理后重新启动任务。'), job)
        elif not node['error']:
            system_issue(store, db, node['id'], 'execution')


def public(db, rows):
    node_ids = {r['instance_id'] for r in rows}
    job_ids = {r['job_id'] for r in rows if r['job_id']}
    names = dict(db.execute(select(instances.c.id, instances.c.name).where(instances.c.id.in_(node_ids))).all()) if node_ids else {}
    jobs = {r['id']: dict(r) for r in db.execute(select(download_lists.c.id, download_lists.c.name,
        download_lists.c.customer).where(download_lists.c.id.in_(job_ids))).mappings()} if job_ids else {}
    return [{**dict(r), 'detail': json.loads(r['detail']),
             'instance_name': names.get(r['instance_id'], '本机' if r['instance_id'] == 'local' else r['instance_id']),
             'job_name': jobs.get(r['job_id'], {}).get('name', ''),
             'customer': jobs.get(r['job_id'], {}).get('customer', '')} for r in rows]


def attach_tasks(db, rows):
    if not rows:
        return rows
    details = {r['task_id']: json.loads(r['detail']) for r in db.execute(select(members.c.task_id,
        members.c.detail).where(members.c.task_id.in_([r['id'] for r in rows]))).mappings()}
    return [{**r, 'notification': details.get(r['id'])} for r in rows]


def register_notifications(app, store):
    def scope():
        clauses = []
        for param, column in (('instance_id', groups.c.instance_id), ('job_id', groups.c.job_id)):
            if request.args.get(param):
                clauses.append(column == request.args[param][:64])
        return clauses

    @app.get('/api/notifications')
    def listing():
        state = request.args.get('state', 'action')
        if state not in ('action', 'recovering', 'resolved'):
            raise ValueError('请选择有效通知状态。')
        clauses = scope()
        with store.connect() as db:
            reconcile(store, db)
            counts = {s: 0 for s in ('action', 'recovering', 'resolved')}
            for status, count in db.execute(select(groups.c.status, func.count()).where(*clauses).group_by(groups.c.status)):
                counts[status] = count
            global_counts = dict(counts)
            if clauses:
                global_counts = {s: 0 for s in counts}
                for status, count in db.execute(select(groups.c.status, func.count()).group_by(groups.c.status)):
                    global_counts[status] = count
            if request.args.get('compact') == '1':
                resources = db.execute(select(groups).where(groups.c.code == 'disk_resource',
                    groups.c.status == 'action').order_by(groups.c.updated_at.desc()).limit(5)).mappings().all()
                return jsonify(counts=counts, global_counts=global_counts, items=[], resource_alerts=public(db, resources),
                    next_cursor=None, sampled_at=time.time())
            statement = select(groups).where(*clauses, groups.c.status == state)
            cursor = request.args.get('cursor')
            if cursor:
                try:
                    stamp, identity = json.loads(cursor)
                    stamp = float(stamp)
                    if not __import__('math').isfinite(stamp) or not isinstance(identity, str) or len(identity) != 64:
                        raise ValueError()
                except (ValueError, TypeError, OverflowError):
                    raise ValueError('无效的通知分页位置。') from None
                statement = statement.where(or_(groups.c.updated_at < stamp,
                    and_(groups.c.updated_at == stamp, groups.c.id > identity)))
            rows = db.execute(statement.order_by(groups.c.updated_at.desc(), groups.c.id).limit(31)).mappings().all()
            resources = db.execute(select(groups).where(groups.c.code == 'disk_resource',
                groups.c.status == 'action').order_by(groups.c.updated_at.desc()).limit(5)).mappings().all()
            return jsonify(counts=counts, global_counts=global_counts, items=public(db, rows[:30]),
                resource_alerts=public(db, resources),
                next_cursor=packed([rows[29]['updated_at'], rows[29]['id']]) if len(rows) > 30 else None,
                sampled_at=time.time())

    @app.get('/api/notifications/<identity>/tasks')
    def affected(identity):
        with store.connect() as db:
            group = db.execute(select(groups).where(groups.c.id == identity)).mappings().first()
            if not group:
                abort(404, description='通知不存在。')
            statement = select(members, tasks.c.status).join(tasks, tasks.c.id == members.c.task_id)
            if group['code'] == 'disk_resource':
                statement = statement.join(groups, groups.c.id == members.c.group_id).where(
                    groups.c.instance_id == group['instance_id'], groups.c.code == 'disk', tasks.c.status == 'queued')
            else:
                statement = statement.where(members.c.group_id == identity)
            if request.args.get('cursor'):
                statement = statement.where(members.c.task_id > request.args['cursor'][:32])
            rows = db.execute(statement.order_by(members.c.task_id).limit(51)).mappings().all()
            return jsonify(items=[{**dict(r), 'detail': json.loads(r['detail'])} for r in rows[:50]],
                           next_cursor=rows[49]['task_id'] if len(rows) > 50 else None)
