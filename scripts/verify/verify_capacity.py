"""Bounded, offline capacity probe using real fleet/queue/Cookie code.

Run on the Docker host to test: python scripts/verify/verify_capacity.py
All services use a random internal network, synthetic credentials and disposable
databases. No application volumes, real Cookies or YouTube traffic are used.
This is a lightweight coordinator probe, not twenty full application processes.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from urllib.request import urlopen


def emit(kind, **fields):
    print(json.dumps({'kind': kind, **fields}, ensure_ascii=False), flush=True)


def percentile(values, fraction):
    if not values:
        return None
    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 2)


class Measurements:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts = Counter()
        self.times = defaultdict(list)
        self.errors = []
        self.active = set()
        self.completed = set()
        self.by_instance = Counter()
        self.stale_checked = set()
        self.sql = Counter()
        self.sql_times = defaultdict(list)

    def call(self, operation, function, *args, **kwargs):
        started = time.monotonic()
        try:
            return function(*args, **kwargs)
        finally:
            elapsed = (time.monotonic() - started) * 1000
            with self.lock:
                self.counts[operation] += 1
                self.times[operation].append(elapsed)

    def error(self, operation, error):
        # SQL exceptions embed parameters/URLs. Record only safe class and code.
        original = getattr(error, 'orig', None)
        code = original.args[0] if original and original.args else None
        with self.lock:
            self.errors.append({'operation': operation, 'type': type(error).__name__,
                                'code': code if isinstance(code, int) else None,
                                'frames': traceback.format_tb(error.__traceback__)[-4:]})

    def summary(self):
        with self.lock:
            return {'counts': dict(self.counts), 'latency_ms': {
                key: {'n': len(values), 'p50': percentile(values, .5),
                      'p95': percentile(values, .95), 'p99': percentile(values, .99),
                      'max': round(max(values), 2)} for key, values in self.times.items()},
                    'errors': list(self.errors), 'completed': len(self.completed),
                    'per_instance_completed': dict(self.by_instance), 'sql': dict(self.sql),
                    'sql_latency_ms': {key: {'n': len(values), 'total_ms': round(sum(values), 1),
                        'p95': percentile(values, .95), 'max': round(max(values), 2)}
                        for key, values in self.sql_times.items()}}


def runner(args):
    # These imports only occur inside the constrained application-image container.
    sys.path.insert(0, '/app')
    import psutil
    from sqlalchemy import create_engine, event, func, select, text
    from sqlalchemy.engine import make_url
    from webapp.config_schema import DEFAULTS
    from webapp.cookie_binding import CookieBindingConflict
    from webapp.cookie_pool import CookiePool, capacity_summary
    from webapp.cookie_profiles import CookieProfiles
    from webapp.channel_scheduler import ChannelScheduler
    from webapp.database import cookie_bindings, download_permits, instances, settings, tasks
    from webapp.fleet import Fleet
    from webapp.download_start_gate import DownloadStartGate
    from webapp.store import TaskStore
    from webapp.throughput import PipelineBusy, pool_worker_limits, policy_key, _atomic

    url = make_url(os.environ['CAPACITY_MYSQL_URL'])
    if not url.database.startswith('capacity_') or not url.username.startswith('capacity_'):
        raise ValueError('Only a disposable capacity database/user is accepted')
    measured = Measurements()
    stores, configurations, bases, profiles, samples = [], [], [], [], []
    engines = []
    account_resources = set()
    cancelled = threading.Event()
    current_stage = [None]
    def cancel(signum, frame):
        cancelled.set()
        if current_stage[0] is not None:
            current_stage[0].set()
    signal.signal(signal.SIGTERM, cancel)
    root_context = tempfile.TemporaryDirectory(prefix='capacity-')
    root = Path(root_context.name)

    def make_engine():
        # Independent bounded pools model separate coordinators; TaskStore's
        # normal process-local shared-engine cache would otherwise collapse them.
        engine = create_engine(url, pool_size=2, max_overflow=1, pool_timeout=3,
            pool_pre_ping=True, isolation_level='READ COMMITTED', hide_parameters=True,
            connect_args={'connect_timeout': 5, 'read_timeout': 8, 'write_timeout': 8})
        engines.append(engine)
        @event.listens_for(engine, 'before_cursor_execute')
        def sql_start(connection, cursor, statement, parameters, context, executemany):
            context.capacity_started = time.monotonic()
            with measured.lock:
                measured.sql['statements'] += 1
                if 'FOR UPDATE' in statement.upper():
                    measured.sql['locking_statements'] += 1
        @event.listens_for(engine, 'after_cursor_execute')
        def sql_end(connection, cursor, statement, parameters, context, executemany):
            elapsed = (time.monotonic() - context.capacity_started) * 1000
            table = re.search(r'\b(?:FROM|UPDATE|INTO)\s+`?([a-z_]+)', statement, re.I)
            key = statement.split()[0].upper() + ':' + (table[1] if table else 'other')
            if 'FOR UPDATE' in statement.upper():
                key += ':locking'
            with measured.lock:
                measured.sql_times[key].append(elapsed)
        return engine

    try:
        for index in range(max(args.stages)):
            if cancelled.is_set():
                raise InterruptedError('Capacity preparation interrupted')
            base = root / f'instance-{index:02}'
            store = TaskStore(base / 'state', url.render_as_string(hide_password=False))
            store.db.engine = make_engine()
            config = copy.deepcopy(DEFAULTS)
            config.update(path='videos', proxy_mode='direct', throughput_mode='manual',
                worker_count=args.slots,
                youtube_cookie_pool_worker_limit=args.slots,
                youtube_cookie_pool_preparation_workers=0, builtin_transfer_limit=0,
                builtin_resolver_limit=0, youtube_start_interval=args.account_interval,
                youtube_task_rest_interval=0, youtube_cookie_pool_start_interval=args.shared_interval,
                youtube_cookie_session_limit=1,
                youtube_hourly_start_limit=5000, youtube_daily_start_limit=5000,
                min_free_gb=0, download_method='yt_dlp', download_methods=['yt_dlp'],
                storage_mode='test')
            fleet = Fleet(store, 'hybrid', identity=f'capacity-{index:02}', name=f'capacity-{index:02}')
            fleet.initialize(config, base, base / 'clash')
            library = CookieProfiles(base, store)
            ids, contents = [], []
            for account in range(args.cookies):
                if cancelled.is_set():
                    raise InterruptedError('Capacity preparation interrupted')
                data = ('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\t'
                        f'CAPACITY_ONLY_{index}_{account}_{uuid.uuid4().hex}\n').encode()
                ids.append(library.add(f'synthetic-{index}-{account}', data)['id'])
                contents.append(data)
            config['youtube_cookie_pool'] = ids
            fleet.save_local(config, base, base / 'clash')
            fleet.control(fleet.id, {'action': 'resume'})
            stores.append(store)
            bases.append(base)
            configurations.append(config)
            profiles.append(library)
            samples.append(contents[0])
            for _, _, scope in CookiePool(store, config, base, None).accounts:
                account_resources.add(hashlib.sha256(scope.encode()).hexdigest())
            if (index + 1) % 5 == 0:
                emit('preparation_progress', instances_registered=index + 1)
        stores[0].activate_download_lists()
        # A different instance must not be able to import the same credential.
        rejected = 0
        if len(stores) > 1:
            for index, data in enumerate(samples):
                try:
                    profiles[(index + 1) % len(stores)].add('forbidden-copy', data)
                except CookieBindingConflict:
                    rejected += 1
                else:
                    raise AssertionError('Cross-instance Cookie binding was accepted')
        emit('prepared', instances=len(stores), cookies=args.cookies * len(stores),
             binding_conflicts_rejected=rejected, independent_pools=len(engines),
             pool_size=2, pool_overflow=1, real_downloads=False)
        if args.queue_mode == 'prefill':
            for offset in range(0, args.queue_depth, 200):
                stores[0].create_batch([{'base': str(root), 'config': {},
                    'urls': [f'https://capacity.invalid/prefill/{number}']}
                    for number in range(offset, min(args.queue_depth, offset + 200))])
            emit('queue_prefilled', tasks=args.queue_depth)

        def require(value, message):
            if not value:
                raise AssertionError(message)

        def simulate(index, stage, deadline, stage_stop):
            store, config, base = stores[index], configurations[index], bases[index]
            gate = DownloadStartGate(store.directory)
            active = []
            next_claim = next_beat = next_status = 0
            sequence = 0
            try:
                while not stage_stop.is_set() and (time.monotonic() < deadline or active):
                    now = time.monotonic()
                    if now >= next_beat:
                        require(measured.call('instance_heartbeat', store.fleet.beat), 'Instance lease lost')
                        next_beat = now + 10
                    if now >= next_status:
                        pool = measured.call('pool_refresh', CookiePool, store, config, base, None)
                        snapshot = measured.call('pool_status', pool.status)
                        if args.modeled_instance_memory_mib:
                            # These are virtual coordinators, not twenty 1-GiB
                            # download servers. Model each node's admission budget
                            # while retaining the runner's real Docker hard limits.
                            summary = capacity_summary(snapshot)
                            limits = pool_worker_limits(config, max(1, summary['capacity']),
                                resources={'memory_total': args.modeled_instance_memory_mib * 2**20})
                            _atomic(store.directory / 'throughput.json', dict(limits,
                                time=time.time(), policy=policy_key(config), mode='cookie_pool', cookie_pool=summary))
                        next_status = now + 5
                    for current in list(active):
                        job, stack, pool, selected, end, progress_at, heartbeat_at = current
                        if now >= end:
                            measured.call('permit_release', stack.close)
                            require(measured.call('finish', store.finish, job['id'],
                                result={'test_mode': True, 'message': 'synthetic completion'},
                                worker_id=store.owner), 'Finish rejected')
                            with measured.lock:
                                require(job['id'] not in measured.completed, 'Duplicate completion')
                                measured.active.remove(job['id'])
                                measured.completed.add(job['id'])
                                measured.by_instance[store.fleet.id] += 1
                                check_stale = store.fleet.id not in measured.stale_checked
                                measured.stale_checked.add(store.fleet.id)
                            if check_stale:
                                require(not measured.call('stale_progress_rejected', store.update_progress,
                                    job['id'], {'stage': 'downloading'}, store.owner), 'Late progress accepted')
                                require(not measured.call('stale_heartbeat_rejected', store.heartbeat,
                                    job['id'], store.owner), 'Late heartbeat accepted')
                                require(not measured.call('stale_finish_rejected', store.finish,
                                    job['id'], result={'test_mode': True}, worker_id=store.owner), 'Late finish accepted')
                            active.remove(current)
                        else:
                            if now >= progress_at:
                                require(measured.call('progress', store.update_progress, job['id'],
                                    {'stage': 'downloading', 'bytes': sequence * 1024,
                                     'cookie_profile_id': selected['_cookie_pool_selected'],
                                     'speed': 1024, 'percent': 50}, store.owner), 'Progress rejected')
                                current[5] = now + 2
                            if now >= heartbeat_at:
                                require(measured.call('task_heartbeat', store.heartbeat, job['id'], store.owner),
                                        'Task lease lost')
                                current[6] = now + 10
                    if len(active) < args.slots and now >= next_claim and now < deadline:
                        sequence += 1
                        if args.queue_mode == 'continuous':
                            measured.call('enqueue', store.create, 'download', {'base': str(base),
                                'config': config, 'urls': [f'https://capacity.invalid/{stage}/{index}/{sequence}']})
                        claim_started = time.monotonic()
                        if args.modeled_instance_memory_mib:
                            job, ticket, waiting = measured.call('claim', gate.claim, store, actions=['download'])
                        else:
                            job = measured.call('claim', store.claim, store.owner, actions=['download'])
                            ticket, waiting = None, False
                        next_claim = now + args.start_period
                        if not job:
                            with measured.lock:
                                measured.counts['gate_wait' if waiting else 'claim_empty'] += 1
                            continue
                        require(job['instance_id'] == store.fleet.id, 'Incorrect task instance')
                        with measured.lock:
                            measured.counts['claim_success'] += 1
                            measured.times['claim_success'].append((time.monotonic() - claim_started) * 1000)
                            require(job['id'] not in measured.active, 'Duplicate active claim')
                            require(job['id'] not in measured.completed, 'Claim after completion')
                            measured.active.add(job['id'])
                        stack = ExitStack()
                        try:
                            pool = measured.call('pool_construct', CookiePool, store, config, base, job['id'])
                            selected = measured.call('cookie_admit', stack.enter_context, pool.assignment())
                            if ticket:
                                ticket.finish(max(0, selected['_cookie_pool_next_at'] - time.time()))
                            require(selected['_cookie_pool_selected'] in config['youtube_cookie_pool'],
                                    'Cookie crossed instance')
                            channel_router = ChannelScheduler(store, selected, job['id'])
                            channel = next(c for c in channel_router.channels if c.adapter == 'yt_dlp')
                            def resolve():
                                with channel_router.permit(channel, 'resolve', timeout=0):
                                    pass
                            measured.call('resolve_admit', resolve)
                            measured.call('transfer_admit', stack.enter_context,
                                          channel_router.permit(channel, 'transfer', timeout=0))
                            active.append([job, stack, pool, selected, now + args.task_seconds, now, now + 10])
                        except PipelineBusy as waiting:
                            if ticket:
                                ticket.finish(getattr(waiting, 'start_gate_delay', 1))
                            stack.close()
                            store.release(job['id'], store.owner, 'capacity pacing', delay=2)
                            with measured.lock:
                                measured.active.remove(job['id'])
                                measured.counts['pacing_wait'] += 1
                                measured.counts['wait_' + str(getattr(waiting, 'wait_scope', 'channel'))] += 1
                        except BaseException:
                            if ticket:
                                ticket.finish()
                            stack.close()
                            raise
                    stage_stop.wait(.05)
            except Exception as error:
                measured.error('instance_' + str(index), error)
                stage_stop.set()
            finally:
                for current in active:
                    try:
                        current[1].close()
                        store.release(current[0]['id'], store.owner, 'capacity stopped', delay=0)
                        with measured.lock:
                            measured.active.discard(current[0]['id'])
                    except Exception as error:
                        measured.error('cleanup', error)

        def observe(count, deadline, stage_stop, readings):
            import redis
            redis_client = redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
            while not stage_stop.is_set() and time.monotonic() < deadline:
                try:
                    started = time.monotonic()
                    result = measured.call('fleet_list_compact', stores[0].fleet.list, compact=True)
                    require(len(result['items']) == len(stores), 'Instance list incomplete')
                    with stores[0].connect() as db:
                        status = dict(db.exec_driver_sql("SHOW GLOBAL STATUS WHERE Variable_name IN "
                            "('Threads_connected','Threads_running','Max_used_connections',"
                            "'Innodb_row_lock_waits','Innodb_row_lock_time','Questions','Com_commit',"
                            "'Created_tmp_disk_tables')").all())
                        lock_waits = db.exec_driver_sql('SELECT COUNT(*) FROM performance_schema.data_lock_waits').scalar()
                        lock_transactions = db.exec_driver_sql('SELECT COUNT(DISTINCT REQUESTING_ENGINE_TRANSACTION_ID) '
                            'FROM performance_schema.data_lock_waits').scalar()
                        grouped = db.execute(select(tasks.c.instance_id, func.count()).where(
                            tasks.c.status == 'running').group_by(tasks.c.instance_id)).all()
                        require(all(total <= args.slots for _, total in grouped), 'Instance slots exceeded')
                        cookie_usage = db.execute(select(download_permits.c.resource, func.sum(download_permits.c.units))
                            .where(download_permits.c.resource.in_(account_resources))
                            .group_by(download_permits.c.resource)).all()
                        require(all(total <= 1 for _, total in cookie_usage), 'Cookie session limit exceeded')
                    readings.append({'time': time.time(), 'mysql': {k: int(v) for k, v in status.items()},
                        'lock_waits_now': lock_waits, 'lock_wait_transactions': lock_transactions,
                        'active': sum(n for _, n in grouped),
                        'rss_mib': round(psutil.Process().memory_info().rss / 2**20, 1),
                        'redis': {k: v for k, v in redis_client.info('stats').items()
                                  if k in ('total_commands_processed', 'rejected_connections', 'evicted_keys')}})
                    emit('sample', instances=count, active=readings[-1]['active'],
                         mysql_connections=status.get('Threads_connected'), lock_waits=lock_waits,
                         runner_rss_mib=readings[-1]['rss_mib'])
                    stage_stop.wait(max(.1, 5 - (time.monotonic() - started)))
                except Exception as error:
                    measured.error('observer', error)
                    stage_stop.set()

        reports = []
        stages = [(n, args.stage_seconds) for n in args.stages]
        if args.soak_seconds:
            stages.append((max(args.stages), args.soak_seconds))
        for stage, (count, duration) in enumerate(stages):
            before = measured.summary()
            started = time.monotonic()
            deadline = started + duration
            stop = threading.Event()
            current_stage[0] = stop
            readings = []
            emit('stage_started', stage=stage, instances=count, seconds=duration)
            with ThreadPoolExecutor(max_workers=count + 1) as executor:
                futures = [executor.submit(simulate, i, stage, deadline, stop) for i in range(count)]
                futures.append(executor.submit(observe, count, deadline, stop, readings))
                for future in futures:
                    future.result()
            after = measured.summary()
            current_stage[0] = None
            report = {'stage': stage, 'instances': count, 'requested_seconds': duration,
                'elapsed_seconds': round(time.monotonic() - started, 2),
                'completed': after['completed'] - before['completed'],
                'per_instance_completed': {store.fleet.id:
                    after['per_instance_completed'].get(store.fleet.id, 0) - before['per_instance_completed'].get(store.fleet.id, 0)
                    for store in stores[:count]},
                'operations': {k: after['counts'][k] - before['counts'].get(k, 0) for k in after['counts']},
                'latency_ms': {key: {'n': len(recent), 'p50': percentile(recent, .5),
                    'p95': percentile(recent, .95), 'p99': percentile(recent, .99),
                    'max': round(max(recent), 2)} for key, values in measured.times.items()
                    if (recent := values[before['counts'].get(key, 0):])},
                'readings': readings}
            reports.append(report)
            emit('stage_finished', **{k: v for k, v in report.items() if k not in ('readings', 'latency_ms')})
            if after['errors'] or cancelled.is_set():
                break
        with stores[0].connect() as db:
            final = dict(db.execute(select(tasks.c.status, func.count()).group_by(tasks.c.status)).all())
            permits = db.execute(select(func.count()).select_from(download_permits)).scalar_one()
            binding_count = db.execute(select(func.count()).select_from(cookie_bindings)).scalar_one()
        result = {'version': 1, 'workload': vars(args), 'summary': measured.summary(),
            'interrupted': cancelled.is_set(),
            'stages': reports, 'final': {'tasks': final, 'permits': permits, 'bindings': binding_count},
            'limitations': ['Lightweight coordinators share one Python process with independent SQL pools.',
                'No browser, real downloads, video merging or HTTP frontend load.',
                'Fresh disposable database; no million-row queue or WAN latency.',
                'MySQL/Redis have dedicated test containers and explicit resource limits.']}
        good = (not cancelled.is_set() and not result['summary']['errors'] and permits == 0
                and final.get('running', 0) == 0 and final.get('succeeded', 0) == result['summary']['completed']
                and binding_count == args.cookies * len(stores) and not measured.active
                and all(report['completed'] > 0 for report in reports))
        result['passed'] = good
        Path('/results/report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        emit('result', passed=good, summary=result['summary'], final=result['final'])
        return 0 if good else 1
    finally:
        for store in stores:
            if store.progress_cache.client:
                store.progress_cache.client.close()
        for engine in engines:
            engine.dispose()
        root_context.cleanup()


def launch(args):
    root = Path(__file__).resolve().parents[2]
    run_id = 'capacity_' + uuid.uuid4().hex[:12]
    output = Path(args.output or root / '.runtime' / run_id).resolve()
    output.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(30)
    names = {kind: run_id.replace('_', '-') + '-' + kind for kind in ('mysql', 'redis', 'runner')}
    network = run_id.replace('_', '-')
    created = []
    network_created = False
    samples = []
    process = None
    host_last = None
    host_high = 0
    health_failed = 0
    abort_reason = None

    def host_sample():
        nonlocal host_last, host_high, health_failed
        values = {}
        stat = Path('/proc/stat')
        if stat.exists():
            cpu = [int(n) for n in stat.read_text().splitlines()[0].split()[1:9]]
            if host_last:
                delta = [a - b for a, b in zip(cpu, host_last)]
                total = max(1, sum(delta))
                values['cpu_busy_percent'] = round(100 * (total - delta[3] - delta[4] - delta[7]) / total, 1)
                values['cpu_iowait_percent'] = round(100 * delta[4] / total, 1)
                values['cpu_steal_percent'] = round(100 * delta[7] / total, 1)
            host_last = cpu
            mem = dict((line.split(':')[0], int(line.split()[1]))
                       for line in Path('/proc/meminfo').read_text().splitlines())
            values['available_memory_mib'] = round(mem['MemAvailable'] / 1024, 1)
            psi = Path('/proc/pressure/io')
            if psi.exists():
                full = next(line for line in psi.read_text().splitlines() if line.startswith('full '))
                values['io_full_avg10'] = float(dict(v.split('=') for v in full.split()[1:])['avg10'])
            high = values.get('cpu_busy_percent', 0) > 92 or values.get('io_full_avg10', 0) > 20
            host_high = host_high + 1 if high else 0
            if mem['MemAvailable'] < 2 * 1024**2:
                raise RuntimeError('Host memory protection')
            if host_high >= 3:
                raise RuntimeError('Host CPU or IO pressure protection: ' + json.dumps(values))
        if args.health_url:
            started_health = time.monotonic()
            try:
                with urlopen(args.health_url, timeout=3) as response:
                    healthy = response.status == 200 and json.loads(response.read(65536)).get('status') == 'ok'
                health_failed = 0 if healthy else health_failed + 1
            except Exception:
                health_failed += 1
            values['business_health_ms'] = round((time.monotonic() - started_health) * 1000, 1)
            values['business_health_failures'] = health_failed
            if health_failed >= 3:
                raise RuntimeError('Existing service health protection')
        return values

    def docker(*arguments, timeout=45, check=True):
        result = subprocess.run(['docker', *arguments], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            # Never include command/env values in error messages.
            raise RuntimeError('Docker ' + arguments[0] + ' failed: ' + result.stderr[-500:].replace(password, '[redacted]'))
        return result

    def remove_created():
        failures = []
        for name in reversed(created):
            owned = docker('inspect', '--format', '{{index .Config.Labels "frameflow.capacity"}}',
                           name, timeout=15, check=False)
            if owned.returncode == 0 and owned.stdout.strip() == run_id:
                removed = docker('rm', '-f', '-v', name, timeout=40, check=False)
                if removed.returncode:
                    failures.append(name)
        if network_created:
            removed = docker('network', 'rm', network, timeout=30, check=False)
            if removed.returncode:
                failures.append(network)
        return failures

    try:
        initial = host_sample()
        if initial.get('io_full_avg10', 0) > 20:
            raise RuntimeError('Host IO pressure already exceeds threshold at preflight')
        for image in (args.image, 'mysql:8.4', 'redis:7.4-alpine'):
            docker('image', 'inspect', image)
        docker('network', 'create', '--internal', '--label', 'frameflow.capacity=' + run_id, network)
        network_created = True
        created.append(names['mysql'])
        docker('run', '-d', '--name', names['mysql'], '--network', network,
            '--network-alias', 'mysql', '--label', 'frameflow.capacity=' + run_id,
            '--cpus', '1', '--memory', '1536m', '--memory-swap', '1536m', '--pids-limit', '256',
            '-e', 'MYSQL_ROOT_PASSWORD=' + password, '-e', 'MYSQL_DATABASE=' + run_id,
            '-e', 'MYSQL_USER=' + run_id, '-e', 'MYSQL_PASSWORD=' + password,
            'mysql:8.4', '--innodb-buffer-pool-size=256M', '--max-connections=128',
            '--innodb-lock-wait-timeout=5', '--performance-schema=ON')
        created.append(names['redis'])
        docker('run', '-d', '--name', names['redis'], '--network', network,
            '--network-alias', 'redis', '--label', 'frameflow.capacity=' + run_id,
            '--cpus', '.25', '--memory', '128m', '--memory-swap', '128m', '--pids-limit', '64',
            'redis:7.4-alpine', 'redis-server', '--maxmemory', '64mb', '--maxmemory-policy', 'allkeys-lru',
            '--save', '', '--appendonly', 'no')
        deadline = time.monotonic() + 120
        while True:
            host_sample()
            ready = docker('exec', '-e', 'MYSQL_PWD=' + password, names['mysql'],
                'mysql', '--protocol=TCP', '-h127.0.0.1', '-u' + run_id, '-D' + run_id,
                '-Nse', 'SELECT 1', check=False)
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Disposable MySQL startup timed out')
            time.sleep(5)
        # Monitoring permission is confined to disposable MySQL, with no business data.
        grant = 'GRANT SELECT ON performance_schema.* TO `' + run_id + '`@`%`;'
        docker('exec', '-e', 'MYSQL_PWD=' + password, names['mysql'],
               'mysql', '-uroot', '-e', grant)
        command = ['docker', 'run', '--name', names['runner'], '--network', network,
            '--label', 'frameflow.capacity=' + run_id, '--cpus', '1.5', '--memory', '1024m',
            '--memory-swap', '1024m', '--pids-limit', '128', '--read-only',
            '--tmpfs', '/tmp:rw,size=256m',
            '-v', str(root / 'webapp') + ':/app/webapp:ro',
            '-v', str(root / 'Utils') + ':/app/Utils:ro',
            '-v', str(Path(__file__).resolve()) + ':/app/scripts/verify/verify_capacity.py:ro',
            '-v', str(output) + ':/results:rw', '-w', '/app',
            '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONUNBUFFERED=1',
            '-e', 'REDIS_URL=redis://redis:6379/0',
            '-e', 'CAPACITY_MYSQL_URL=mysql+pymysql://' + run_id + ':' + password + '@mysql/' + run_id,
            '--entrypoint', 'python', args.image, 'scripts/verify/verify_capacity.py', '--runner',
            '--stages', ','.join(map(str, args.stages)), '--cookies', str(args.cookies),
            '--stage-seconds', str(args.stage_seconds), '--soak-seconds', str(args.soak_seconds),
            '--slots', str(args.slots), '--task-seconds', str(args.task_seconds),
            '--start-period', str(args.start_period), '--account-interval', str(args.account_interval),
            '--shared-interval', str(args.shared_interval),
            '--modeled-instance-memory-mib', str(args.modeled_instance_memory_mib),
            '--queue-mode', args.queue_mode, '--queue-depth', str(args.queue_depth)]
        created.append(names['runner'])
        emit('launch', run_id=run_id, output=str(output), cpu_limit_total=2.75,
             memory_limit_mib_total=2688, internal_network=True)
        log = (output / 'runner.log').open('w', encoding='utf-8')
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        started = time.monotonic()
        last_printed = 0
        max_seconds = (args.stage_seconds * len(args.stages) + args.soak_seconds
                       + (args.task_seconds + 45) * (len(args.stages) + 1) + 240)
        while process.poll() is None:
            result = docker('stats', '--no-stream', '--format', '{{json .}}', *created,
                            check=False, timeout=15)
            sample = {'elapsed': round(time.monotonic() - started, 1),
                      'host': host_sample(),
                      'containers': [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]}
            samples.append(sample)
            # Docker memory percentage is relative to each explicit limit.
            if any(float(c.get('MemPerc', '0%').rstrip('%')) > 90 for c in sample['containers']):
                raise RuntimeError('Test container memory exceeded 90 percent of its limit')
            if time.monotonic() - started > max_seconds:
                raise TimeoutError('Capacity probe exceeded its wall-clock budget')
            if time.monotonic() - started - last_printed >= 20:
                emit('progress', elapsed=sample['elapsed'], containers=[{
                    k: c.get(k) for k in ('Name', 'CPUPerc', 'MemUsage', 'MemPerc', 'BlockIO')}
                    for c in sample['containers']])
                last_printed = time.monotonic() - started
            time.sleep(3)
        log.close()
        state = json.loads(docker('inspect', '--format', '{{json .State}}', names['runner']).stdout)
        (output / 'runner-state.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        emit('finished', exit_code=process.returncode, report=str(output / 'report.json'))
        return process.returncode
    except Exception as error:
        abort_reason = type(error).__name__ + ': ' + str(error).replace(password, '[redacted]')
        raise
    finally:
        try:
            if process and process.poll() is None:
                docker('stop', '-t', '20', names['runner'], check=False)
                process.wait(timeout=30)
        finally:
            (output / 'docker-stats.json').write_text(json.dumps(samples, indent=2), encoding='utf-8')
            if abort_reason:
                (output / 'abort.json').write_text(json.dumps({'reason': abort_reason}), encoding='utf-8')
            cleanup_failures = remove_created()
            (output / 'launch-summary.json').write_text(json.dumps({
                'aborted': abort_reason is not None, 'reason': abort_reason,
                'exit_code': process.returncode if process else None,
                'cleanup_failures': cleanup_failures,
                'report_available': (output / 'report.json').exists()}, indent=2), encoding='utf-8')
            emit('cleanup', ok=not cleanup_failures, remaining=cleanup_failures)
            if cleanup_failures:
                raise RuntimeError('Capacity resources need cleanup')


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='youtube-downloader-local-app:latest')
    parser.add_argument('--output')
    parser.add_argument('--health-url', help='Optional existing service health URL, sampled every five seconds')
    parser.add_argument('--stages', type=lambda value: [int(n) for n in value.split(',')], default=[1, 5, 10, 20])
    parser.add_argument('--cookies', type=int, default=20)
    parser.add_argument('--stage-seconds', type=int, default=60)
    parser.add_argument('--soak-seconds', type=int, default=600)
    parser.add_argument('--slots', type=int, default=8)
    parser.add_argument('--task-seconds', type=float, default=20)
    parser.add_argument('--start-period', type=float, default=1)
    parser.add_argument('--account-interval', type=int, default=120)
    parser.add_argument('--shared-interval', type=int, default=5)
    parser.add_argument('--modeled-instance-memory-mib', type=int, default=16384,
                        help='Virtual node memory for admission budgets; 0 uses runner cgroup memory')
    parser.add_argument('--queue-mode', choices=('prefill', 'continuous'), default='prefill')
    parser.add_argument('--queue-depth', type=int, default=10000)
    parser.add_argument('--runner', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.stages or any(n < 1 or n > 20 for n in args.stages):
        parser.error('Stages must be between 1 and 20')
    if not 1 <= args.cookies <= 50 or not 1 <= args.slots <= 20:
        parser.error('Cookies must be 1..50 and slots 1..20')
    if not 1 <= args.stage_seconds <= 1800 or not 0 <= args.soak_seconds <= 43200:
        parser.error('Invalid duration')
    if not 1 <= args.task_seconds <= 120 or not .2 <= args.start_period <= 60:
        parser.error('Invalid task duration/start period')
    if not 0 <= args.modeled_instance_memory_mib <= 131072:
        parser.error('Invalid modeled node memory')
    if not 1 <= args.queue_depth <= 100000:
        parser.error('Queue depth must be 1..100000')
    return args


if __name__ == '__main__':
    options = arguments()
    try:
        sys.exit(runner(options) if options.runner else launch(options))
    except KeyboardInterrupt:
        emit('stopped', reason='interrupted')
        sys.exit(130)
    except Exception as failure:
        emit('failed', error_type=type(failure).__name__)
        sys.exit(1)
