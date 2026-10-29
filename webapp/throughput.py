"""Instance-local, bounded preparation/transfer pipeline and resource feedback."""
from collections import deque
from contextlib import contextmanager, ExitStack
import json
import hashlib
import math
import os
from pathlib import Path
import time

import psutil

MAX_TRANSFERS = 64
MAX_WORKERS = 96
MIB = 1024 ** 2


class PipelineBusy(RuntimeError):
    """Temporary admission pressure; do not charge an attempt or isolate a node."""
    retry_after = 10


def policy_key(config):
    return [config.get('throughput_mode', 'manual'), int(config.get('worker_count', 1)),
            int(config.get('yt5s_browser_count', 2)), int(config.get('bandwidth_mbps', 0)),
            hashlib.sha256(json.dumps({key: config.get(key) for key in ('download_methods', 'download_method',
                'download_channels', 'scheduler_mode', 'download_connection_limit')}, sort_keys=True).encode()).hexdigest()[:16]]


def automatic(config):
    return config.get('throughput_mode', 'manual') == 'auto' or config.get('scheduler_mode') == 'auto'


def _number(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def resource_limits(cgroup='/sys/fs/cgroup'):
    """Respect affinity and cgroup v2 ancestors; v1 limits are also recognized."""
    cpu = float(psutil.cpu_count() or 1)
    try:
        cpu = min(cpu, len(psutil.Process().cpu_affinity()))
    except (AttributeError, psutil.Error):
        pass
    memory = psutil.virtual_memory()
    total, available = memory.total, memory.available
    group = Path(cgroup)
    # Containers commonly mount their cgroup as this root. On an ordinary host,
    # include the process's nested cgroup and every visible ancestor too.
    paths = [group]
    try:
        relative = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines()
                        if line.startswith('0::')).lstrip('/')
        candidate = group / relative
        if candidate.is_dir():
            paths += [candidate, *[p for p in candidate.parents if p != group and group in p.parents]]
    except (OSError, StopIteration):
        pass
    for path in dict.fromkeys(paths):
        try:
            quota, period = (path / 'cpu.max').read_text().split()
            if quota != 'max':
                cpu = min(cpu, int(quota) / int(period))
        except (OSError, ValueError, ZeroDivisionError):
            pass
        limit, current = _number(path / 'memory.max'), _number(path / 'memory.current')
        if limit is not None:
            total = min(total, limit)
            available = min(available, max(0, limit - (current or 0)))
    quota = _number(group / 'cpu/cpu.cfs_quota_us')
    period = _number(group / 'cpu/cpu.cfs_period_us')
    if quota is not None and quota > 0 and period:
        cpu = min(cpu, quota / period)
    limit = _number(group / 'memory/memory.limit_in_bytes')
    current = _number(group / 'memory/memory.usage_in_bytes')
    if limit:
        total, available = min(total, limit), min(available, max(0, limit - (current or 0)))
    return {'cpu_cores': max(.1, cpu), 'memory_total': total, 'memory_available': available}


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def runtime_limits(config, directory):
    requested = max(1, min(MAX_TRANSFERS, int(config.get('worker_count', 1))))
    if not automatic(config):
        return {'transfers': requested, 'workers': requested,
                'browsers': int(config.get('yt5s_browser_count', 2)), 'mode': 'manual'}
    try:
        saved = json.loads((Path(directory) / 'throughput.json').read_text(encoding='utf-8'))
        if (saved['policy'] == policy_key(config) and 0 <= time.time() - saved['time'] < 20
                and 1 <= saved['transfers'] <= requested
                and 1 <= saved['workers'] <= MAX_WORKERS
                and 1 <= saved['browsers'] <= int(config.get('yt5s_browser_count', 2))):
            return saved
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # An absent/stale supervisor snapshot never starts a large unmeasured pool.
    return {'transfers': min(requested, 2), 'workers': min(requested, 2),
            'browsers': 1, 'mode': 'auto', 'reason': '等待资源采样'}


