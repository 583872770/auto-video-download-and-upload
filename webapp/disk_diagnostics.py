"""Cached disk explanations; page reads never scan host directories or task history."""
import json
import math
import time
from pathlib import Path

from sqlalchemy import select

from webapp.diagnostics import safe_text

HOST_MAX_AGE = 900
LABELS = {'test': '测试下载临时文件', 'downloads': '下载临时文件', 'cache': '运行缓存',
          'imports': '导入临时文件', 'browsers': '浏览器文件', 'logs': '执行日志'}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def host_usage(directory, now):
    """The optional root-owned collector exposes amounts, never host credentials."""
    try:
        path = Path(directory) / 'host-disk-usage.json'
        with path.open(encoding='utf-8') as stream:
            report = json.loads(stream.read(65537))
        if not isinstance(report, dict) or not isinstance(report.get('sources'), list):
            raise ValueError('Invalid report')
        stamp = report['sampled_at']
        if not number(stamp) or not 0 <= now - stamp <= HOST_MAX_AGE:
            return dict(status='stale', sampled_at=stamp, sources=[])
        local_device = str(Path(directory).stat().st_dev)
        sources = []
        seen = set()
        for item in report['sources'][:12]:
            if not isinstance(item, dict):
                continue
            key = item.get('key')
            if key not in ('mysql_binlog', 'mysql_data', 'deployments', 'docker_images') or key in seen:
                continue
            seen.add(key)
            size = item.get('used_bytes')
            sources.append(dict(key=key, label={'mysql_binlog': 'MySQL 二进制日志', 'mysql_data': '数据库业务文件',
                'deployments': '历史部署文件', 'docker_images': 'Docker 镜像与文件层'}[key],
                used_bytes=int(size) if number(size) else None,
                status='ok' if number(size) else 'unavailable',
                same_filesystem=str(item.get('device')) == local_device,
                reclaimable_bytes=int(item['reclaimable_bytes']) if number(item.get('reclaimable_bytes')) else None,
                retention_days=item.get('retention_days') if number(item.get('retention_days')) else None,
                note=safe_text(str(item.get('note', '')))[:300]))
        return dict(status='ok', sampled_at=stamp, sources=sources)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(status='unavailable', sampled_at=None, sources=[])


