"""Scoped I/O sampling and the shared resource protection/reporting contract."""
import math
import os
from pathlib import Path

MIB = 1024 ** 2
IO_HIGH = 15
CPU_HIGH = 90
CPU_CRITICAL = 97
CPU_RECOVER = 80
PRESSURE_SAMPLES = 3
WORKER_ADJUST_SECONDS = 60
WORKER_REDUCTION = .8


def _io_average(path):
    try:
        for line in path.read_text().splitlines():
            if line.startswith('some '):
                fields = dict(field.split('=', 1) for field in line.split()[1:])
                value = float(fields['avg10'])
                return value if math.isfinite(value) and 0 <= value <= 100 else None
    except (OSError, ValueError, KeyError):
        pass
    return None


def sample_io_pressure(cgroup='/sys/fs/cgroup', proc='/proc'):
    """Prefer this process's cgroup, not the host or a containing service group.

    A cgroup namespace can mount our group at the root of /sys/fs/cgroup.
    On ordinary hosts resolve the actual nested process group instead. Legacy
    kernels without scoped PSI fall back to the host with an explicit source.
    """
    group, proc = Path(cgroup), Path(proc)
    host = _io_average(proc / 'pressure/io')
    current = None
    try:
        relative = next(line.split(':', 2)[2] for line in (proc / 'self/cgroup').read_text().splitlines()
                        if line.startswith('0::')).lstrip('/')
        candidate = group / relative
        if candidate.resolve().is_relative_to(group.resolve()) and candidate.is_dir():
            current = candidate
        elif str(os.getpid()) in (group / 'cgroup.procs').read_text().splitlines():
            current = group  # Namespaced mount with an inaccessible host path.
    except (OSError, StopIteration, ValueError):
        pass
    local = _io_average(current / 'io.pressure') if current is not None else None
    return {'io_pressure': local if local is not None else host,
            'io_pressure_source': 'cgroup' if local is not None else 'host' if host is not None else 'unavailable',
            'io_pressure_host': host}


def memory_thresholds(total):
    return max(256 * MIB, total * .08), max(512 * MIB, total * .12)


def pressure_state(resources, cpu, hits):
    low_memory, _ = memory_thresholds(resources['memory_total'])
    reasons = []
    if cpu >= CPU_CRITICAL or (hits >= PRESSURE_SAMPLES and cpu >= CPU_HIGH):
        reasons.append({'code': 'cpu', 'value': round(cpu, 1),
                        'threshold': CPU_CRITICAL if cpu >= CPU_CRITICAL else CPU_HIGH})
    io = resources.get('io_pressure')
    if hits >= PRESSURE_SAMPLES and io is not None and io >= IO_HIGH:
        reasons.append({'code': 'io', 'value': round(io, 2), 'threshold': IO_HIGH,
                        'source': resources.get('io_pressure_source', 'unavailable')})
    if resources['memory_available'] < low_memory:
        reasons.append({'code': 'memory', 'value': round(resources['memory_available'] / MIB),
                        'threshold': math.ceil(low_memory / MIB)})
    return {'active': bool(reasons), 'reasons': reasons, 'high_samples': hits,
            'required_samples': PRESSURE_SAMPLES}


def worker_control(workers, limits, resources, cpu, pressure, last_reduction, eligible_at):
    """Publish only observations and eligibility, never promise the next increase."""
    _, memory = memory_thresholds(resources['memory_total'])
    blocked = []
    if pressure['active']:
        blocked.append('pressure')
    if cpu >= CPU_RECOVER:
        blocked.append('cpu')
    if (resources.get('io_pressure') or 0) >= IO_HIGH:
        blocked.append('io')
    if resources['memory_available'] < memory:
        blocked.append('memory')
    return {'dynamic_limit': workers, 'normal_limit': limits['workers'],
            'configured_limit': limits['configured_worker_limit'],
            'bounds': limits.get('worker_bounds', {}),
            'limit_reasons': limits.get('worker_limit_reasons', []),
            'resource_limited': workers < limits['workers'],
            'pressure': pressure, 'last_reduction': last_reduction,
            'measurements': {'cpu_percent': round(cpu, 1),
                'memory_available_mb': round(resources['memory_available'] / MIB),
                **{key: resources.get(key) for key in ('io_pressure', 'io_pressure_source', 'io_pressure_host')}},
            'recovery': {'eligible_at': eligible_at, 'blocked_by': blocked,
                'interval_seconds': WORKER_ADJUST_SECONDS, 'step': 1,
                'cpu_below': CPU_RECOVER, 'io_below': IO_HIGH,
                'memory_at_least_mb': math.ceil(memory / MIB)}}
