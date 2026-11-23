"""Instance-local, bounded preparation/transfer pipeline and resource feedback."""
from collections import deque
import json
import hashlib
import math
import os
from pathlib import Path
import time
import tempfile

import psutil

from webapp.config.schema import DEFAULTS, PIPELINE_KEY_FIELDS
from webapp.db_pool_calculator import download_task_limit
from webapp.concurrency_policy import MAX_TRANSFERS, MAX_WORKERS, pool_process_limits, fragment_limit
from webapp.resource_pressure import (CPU_HIGH, CPU_RECOVER, IO_HIGH, WORKER_ADJUST_SECONDS,
                                      WORKER_RECOVERY_STEP, WORKER_REDUCTION, memory_thresholds, pressure_state,
                                      sample_io_pressure, worker_control)

MIB = 1024 ** 2


class PipelineBusy(RuntimeError):
    """Temporary admission pressure; do not charge an attempt or isolate a node."""
    retry_after = 10


def policy_key(config):
    return hashlib.sha256(json.dumps({key: config.get(key, DEFAULTS[key]) for key in PIPELINE_KEY_FIELDS},
                                      sort_keys=True).encode()).hexdigest()[:16]


def pool_worker_limits(config, capacity, resources=None):
    """Size processes from static capacity; live permits still govern execution."""
    from webapp.db_pool_calculator import connection_budget
    resources = resources if resources is not None else resource_limits()
    return pool_process_limits(config, capacity, resources['memory_total'], connection_budget(config))


def worker_should_retire(directory, slot):
    """Read a fresh local sizing hint before claiming another task."""
    if slot <= 0:
        return False
    try:
        saved = json.loads((Path(directory) / 'throughput.json').read_text(encoding='utf-8'))
        return 0 <= time.time() - saved['time'] < 20 and slot >= max(1, int(saved['workers']))
    except (OSError, ValueError, KeyError, TypeError):
        return False


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
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError as error:
                if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 4:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def runtime_limits(config, directory):
    target = max(1, min(MAX_TRANSFERS, len(config.get('youtube_cookie_pool', [])) *
                        max(1, int(config.get('youtube_cookie_session_limit', 1)))))
    result = {'transfers': target, 'mode': 'cookie_pool', 'reason': '账号池调度，等待资源采样'}
    result.update(pool_worker_limits(config, target))
    try:
        saved = json.loads((Path(directory) / 'throughput.json').read_text(encoding='utf-8'))
        if saved['policy'] == policy_key(config) and 0 <= time.time() - saved['time'] < 20:
            result.update(saved, workers=max(1, min(result['workers'], int(saved['workers']))))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result