def explain(budget, snapshot):
    now = time.time()
    with budget.connect(write=False) as db:
        pressure = budget._get(db, 'pressure', {})
        requirement = db.execute('SELECT * FROM requirements r WHERE task=? AND NOT EXISTS '
            '(SELECT 1 FROM leases l WHERE l.task=r.task AND l.reserved > 0)',
            (pressure.get('task_id') or '',)).fetchone()
        retention = budget._get(db, 'retention_health', {})
        alert = budget._get(db, 'disk_alert', {})
        retained_bytes = db.execute('SELECT COALESCE(SUM(measured),0) FROM leases WHERE task=? AND reserved=0',
            (pressure.get('task_id') or '',)).fetchone()[0]
    total, free = snapshot['total_bytes'], snapshot['free_bytes']
    unused = max(0, snapshot['committed_bytes'] - snapshot['used_bytes'])
    minimum = int(budget.config['disk_min_free_gb']) * 1024 ** 3
    sources = [dict(key=key, label=LABELS[key], used_bytes=value['used_bytes'], status='ok',
                    same_filesystem=True, note='本实例受管文件；预留空间单独计算，不计入实际磁盘用量。')
               for key, value in snapshot['categories'].items()]
    host = host_usage(budget.directory, now)
    sources.extend(host['sources'])
    present = {s['key'] for s in sources}
    for key, label in (('mysql_binlog', 'MySQL 二进制日志'), ('mysql_data', '数据库业务文件'),
                       ('deployments', '历史部署文件'), ('docker_images', 'Docker 镜像与文件层')):
        if key not in present:
            sources.append(dict(key=key, label=label, used_bytes=None, status='unavailable',
                same_filesystem=None, note='等待主机后台采集；尚不能确认占用。'))
    known = sum(s['used_bytes'] or 0 for s in sources if s['same_filesystem'])
    sources.append(dict(key='other', label='其他文件与未分类占用', used_bytes=max(0, total-free-known),
                        status='ok', same_filesystem=True, note='包含尚未采集分类的占用；各项只在同一磁盘内汇总。'))
    sources.sort(key=lambda s: (s['key'] == 'other',
        0 if (s['used_bytes'] or 0) > 0 else 1 if s['used_bytes'] is None else 2, -(s['used_bytes'] or 0)))
    category = snapshot['categories'].get(pressure.get('category'), {})
    protected = snapshot.get('processing_scratch_bytes', 0)
    available = max(0, min(free-unused-minimum,
        category.get('budget_bytes', snapshot['budget_bytes'])-category.get('committed_bytes', 0),
        snapshot['budget_bytes']-snapshot['committed_bytes']) - protected)
    held = bool(requirement and requirement['needed'] > available)
    if not held:
        pressure = {}
    elif protected:
        pressure = {**pressure, 'processing_scratch_bytes': protected}
    if now - retention.get('sampled_at', 0) > 120:
        retention = dict(status='unavailable')
    blocked = (snapshot['low_space'] or snapshot['committed_bytes'] >= snapshot['budget_bytes']
        or any(value['used_bytes'] > value['budget_bytes'] for value in snapshot['categories'].values()))
    severity = 'blocked' if blocked or (pressure and snapshot['waiting_tasks']) else alert.get('severity', 'normal')
    kind = ('space' if snapshot['low_space'] else 'budget' if blocked else
            pressure.get('kind', '') if snapshot['waiting_tasks'] else '')
    headline = ('磁盘空间不足，下载等待恢复' if kind == 'space' else
                '临时文件额度不足，任务等待空间' if kind == 'budget' else
                '可分配预留空间不足，任务等待资源' if severity == 'blocked' else
                '服务器磁盘使用率偏高' if severity in ('warning', 'critical') else '磁盘空间正常')
    if retention.get('completed_count', 0) or retention.get('failed_count', 0) or retention.get('untracked_overdue'):
        if severity == 'normal':
            severity, kind, headline = 'warning', 'retention', '测试临时文件清理需要检查'
            if retention.get('failed_processing_count'):
                headline = f'{retention["failed_processing_count"]} 个失败测试等待处理，可重新下载或清理'
    if snapshot['waiting_tasks'] and not pressure and not blocked:
        severity, kind, headline = 'warning', 'recheck', '任务等待空间复查'
    result = dict(sampled_at=now, severity=severity, kind=kind, headline=headline,
        budget=dict(limit_bytes=snapshot['budget_bytes'], committed_bytes=snapshot['committed_bytes'],
            category_label=LABELS.get(pressure.get('category'), ''),
            category_limit_bytes=category.get('budget_bytes') if pressure else None,
            category_committed_bytes=category.get('committed_bytes') if pressure else None),
        used_percent=round(100*(total-free)/total, 1) if total else 0,
        total_bytes=total, free_bytes=free, minimum_free_bytes=minimum,
        resume_free_bytes=int(budget.config['disk_resume_free_gb'])*1024**3,
        unwritten_reserved_bytes=unused, allocatable_bytes=max(0, free-unused-minimum-protected),
        processing_scratch_bytes=protected,
        waiting_tasks=snapshot['waiting_tasks'], pressure=pressure, retention=retention,
        host_status=host['status'], host_sampled_at=host['sampled_at'], sources=sources)
    from webapp.disk_explanation import describe
    result['download'] = describe(snapshot, result, requirement, retained_bytes)
    return result


def evaluate_alert(previous, percent, now):
    """Two samples 30s apart; recovery uses a five-point hysteresis."""
    if previous and now - previous.get('sampled_at', 0) < 30:
        return previous
    severity = previous.get('severity', 'normal')
    target = 'critical' if percent >= 90 else 'warning' if percent >= 80 else 'normal'
    if severity == 'critical' and percent >= 85:
        target = 'critical'
    elif severity == 'warning' and percent >= 75:
        target = 'warning' if target == 'normal' else target
    count = previous.get('count', 0)+1 if previous.get('candidate') == target else 1
    return dict(sampled_at=now, severity=target if count >= 2 else severity,
                candidate=target, count=min(2, count))


