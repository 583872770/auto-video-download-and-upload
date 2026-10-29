"""Instance-local, bounded preparation/transfer pipeline and resource feedback."""
from collections import deque
from contextlib import contextmanager, ExitStack
import json
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
            int(config.get('yt5s_browser_count', 2)), int(config.get('bandwidth_mbps', 0))]


def automatic(config):
    return config.get('throughput_mode', 'manual') == 'auto'


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
                and saved['transfers'] <= saved['workers'] <= MAX_WORKERS
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
        browser_ceiling = min(browser_requested, max(1, math.floor(resources['cpu_cores'] / 2)),
                              max(1, int(resources['memory_total'] * .3 / (384 * MIB)))) if browser_methods else 1
        same = self.state and self.state['policy'] == policy_key(config)
        browsers = min(browser_ceiling, self.state['browsers']) if same else browser_ceiling
        nominal_workers = max(1, int((resources['memory_total'] * .65 - 256 * MIB -
                                     (browsers * 384 * MIB if browser_methods else 0)) / (128 * MIB)))
        ceiling = min(requested, max(1, nominal_workers - (browsers + 1 if browser_methods else 2)))
        transfers = min(ceiling, self.state['transfers'] if same else max(1, int(resources['cpu_cores'] * 2)))
        counts = {}
        for stage in stages:
            counts[stage] = counts.get(stage, 0) + 1
        rate, cpu = 0.0, resources['cpu_percent']
        if self.previous and tick > self.last:
            elapsed = tick - self.last
            self.history.append((elapsed, max(0, resources['rx'] - self.previous['rx'])))
            rate = sum(item[1] for item in self.history) / sum(item[0] for item in self.history)
            if 'cpu_usec' in resources and 'cpu_usec' in self.previous:
                quota_cpu = max(0, resources['cpu_usec'] - self.previous['cpu_usec']) / 1e6 / elapsed / resources['cpu_cores'] * 100
                cpu = max(cpu, quota_cpu)
        pressure = (cpu >= 90 or resources['io_pressure'] >= 15
                    or resources['memory_available'] < max(256 * MIB, resources['memory_total'] * .08))
        reason = '有界解析与传输并行'
        if automatic(config) and same and tick - self.adjusted >= 30:
            active = counts.get('downloading', 0) + counts.get('processing', 0)
            target = int(config.get('bandwidth_mbps', 0)) * 1e6 / 8
            if pressure:
                transfers = max(1, math.floor(transfers * .8))
                if cpu >= 95 or resources['memory_available'] < 384 * MIB:
                    browsers = max(1, browsers - 1)
                reason = '资源压力升高，收敛并发'
                self.probe_baseline = None
            elif counts.get('waiting_browser', 0) >= 2 and cpu < 65 and resources['memory_available'] > 1024 * MIB and browsers < browser_ceiling:
                browsers += 1
                reason = '解析等待且资源充足，恢复浏览器额度'
            elif self.probe_baseline is not None and rate < self.probe_baseline * 1.05:
                transfers = max(1, transfers - 1)
                self.probe_baseline = None
                self.plateau_until = tick + 120
                reason = '增加并发未提高接收吞吐，等待下一轮探索'
            elif active >= max(1, transfers * .75) and (not target or rate < target * .9) and tick >= self.plateau_until:
                self.probe_baseline = rate if transfers < ceiling else None
                transfers = min(ceiling, transfers + 1)
                reason = '传输名额持续繁忙，探索增加并发'
            self.adjusted = tick
        if not automatic(config):
            transfers, workers, browsers = requested, requested, browser_requested
            reason = '使用手动并发'
        else:
            # Extra preparation workers are bounded; a ready URL waits inside
            # its owning leased task, never in an unbounded or cross-node cache.
            preparation = browsers + min(2, browsers) if browser_methods else 2
            workers = max(transfers, min(MAX_WORKERS, nominal_workers, transfers + preparation))
        self.state = {'time': time.time(), 'policy': policy_key(config), 'mode': config.get('throughput_mode', 'manual'),
                      'transfers': transfers, 'workers': workers, 'browsers': browsers,
                      'receive_mbps': round(rate * 8 / 1e6, 2), 'cpu_percent': round(cpu, 1),
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