class ThroughputController:
    def __init__(self, directory, clock=time.monotonic):
        self.directory, self.clock = Path(directory), clock
        self.last = 0
        self.previous = None
        self.state = None
        self.history = deque(maxlen=6)
        # Keep a three-minute completion window.  A one-minute window made the
        # displayed finished-output rate collapse whenever a large file was
        # still downloading, even while media bytes were arriving steadily.
        self.metric_history = deque(maxlen=36)
        self.metric_previous = None
        self.pressure_hits = 0
        self.pool_adjusted = 0
        self.pool_last_reduction = None
        self.pressure_source = None

    def sample(self):
        resources = resource_limits()
        net = psutil.net_io_counters(pernic=True)
        # Avoid counting loopback, Docker bridges and their veth copies on hosts.
        physical = [v for k, v in net.items() if k != 'lo' and not k.startswith(('veth', 'br-', 'docker', 'Loopback'))]
        resources['rx'] = sum(v.bytes_recv for v in physical)
        resources['cpu_percent'] = psutil.cpu_percent(None)
        resources.update(sample_io_pressure())
        # Host CPU percent alone misses CPU quotas smaller than one host core.
        try:
            content = Path('/sys/fs/cgroup/cpu.stat').read_text()
            resources['cpu_usec'] = int(content.split('usage_usec ')[1].splitlines()[0])
        except (OSError, ValueError, IndexError):
            pass
        return resources

    def update(self, config, stages=(), force=False, *, cookie_capacity=None):
        tick = self.clock()
        if not force and self.state is not None and tick - self.last < 5 and self.state['policy'] == policy_key(config):
            return self.state
        resources = self.sample()
        cookie_capacity = cookie_capacity or {'known': False, 'capacity': 0,
            'reason': '账号会话容量尚未采样', 'configured_files': len(config.get('youtube_cookie_pool', []))}
        requested = max(1, min(MAX_TRANSFERS, int(cookie_capacity['capacity'])))
        same = self.state and self.state['policy'] == policy_key(config)
        if not same:
            self.pool_last_reduction = None
            self.pressure_hits = 0
        from webapp.db_pool_calculator import connection_budget
        db_budget = connection_budget(config)
        db_worker_cap = db_budget['workers']
        counts = {}
        wait_resources = {}
        ready_ages = []
        for item in stages:
            stage = item.get('stage') if isinstance(item, dict) else item
            counts[stage] = counts.get(stage, 0) + 1
            if isinstance(item, dict) and stage == 'waiting_transfer':
                blocked = item.get('transfer_wait_resource') or 'unknown'
                wait_resources[blocked] = wait_resources.get(blocked, 0) + 1
                started = item.get('stage_started_at')
                if isinstance(started, (int, float)):
                    ready_ages.append(max(0, time.time() - started))
        from webapp.download_metrics import read_counters
        metrics = read_counters(self.directory)
        received = verified = resolved = 0
        if self.metric_previous is not None and tick > self.last:
            for identity, value in metrics.items():
                previous = self.metric_previous.get(identity, {})
                received += max(0, value.get('received_bytes', 0) - previous.get('received_bytes', 0))
                verified += max(0, value.get('verified_bytes', 0) - previous.get('verified_bytes', 0))
                resolved += max(0, value.get('resolved', 0) - previous.get('resolved', 0))
            self.metric_history.append((tick - self.last, received, verified, resolved))
        self.metric_previous = metrics
        metric_seconds = sum(item[0] for item in self.metric_history) or 1
        media_rate = sum(item[1] for item in self.metric_history) / metric_seconds
        verified_rate = sum(item[2] for item in self.metric_history) / metric_seconds
        resolve_rate = sum(item[3] for item in self.metric_history) / metric_seconds
        verified_bytes_window = sum(item[2] for item in self.metric_history)
        rate, cpu = 0.0, resources['cpu_percent']
        if self.previous and tick > self.last:
            elapsed = tick - self.last
            self.history.append((elapsed, max(0, resources['rx'] - self.previous['rx'])))
            rate = sum(item[1] for item in self.history) / sum(item[0] for item in self.history)
            if 'cpu_usec' in resources and 'cpu_usec' in self.previous:
                quota_cpu = max(0, resources['cpu_usec'] - self.previous['cpu_usec']) / 1e6 / elapsed / resources['cpu_cores'] * 100
                cpu = max(cpu, quota_cpu)
        source = resources.get('io_pressure_source')
        if source != self.pressure_source:
            self.pressure_hits = 0
            self.pressure_source = source
        io_pressure = resources.get('io_pressure') or 0
        self.pressure_hits = self.pressure_hits + 1 if cpu >= CPU_HIGH or io_pressure >= IO_HIGH else 0
        protection = pressure_state(resources, cpu, self.pressure_hits)
        pressure = protection['active']
        reason = cookie_capacity.get('reason', '账号池调度')
        pool_limits = pool_worker_limits(config, requested, resources)
        workers = pool_limits['workers']
        auto_reduce = config.get('download_auto_reduce', True)
        if not auto_reduce:
            self.pool_last_reduction = None
            self.pool_adjusted = tick
        elif same:
            previous_workers = min(workers, self.state['workers'])
            workers = previous_workers
            if pressure and tick - self.pool_adjusted >= WORKER_ADJUST_SECONDS:
                workers = max(1, math.floor(previous_workers * WORKER_REDUCTION))
                if workers < previous_workers:
                    self.pool_last_reduction = {'at': time.time(), 'from': previous_workers,
                        'to': workers, 'reasons': protection['reasons']}
            elif (not pressure and cpu < CPU_RECOVER and io_pressure < IO_HIGH
                  and tick - self.pool_adjusted >= WORKER_ADJUST_SECONDS
                  and resources['memory_available'] >= memory_thresholds(resources['memory_total'])[1]):
                workers = min(pool_limits['workers'], previous_workers + WORKER_RECOVERY_STEP)
            if workers != self.state['workers']:
                self.pool_adjusted = tick
        else:
            if resources['memory_available'] < memory_thresholds(resources['memory_total'])[0]:
                workers = min(workers, 2)
                if workers < pool_limits['workers']:
                    self.pool_last_reduction = {'at': time.time(), 'from': pool_limits['workers'],
                        'to': workers, 'reasons': protection['reasons']}
            self.pool_adjusted = tick
        reason += f'；本机下载进程上限 {workers}，额外任务等待空闲进程'
        if not auto_reduce:
            reason += '；自动降低下载进程上限已关闭'
        if workers < pool_limits['workers']:
            reason += '；资源保护已降低上限，压力收敛后逐步恢复进程'
        self.state = {'time': time.time(), 'policy': policy_key(config),
                      'mode': 'cookie_pool',
                      'transfers': workers, 'workers': workers, 'fragment_limit': fragment_limit(config),
                      'receive_mbps': round(rate * 8 / 1e6, 2), 'cpu_percent': round(cpu, 1),
                      'media_receive_mbps': round(media_rate * 8 / 1e6, 2),
                      'verified_mbps': round(verified_rate * 8 / 1e6, 2),
                      'verified_window_seconds': round(metric_seconds, 1),
                      'verified_samples': len(self.metric_history),
                      'verified_bytes_window': int(verified_bytes_window),
                      'resolves_per_minute': round(resolve_rate * 60, 2),
                      'io_pressure': resources['io_pressure'], 'db_worker_cap': db_worker_cap,
                      'cpu_cores': round(resources['cpu_cores'], 2),
                      'memory_available_mb': round(resources['memory_available'] / MIB),
                      'stages': counts, 'ready_transfer_count': counts.get('waiting_transfer', 0),
                      'transfer_wait_resources': wait_resources,
                      'oldest_ready_seconds': round(max(ready_ages), 1) if ready_ages else 0,
                      'reason': reason, 'control_metric': 'media_received_bytes',
                      'cookie_pool': cookie_capacity,
                      'database_budget': db_budget}
        self.state.update(pool_limits, workers=workers, worker_pressure_limited=workers < pool_limits['workers'])
        self.state['worker_control'] = worker_control(workers, pool_limits, resources, cpu, protection,
            self.pool_last_reduction,
            self.state['time'] + max(0, WORKER_ADJUST_SECONDS - (tick - self.pool_adjusted)),
            auto_reduce=auto_reduce)
        self.state.update({key: resources.get(key) for key in ('io_pressure_source', 'io_pressure_host')})
        from webapp.download_start_gate import DownloadStartGate
        self.state['download_start_gate'] = DownloadStartGate(self.directory).status(config, self.state['time'])
        _atomic(self.directory / 'throughput.json', self.state)
        self.previous, self.last = resources, tick
        return self.state