def publish(budget, store, snapshot):
    from webapp.database import settings, instances, counters
    from webapp.notifications import system_issue
    now = time.time()
    percent = 100*(snapshot['total_bytes']-snapshot['free_bytes'])/snapshot['total_bytes'] if snapshot['total_bytes'] else 0
    with budget.connect() as local:
        budget._set(local, 'disk_alert', evaluate_alert(budget._get(local, 'disk_alert', {}), percent, now))
    diagnostic = explain(budget, snapshot)
    identity = store.fleet.id if store.fleet else 'local'
    blocked = snapshot['low_space'] or snapshot['committed_bytes'] >= snapshot['budget_bytes']
    value = dict(blocked=blocked, message=snapshot['message'], time=now, diagnostic=diagnostic)
    with store.connect() as db:
        db.execute(store.db.upsert(settings, {'key': 'disk:'+identity, 'value': json.dumps(value, ensure_ascii=False)}, ['key']))
        if store.fleet:
            node = db.execute(select(instances.c.paused, instances.c.stop_requested).where(instances.c.id == identity)).mappings().one()
            paused = bool(node['paused'] or node['stop_requested'])
        else:
            paused = store.downloads_paused(db)
        selected = db.execute(select(settings.c.value).where(settings.c.key == (
            'workbench_job:'+identity if store.fleet else 'selected_download_job'))).scalar_one_or_none()
        affected = snapshot['waiting_tasks']
        if blocked and selected:
            affected = max(affected, db.execute(select(counters.c.value).where(
                counters.c.scope == 'batch:'+selected, counters.c.status == 'queued')).scalar_one_or_none() or 0)
        decision = diagnostic['download']
        reason = decision['reason']
        next_step = ' '.join(decision['actions'])
        if decision['impact'] != 'unaffected':
            next_step += ('空间条件满足后，仍需手动开启已暂停的任务。' if paused else
                          '空间条件满足后自动重新检查，符合执行条件的任务继续调度。')
        else:
            next_step += '当前没有任务因磁盘资源等待。'
        detail = dict(headline=diagnostic['headline'], phase='实例磁盘资源',
            reason=reason, next_step=next_step,
            affected_tasks=affected, disk=diagnostic, paused=paused)
        if diagnostic['severity'] != 'normal':
            system_issue(store, db, identity, 'disk_resource', detail)
        else:
            from webapp.database import notifications
            from webapp.notifications import group_id, packed
            key = group_id(identity, None, 'disk_resource')
            old = db.execute(select(notifications).where(notifications.c.id == key)).mappings().first()
            if old and old['status'] != 'resolved':
                prior = json.loads(old['detail']).get('disk', {})
                detail.update(headline='磁盘资源已恢复', reason='磁盘可用空间与临时文件额度已满足本次检查。',
                    activity='空间已恢复，任务仍处于暂停。' if paused else '空间已恢复，调度器将按执行条件继续领取任务。',
                    recovery=dict(freed_bytes=max(0, diagnostic['free_bytes']-prior.get('free_bytes', diagnostic['free_bytes'])),
                                  recovered_at=now, paused=paused), code='disk_resource', task_id=None, completed=[], missing=[])
                if prior.get('kind') == 'retention':
                    detail.update(headline='测试临时文件清理告警已解除',
                        reason='本次检查未发现超时测试残留、清理失败或持续未归属占用。',
                        activity='清理检查已恢复正常；任务继续遵循当前执行与暂停设置。')
                db.execute(notifications.update().where(notifications.c.id == key).values(detail=packed(detail)))
            system_issue(store, db, identity, 'disk_resource')
    return diagnostic


def summary(db, identity):
    from webapp.database import settings
    raw = db.execute(select(settings.c.value).where(settings.c.key == 'disk:'+identity)).scalar_one_or_none()
    value = json.loads(raw) if raw else {}
    diagnostic = value.get('diagnostic')
    return diagnostic if diagnostic and 0 <= time.time()-value.get('time', 0) < 90 else None
