"""User-facing consequences of disk samples; never changes admission decisions."""
def format_bytes(value):
    size = max(0, value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} B'
        size /= 1024


def describe(snapshot, diagnostic, requirement=None, retained_bytes=0):
    free = snapshot['free_bytes']
    unused = diagnostic['unwritten_reserved_bytes']
    minimum, resume = diagnostic['minimum_free_bytes'], diagnostic['resume_free_bytes']
    waiting = snapshot['waiting_tasks']
    global_limit = (snapshot['low_space'] or snapshot['committed_bytes'] >= snapshot['budget_bytes']
        or any(value['used_bytes'] > value['budget_bytes'] for value in snapshot['categories'].values()))
    demand = requirement['needed'] if requirement and diagnostic['pressure'] and not retained_bytes else None
    # Retained inputs can complete locally without another download allocation.
    # Do not claim an exact restart demand until that recovery is checked.
    rows = []
    for key, label, used, committed, limit in [
        ('total', '全部临时文件', snapshot['used_bytes'], snapshot['committed_bytes'], snapshot['budget_bytes']),
        *[(key, diagnostic_label(key), value['used_bytes'], value['committed_bytes'], value['budget_bytes'])
          for key, value in snapshot['categories'].items()]]:
        available = max(0, limit-committed)
        relevant = key == 'total' or key == diagnostic['pressure'].get('category')
        shortage = max(0, committed + (demand or 0)-limit) if relevant else max(0, used-limit)
        rows.append(dict(key=key, label=label, used_bytes=used, reserved_bytes=max(0, committed-used),
            committed_bytes=committed, limit_bytes=limit, remaining_bytes=available,
            shortfall_bytes=shortage, limiting=bool(shortage or (relevant and committed >= limit))))
    physical_gap = max(0, (resume if snapshot['low_space'] else minimum)-free)
    reservation_gap = max(0, minimum+unused+(demand or 0)-free) if requirement else 0
    limits = [row for row in rows if row['limiting']]
    affected = global_limit or bool(waiting)
    impact = 'new_downloads_paused' if global_limit else 'some_tasks_waiting' if affected else 'unaffected'
    impact_text = ('暂缓启动新下载' if global_limit else
        f'{waiting} 个任务等待空间复查，其他可容纳的下载可继续' if waiting and not diagnostic['pressure'] else
        f'{waiting} 个任务等待空间，其他可容纳的下载可继续' if affected else '当前未因空间限制下载')
    actions = []
    gaps = [physical_gap, reservation_gap, *[row['shortfall_bytes'] for row in limits]]
    shortage = max(gaps)
    if snapshot['low_space']:
        reason = f'磁盘实际可用 {format_bytes(free)}，需恢复到 {format_bytes(resume)} 才能启动新下载。'
        actions.append('先清理可回收文件；仍不足时检查下方占用来源，清理主机文件或扩容磁盘。')
        if limits:
            actions.append('临时文件额度也受限，需同时释放或提高下列受限额度；只增加磁盘空间还不够。')
    elif limits and affected:
        names = '、'.join(row['label'] for row in limits)
        reason = f'{names}的设置额度不足；磁盘本身还有 {format_bytes(free)} 可用。'
        actions.append('等待正在执行的任务处理完成、释放临时文件，或点击“清理可回收文件”。')
        actions.append('需要继续增加并发时，到“空间与日志”中提高受限的临时文件额度；分类额度和总额度都要满足。')
    elif diagnostic['pressure'] and waiting:
        reason = '其他执行已预留部分空间，当前不能满足最近等待任务的申请。'
        actions.append('等待其他执行释放预留；也可减少并发，避免同时申请过多空间。')
    elif waiting:
        reason = '仍有任务记录了空间等待；最近一次申请已不再受限，其余任务需逐个重新检查。'
        actions.append('等待调度器按各任务的空间需求重新检查；当前没有可靠的统一差额，不需要据此重启下载。')
    elif diagnostic['kind'] == 'retention':
        health = diagnostic['retention']
        facts = []
        if health.get('completed_count'):
            facts.append(f'{health["completed_count"]} 个已结束测试仍留有 {format_bytes(health["completed_bytes"])} 临时文件')
        if health.get('pending_processing_count'):
            facts.append(f'其中 {health["pending_processing_count"]} 项仍有本地处理计划，需先完成处理并登记统计')
        if health.get('failed_count'):
            facts.append(f'{health["failed_count"]} 项清理失败：'+('；'.join(health.get('failure_reasons') or []) or '请查看近期日志'))
        if health.get('untracked_overdue'):
            facts.append(f'{format_bytes(health["untracked_bytes"])} 文件尚未确认归属')
        reason = '；'.join(facts)+'。'
        actions.append('点击“清理可回收文件”重试安全清理；仍待本地处理或统计核对的文件会继续保留。')
        if health.get('pending_processing_count'):
            actions.append('仍有本地处理计划的任务需完成处理并登记统计，再清理；持续不减少时查看近期日志排查。')
        if health.get('untracked_overdue'):
            actions.append('未确认归属的文件需人工核对，安全清理不会直接删除。')
    elif diagnostic['severity'] in ('warning', 'critical'):
        reason = f'磁盘已使用 {diagnostic["used_percent"]}%，尚未触发本次空间准入限制。'
        actions.append('建议提前清理可回收文件，并查看下方占用来源；暂不需要为此重启下载。')
    else:
        reason = '磁盘可用空间及当前临时文件额度满足本次检查。'
        actions.append('无需处理。继续按当前下载、并发和暂停设置运行。')
    if not affected:
        shortage = 0
        shortfall_text = '无需补充空间' if diagnostic['kind'] != 'retention' else '无需为本次清理提醒增加空间'
    elif shortage:
        shortfall_text = f'还差约 {format_bytes(shortage)}'
        if sum(bool(gap) for gap in [physical_gap or reservation_gap, *[r['shortfall_bytes'] for r in limits]]) > 1:
            shortfall_text += '；下列限制需同时满足，差额不能简单相加'
    else:
        shortage = None
        shortfall_text = ('已有文件需先核对本地恢复，暂不能确定还需多少空间' if retained_bytes else
            '下一次申请量尚未确定，先释放额度；调度时重新检查')
    resume_text = ('空间满足后自动重新检查并参与调度；如果已人工暂停，仍需手动开启。' if affected else
        '此状态只说明空间条件，账号、网络及人工暂停仍按原设置生效。')
    return dict(impact=impact, impact_text=impact_text, reason=reason, shortfall_bytes=shortage,
        shortfall_text=shortfall_text, actions=actions, resume_text=resume_text, constraints=rows,
        physical_shortfall_bytes=physical_gap, reservation_shortfall_bytes=reservation_gap,
        request_bytes=demand, request_note='按最近一个等待任务的需求和本次采样估算，不代表全部排队任务的空间总和。')


def diagnostic_label(key):
    return {'test': '测试下载', 'downloads': '正式下载临时文件', 'cache': '运行缓存',
        'imports': '导入与上传', 'browsers': '浏览器临时文件', 'logs': '执行日志'}[key]