class ThroughputController:
    def __init__(self, directory, clock=time.monotonic):
        self.directory, self.clock = Path(directory), clock
        self.last = 0
        self.adjusted = 0
        self.previous = None
        self.state = None
        self.previous_rate = None
        self.probe_baseline = None
        self.plateau_until = 0
        self.history = deque(maxlen=6)
        self.metric_history = deque(maxlen=12)
        self.metric_previous = None
        self.group_control = {}
        self.pressure_hits = 0
        self.browser_trial = None
        self.browser_until = 0

    def sample(self):
        resources = resource_limits()
        net = psutil.net_io_counters(pernic=True)
        # Avoid counting loopback, Docker bridges and their veth copies on hosts.
        physical = [v for k, v in net.items() if k != 'lo' and not k.startswith(('veth', 'br-', 'docker', 'Loopback'))]
        resources['rx'] = sum(v.bytes_recv for v in physical)
        resources['cpu_percent'] = psutil.cpu_percent(None)
        resources['io_pressure'] = 0.0
        try:
            line = Path('/proc/pressure/io').read_text().splitlines()[0]
            resources['io_pressure'] = float(line.split('avg10=')[1].split()[0])
        except (OSError, ValueError, IndexError):
            pass
        # Host CPU percent alone misses CPU quotas smaller than one host core.
        try:
            content = Path('/sys/fs/cgroup/cpu.stat').read_text()
            resources['cpu_usec'] = int(content.split('usage_usec ')[1].splitlines()[0])
        except (OSError, ValueError, IndexError):
            pass
        return resources

    def update(self, config, stages=(), force=False):
        tick = self.clock()
        if not force and self.state is not None and tick - self.last < 5 and self.state['policy'] == policy_key(config):
            return self.state
        resources = self.sample()
        requested = max(1, min(MAX_TRANSFERS, int(config.get('worker_count', 1))))
        browser_requested = max(1, min(16, int(config.get('yt5s_browser_count', 2))))
        browser_methods = {'yt5s', 'yt1d', 'bergung'} & set(config.get('download_methods') or [config.get('download_method', 'yt_dlp')])
        browser_ceiling = min(browser_requested, max(1, math.floor(resources['cpu_cores'] * .75)),
                              max(1, int(resources['memory_total'] * .3 / (384 * MIB)))) if browser_methods else 1
        same = self.state and self.state['policy'] == policy_key(config)
        if not same:
            self.group_control.clear()
            self.browser_trial = None
            self.probe_baseline = None
        browsers = min(browser_ceiling, self.state['browsers']) if same else min(2, browser_ceiling)
        nominal_workers = max(1, int((resources['memory_total'] * .65 - 256 * MIB -
                                     (browsers * 384 * MIB if browser_methods else 0)) / (128 * MIB)))
        worker_pool = max(2, int(os.environ.get('VIDEO_WORKER_DB_POOL_SIZE', '2'))) + max(0, int(os.environ.get('VIDEO_WORKER_DB_MAX_OVERFLOW', '0')))
        db_worker_cap = max(1, int(os.environ.get('VIDEO_WORKER_DB_CONNECTION_BUDGET', '48')) // worker_pool)
        nominal_workers = min(nominal_workers, db_worker_cap)
        ceiling = min(requested, max(1, nominal_workers - (browsers + 1 if browser_methods else 2)))
        transfers = min(ceiling, self.state['transfers'] if same else max(1, int(resources['cpu_cores'] * 2)))
        counts = {}
        group_active = {}
        for item in stages:
            stage = item.get('stage') if isinstance(item, dict) else item
            counts[stage] = counts.get(stage, 0) + 1
            if isinstance(item, dict) and stage in ('downloading', 'processing') and item.get('upstream_group'):
                group = item['upstream_group']
                group_active[group] = group_active.get(group, 0) + 1
        from webapp.download_metrics import read_counters
        metrics = read_counters(self.directory)
        received = verified = resolved = 0
        group_deltas = {}
        channel_deltas = {}
        if self.metric_previous is not None and tick > self.last:
            for identity, value in metrics.items():
                previous = self.metric_previous.get(identity, {})
                received += max(0, value.get('received_bytes', 0) - previous.get('received_bytes', 0))
                verified += max(0, value.get('verified_bytes', 0) - previous.get('verified_bytes', 0))
                resolved += max(0, value.get('resolved', 0) - previous.get('resolved', 0))
                for group, current in value.get('groups', {}).items():
                    before = previous.get('groups', {}).get(group, {})
                    delta = group_deltas.setdefault(group, {'bytes': 0, 'failed': 0, 'succeeded': 0, 'upstream_failed': 0})
                    for key, name in (('bytes', 'received_bytes'), ('failed', 'failed'), ('succeeded', 'succeeded'), ('upstream_failed', 'upstream_failed')):
                        delta[key] += max(0, current.get(name, 0) - before.get(name, 0))
                for channel, current in value.get('channels', {}).items():
                    before = previous.get('channels', {}).get(channel, {})
                    delta = channel_deltas.setdefault(channel, {})
                    for name in ('received_bytes','verified_bytes','succeeded','failed','resolved','resolve_seconds'):
                        delta[name] = delta.get(name, 0) + max(0, current.get(name, 0) - before.get(name, 0))
            self.metric_history.append((tick - self.last, received, verified, group_deltas, resolved, channel_deltas))
        self.metric_previous = metrics
        metric_seconds = sum(item[0] for item in self.metric_history) or 1
        media_rate = sum(item[1] for item in self.metric_history) / metric_seconds
        verified_rate = sum(item[2] for item in self.metric_history) / metric_seconds
        resolve_rate = sum(item[4] for item in self.metric_history) / metric_seconds
        rate, cpu = 0.0, resources['cpu_percent']
        if self.previous and tick > self.last:
            elapsed = tick - self.last
            self.history.append((elapsed, max(0, resources['rx'] - self.previous['rx'])))
            rate = sum(item[1] for item in self.history) / sum(item[0] for item in self.history)
            if 'cpu_usec' in resources and 'cpu_usec' in self.previous:
                quota_cpu = max(0, resources['cpu_usec'] - self.previous['cpu_usec']) / 1e6 / elapsed / resources['cpu_cores'] * 100
                cpu = max(cpu, quota_cpu)
        self.pressure_hits = self.pressure_hits + 1 if cpu >= 90 or resources['io_pressure'] >= 15 else 0
        pressure = (self.pressure_hits >= 3 or cpu >= 97 or resources['memory_available'] < max(256 * MIB, resources['memory_total'] * .08))
        control_rate = media_rate if metrics else rate
        reason = '有界解析与传输并行'
        changed = False
        if automatic(config) and same and tick - self.adjusted >= 60:
            active = counts.get('downloading', 0) + counts.get('processing', 0)
            target = int(config.get('bandwidth_mbps', 0)) * 1e6 / 8
            if pressure:
                transfers = max(1, math.floor(transfers * .8))
                if cpu >= 95 or resources['memory_available'] < 384 * MIB:
                    browsers = max(1, browsers - 1)
                reason = '资源压力升高，收敛并发'
                self.probe_baseline = None
                self.browser_trial = None
                self.browser_until = tick + 120
                changed = True
            elif self.browser_trial is not None:
                trial = self.browser_trial
                if tick - trial['time'] >= 120:
                    improved = ((control_rate > trial['rate'] * 1.08 and control_rate > 0)
                                or (resolve_rate > trial['resolve_rate'] * 1.15 and resolve_rate > 0))
                    if not improved:
                        browsers = max(1, browsers - 1)
                        self.browser_until = tick + 180
                        reason = '增加浏览器未改善解析或媒体吞吐，恢复原额度'
                    self.browser_trial = None
                else:
                    reason = '观察新增浏览器的解析与下载收益'
                changed = True
            elif (counts.get('waiting_browser', 0) >= 2 and counts.get('waiting_transfer', 0) <= 1
                  and cpu < 65 and resources['memory_available'] > 1024 * MIB and browsers < browser_ceiling
                  and tick >= self.browser_until):
                browsers += 1
                self.browser_trial = {'time': tick, 'rate': control_rate, 'resolve_rate': resolve_rate}
                reason = '解析供给不足且资源充足，试增一个浏览器'
                changed = True
            elif self.probe_baseline is not None and control_rate < self.probe_baseline * 1.05:
                transfers = max(1, transfers - 1)
                self.probe_baseline = None
                self.plateau_until = tick + 120
                reason = '增加并发未提高接收吞吐，等待下一轮探索'
                changed = True
            elif active >= max(1, transfers * .75) and (not target or control_rate < target * .9) and tick >= self.plateau_until:
                self.probe_baseline = control_rate if transfers < ceiling else None
                transfers = min(ceiling, transfers + 1)
                reason = '传输名额持续繁忙，探索增加并发'
                changed = True
            self.adjusted = tick
        from webapp.channels import group_caps, registry
        channels = registry(config)
        channel_rates = {}
        for channel in channels:
            total = {name: sum(item[5].get(channel.id, {}).get(name, 0) for item in self.metric_history)
                     for name in ('received_bytes','verified_bytes','succeeded','failed','resolved','resolve_seconds')}
            channel_rates[channel.id] = dict(label=channel.label,
                receive_mbps=round(total['received_bytes'] * 8 / metric_seconds / 1e6, 2),
                verified_mbps=round(total['verified_bytes'] * 8 / metric_seconds / 1e6, 2),
                succeeded=total['succeeded'], failed=total['failed'], resolved=total['resolved'],
                resolve_seconds=round(total['resolve_seconds'] / total['resolved'], 1) if total['resolved'] else None)
        upstream_caps = {key.split(':', 1)[1]: cap for key, cap in group_caps(channels).items() if key.startswith('upstream:')}
        if 'youtube-origin' in group_active:
            upstream_caps.setdefault('youtube-origin', 32 if config.get('scheduler_mode') == 'auto' else 8)
        groups = {}
        for group, cap in upstream_caps.items():
            cap = min(cap, transfers)
            control = self.group_control.setdefault(group, {'limit': min(2, cap), 'adjusted': tick, 'until': 0})
            limit = min(control['limit'], cap)
            total = {name: sum(item[3].get(group, {}).get(name, 0) for item in self.metric_history)
                     for name in ('bytes', 'failed', 'succeeded', 'upstream_failed')}
            group_rate = total['bytes'] / metric_seconds
            if automatic(config) and tick - control['adjusted'] >= 60 and not changed:
                if pressure or (total['upstream_failed'] >= 3 and total['upstream_failed'] > total['succeeded']):
                    limit = max(1, limit - 1)
                    control.pop('baseline', None)
                    control['until'] = tick + 120
                elif 'baseline' in control:
                    if group_rate < control.pop('baseline') * 1.08:
                        limit = max(1, limit - 1)
                        control['until'] = tick + 120
                elif (group_active.get(group, 0) >= limit and group_rate > 0 and limit < cap
                      and tick >= control['until']):
                    control['baseline'] = group_rate
                    limit += 1
                control['adjusted'] = tick
                changed = True
            control['limit'] = limit
            groups[group] = {'limit': limit if automatic(config) else cap,
                             'active': group_active.get(group, 0), 'receive_mbps': round(group_rate * 8 / 1e6, 2),
                             'failed': total['failed'], 'upstream_failed': total['upstream_failed'], 'succeeded': total['succeeded']}
        if not automatic(config):
            transfers, workers, browsers = requested, requested, browser_requested
            reason = '使用手动并发'
        else:
            # Extra preparation workers are bounded; a ready URL waits inside
            # its owning leased task, never in an unbounded or cross-node cache.
            preparation = browsers + min(2, browsers) if browser_methods else 2
            workers = max(transfers, min(MAX_WORKERS, nominal_workers, transfers + preparation))
            workers = min(workers, nominal_workers, sum(value['limit'] for value in groups.values()) + preparation)
        self.state = {'time': time.time(), 'policy': policy_key(config), 'mode': 'auto' if automatic(config) else 'manual',
                      'transfers': transfers, 'workers': workers, 'browsers': browsers,
                      'receive_mbps': round(rate * 8 / 1e6, 2), 'cpu_percent': round(cpu, 1),
                      'media_receive_mbps': round(media_rate * 8 / 1e6, 2),
                      'verified_mbps': round(verified_rate * 8 / 1e6, 2), 'groups': groups, 'channels':channel_rates,
                      'resolves_per_minute': round(resolve_rate * 60, 2),
                      'io_pressure': resources['io_pressure'], 'db_worker_cap': db_worker_cap,
                      'cpu_cores': round(resources['cpu_cores'], 2),
                      'memory_available_mb': round(resources['memory_available'] / MIB),
                      'stages': counts, 'reason': reason}
        _atomic(self.directory / 'throughput.json', self.state)
        self.previous, self.last = resources, tick
        return self.state


@contextmanager
def transfer_slot(directory, config, on_wait=None, on_ready=None, timeout=120):
    """OS locks bound all transfers and survive neither crashes nor restarts."""
    if not automatic(config):
        yield
        return
    from webapp.disk_budget import file_lock
    root = Path(directory) / 'transfer-slots'
    root.mkdir(parents=True, exist_ok=True)
    deadline, reported = time.monotonic() + timeout, 0
    while True:
        with ExitStack() as selected:
            picked = False
            with file_lock(root / 'admission.lock') as admitted:
                if admitted:
                    limit = runtime_limits(config, directory)['transfers']
                    busy, free = 0, []
                    for index in range(MAX_TRANSFERS):
                        with file_lock(root / f'{index}.lock') as available:
                            if available:
                                free.append(index)
                            else:
                                busy += 1
                    if busy < limit and free:
                        picked = selected.enter_context(file_lock(root / f'{free[0]}.lock'))
            if picked:
                if on_ready:
                    on_ready()
                yield
                return
        if time.monotonic() >= deadline:
            raise PipelineBusy('传输队列繁忙，释放当前准备结果后稍后重试。')
        if on_wait and time.monotonic() - reported >= 5:
            on_wait()
            reported = time.monotonic()
        time.sleep(.2)
