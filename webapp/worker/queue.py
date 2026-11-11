"""Queue claims, task leases, settlement and worker retirement."""
from contextlib import nullcontext
import os
from pathlib import Path
import shutil
import sys
import time
import traceback
import uuid
from webapp.diagnostics import event, error_details, safe_text, diagnostic_context, configure_diagnostics, active_task_log
from webapp.download_policy import failure_evidence, recovery_policy
from webapp.worker.formatting import format_bytes
from webapp.worker.locks import worker_lock
from webapp.worker.recovery import TaskRecovery
from webapp.worker.heartbeat import LeaseHeartbeat


def drain_queue(directory, slot=0, role='all', *, execute_task, store_factory):
    execute = execute_task
    TaskStore = store_factory
    from webapp.disk_budget import DiskPressure, is_disk_full, MIB
    from webapp.downmp3 import Downmp3Error
    from webapp.bergung import BergungError
    from webapp.youtube_auth import YouTubeAuthRequired
    from webapp.environment import DownloadEnvironmentRequired
    from webapp.downloads import DownloadLeaseLost, YouTubeAccountLimited
    from webapp.yt5s import BrowserEnvironmentRequired, WebsiteHTTPError, WebsiteTimeoutError
    # Mark this process as a worker for dynamic connection pool sizing
    os.environ['VIDEO_WORKER_PROCESS'] = 'true'
    store = TaskStore(directory, os.environ.get('VIDEO_WORKER_DATABASE_URL'))
    name = 'subtitles' if slot == -2 else 'maintenance' if slot < 0 else str(slot)
    with worker_lock(store.directory / f'worker-{name}.lock') as acquired:
        if not acquired:
            return
        store.recover(expired_only=True)
        actions = ('subtitles',) if role == 'subtitles' else ('import', 'index') if role == 'maintenance' else ('download',) if role == 'download' else None
        exclude = ('import', 'index', 'subtitles') if role == 'regular' else None
        next_claim_after = 0
        from webapp.download_start_gate import DownloadStartGate
        start_gate = DownloadStartGate(store.directory)
        while True:
            from webapp.throughput import worker_should_retire
            if worker_should_retire(store.directory, slot):
                break  # Complete in-flight work, then retire before claiming again.
            remaining = next_claim_after - time.monotonic()
            if remaining > 0:
                # Delay this idle worker too. Delaying only the released task
                # lets a large queue churn thousands of unrelated task claims.
                # No task lease or DB transaction is held during this wait.
                time.sleep(min(1, remaining))
                continue
            task, start_ticket, waiting_start = start_gate.claim(store, actions=actions, exclude=exclude)
            if not task:
                if waiting_start:
                    time.sleep(1)  # No task lease, DB transaction or workspace held.
                    continue
                break
            config, base = task['payload'].get('config', {}), Path(task['payload']['base'])
            runtime = store.runtime()
            configure_diagnostics(runtime)
            task_context = dict(task_id=task['id'], video_id=task.get('video_id'),
                batch_id=task.get('batch_id'), business_job=task['payload'].get('_business_job'),
                instance_id=task.get('instance_id') or 'local', worker_id=store.owner, worker_slot=slot,
                attempt=task.get('attempts'), max_attempts=task.get('max_attempts'),
                execution_id=uuid.uuid4().hex)
            from webapp.throughput import PipelineBusy, runtime_limits
            if slot > 0 and slot >= runtime_limits(runtime, store.directory)['workers']:
                if start_ticket:
                    start_ticket.finish()
                store.release(task['id'], store.owner, '下载进程数量已调整，任务等待其他进程继续。', delay=0)
                break
            if task['action'] == 'download':
                from Utils.config import video_directory
                from Utils.download_storage import DownloadWorkspace
                workspace = DownloadWorkspace(config, store.directory, task['id'])
                target = store.directory if workspace.test_mode else video_directory(workspace.config, base)
                target.mkdir(parents=True, exist_ok=True)
                reserve_gb = int(runtime.get('min_free_gb', config.get('min_free_gb', 2)))
                reserve = reserve_gb * 1024 ** 3
                if shutil.disk_usage(target).free < reserve:
                    message = f'磁盘剩余空间不足 {reserve_gb} GB，下载已暂停，释放空间后自动继续。'
                    store.release(task['id'], store.owner, message)
                    store.worker_status(store.owner, name, 'paused', message=message)
                    break
                if not workspace.test_mode:
                    store.import_archive(base)
            store.worker_status(store.owner, name, 'running', task['id'])
            heartbeat = LeaseHeartbeat(store, task['id'], name, task_context)
            lease_guard = heartbeat.guard
            heartbeat.start()
            resource_wait = None
            last_progress = {}
            started = time.monotonic()

            def report(value):
                last_progress.update(value)
                if not store.update_progress(task['id'], value, store.owner):
                    raise DownloadLeaseLost('任务租约已失效，当前执行已停止。')


            recovery = TaskRecovery(store, task, config, runtime, last_progress, lease_guard)
            record_failure = recovery.record_failure
            settle_account_failure = recovery.settle_account_failure
            # Redirect OS handles too, so FFmpeg stderr and Python logs stay together.
            sys.stdout.flush()
            sys.stderr.flush()
            from webapp.maintenance import bounded_task_log
            maximum = min(int(runtime.get('max_log_mb', 2)) * MIB,
                          int(runtime.get('disk_log_budget_mb', 256)) * MIB // 64)
            with (diagnostic_context(**task_context),
                  active_task_log(store.directory, slot, task['id']) as log_admitted,
                  bounded_task_log(store.log_path(task['id']), maximum,
                      warning_path=store.directory / 'log-write-warning.json') if log_admitted else nullcontext()):
                try:
                    print(f'[{task["action"]}] 任务开始', flush=True)
                    if not log_admitted:
                        event('task_log_unavailable', fallback='supervisor_log', reason='maintenance_busy_or_write_failed')
                    event('attempt_start', action=task['action'], policy_version=task.get('policy_version'))
                    result = execute(task['action'], task['payload'],
                                     report,
                                     rotation_completed=store.rotation_completed, store=store, worker_slot=max(0, slot), task_id=task['id'],
                                     **({'start_ticket': start_ticket} if start_ticket else {}))
                    if task['action'] == 'download':
                        transferred = last_progress.get('downloaded_bytes', 0)
                        output_bytes = result.get('file_bytes', 0)
                        print(f'任务执行完成。传输数据：{format_bytes(transferred)}；文件大小：{format_bytes(output_bytes)}。', flush=True)
                    else:
                        print('任务执行完成。', flush=True)
                    committed = lease_guard.settle(store.finish, task['id'], result=result, worker_id=store.owner)
                    event('attempt_end', outcome='succeeded' if committed else 'lease_lost',
                          elapsed_seconds=round(time.monotonic() - started, 3),
                          test_mode=bool(result.get('test_mode')), skipped=bool(result.get('skipped')),
                          file_count=result.get('file_count'), file_bytes=result.get('file_bytes'),
                          stage_seconds=last_progress.get('stage_seconds', {}))
                except PipelineBusy as error:
                    start_delay = getattr(error, 'start_gate_delay', error.retry_after)
                    if start_ticket:
                        start_ticket.finish(start_delay)
                    store.update_progress(task['id'], {**last_progress, 'stage': 'scheduling_wait',
                        'speed': None, 'eta': None, 'wait_reason': str(error),
                        'retry_at': time.time() + error.retry_after,
                        'retry_delay_seconds': error.retry_after}, store.owner)
                    lease_guard.settle(store.release, task['id'], store.owner, str(error), delay=error.retry_after)
                    # The task keeps its backoff, but an account-specific wait
                    # must not put every worker to sleep ahead of healthy work.
                    worker_delay = 0 if start_delay == 0 else max(2, min(60, start_delay))
                    next_claim_after = time.monotonic() + worker_delay
                    event('attempt_released', reason='pipeline_backpressure', message=str(error),
                          retry_after=error.retry_after, worker_delay_seconds=worker_delay,
                          wait_scope=getattr(error, 'wait_scope', None))
                    continue
                except DiskPressure as error:
                    resource_wait = str(error)
                    lease_guard.settle(store.release, task['id'], store.owner, str(error), delay=60, resource=True)
                    store.worker_status(store.owner, name, 'disk_wait', message=str(error))
                    print(f'磁盘资源等待：{error}', flush=True)
                    event('attempt_released', reason='disk_pressure', message=str(error), **error_details(error))
                    break
                except (BrowserEnvironmentRequired, DownloadEnvironmentRequired) as error:
                    from webapp.execution import isolate_download_failure
                    isolate_download_failure(store, error)
                    lease_guard.settle(store.release, task['id'], store.owner, str(error), delay=0)
                    print(f'下载环境检查未通过，本机停止领取，任务运行意愿保留：{error}', flush=True)
                    event('attempt_released', reason='environment_required', **error_details(error))
                    break
                except YouTubeAuthRequired as error:
                    record_failure(error)
                    failure = failure_evidence(error)
                    if recovery_policy(failure, {**runtime, **config}).targets == ('session',):
                        settle_account_failure(error)
                        continue
                    if error.retryable:
                        retry_after = (recovery_policy(failure, {**runtime, **config}).delay
                                       if failure.kind == 'request_rate_limit' else 30)
                        message = f'{error} 任务将在 {retry_after} 秒后自动重试，实例继续运行。'
                        store.update_progress(task['id'], {**last_progress, 'stage': 'retrying',
                            'speed': None, 'eta': None, 'retry_delay_seconds': retry_after,
                            'retry_reason': 'youtube_auth_check_unavailable'}, store.owner)
                        lease_guard.settle(store.release, task['id'], store.owner, message, delay=retry_after)
                        print(message, flush=True)
                        event('attempt_released', reason='youtube_auth_check_unavailable',
                              auth_state=error.result['state'], retry_after=retry_after, **error_details(error))
                        continue
                    from webapp.youtube_auth import YouTubeAuth
                    from webapp.execution import isolate_download_failure
                    isolate_download_failure(store, error)
                    if not store.fleet:
                        YouTubeAuth(store).block(error.result)
                    lease_guard.settle(store.release, task['id'], store.owner, str(error), delay=0)
                    print(f'登录检查未通过，队列已停止，任务保留：{error}', flush=True)
                    event('attempt_released', reason='authentication_required', **error_details(error))
                    break
                except YouTubeAccountLimited as error:
                    record_failure(error)
                    if settle_account_failure(error):
                        break
                    continue
                except BaseException as error:
                    record_failure(error)
                    failure = failure_evidence(error)
                    if failure.kind == 'account_rate_limit':
                        if settle_account_failure(error):
                            break
                        continue
                    if failure.kind in ('authentication_required', 'verification_required'):
                        if recovery_policy(failure, {**runtime, **config}).targets == ('session',):
                            settle_account_failure(error)
                            continue
                        from webapp.execution import isolate_download_failure
                        isolate_download_failure(store, error)
                        lease_guard.settle(store.release, task['id'], store.owner, str(error), delay=0)
                        event('attempt_released', reason='authentication_required', **error_details(error))
                        break
                    if is_disk_full(error) or any(term in str(error).lower() for term in ('no space left', 'disk full', 'disk quota exceeded')):
                        resource_wait = '磁盘空间或配额不足，等待释放空间。'
                        lease_guard.settle(store.release, task['id'], store.owner, resource_wait, delay=60, resource=True)
                        store.worker_status(store.owner, name, 'disk_wait', message='磁盘空间或配额不足。')
                        event('attempt_released', reason='disk_full', **error_details(error))
                        break
                    traceback.print_exc()
                    print(f'任务失败：{error}', flush=True)
                    message = safe_text(str(error) or type(error).__name__)[:2000]
                    policy = recovery_policy(failure, {**runtime, **config}, youtube=failure.source != 'provider')
                    server_delay = max(getattr(error, '_download_recovery_delay', policy.delay),
                        failure.retry_after or 0, getattr(error, 'retry_after', 0) or 0) or None
                    if failure.reason in ('no_data', 'incomplete_response'):
                        server_delay = max(server_delay or 0, min(300, 60 * 2 ** max(0, task['attempts'] - 1)))
                    transient = failure.retryable
                    lease_guard.settle(store.finish, task['id'], error=message, worker_id=store.owner, retryable=transient, retry_after=server_delay)
                    current = store.get(task['id'])
                    event('attempt_end', outcome=current['status'] if current else 'unknown',
                          failed_stage=last_progress.get('stage'),
                          elapsed_seconds=round(time.monotonic() - started, 3),
                          stage_seconds=last_progress.get('stage_seconds', {}),
                          retryable=transient, retry_scheduled=bool(current and current['status'] == 'queued'),
                          retry_reason=('transient_error' if transient else 'not_classified_as_transient'),
                          retry_delay_seconds=max(0, round(current.get('available_at', 0) - time.time())) if current and current['status'] == 'queued' else None,
                          **error_details(error))
                    if current and current['status'] == 'queued':
                        print(f'临时错误，已安排自动重试；本次为第 {current["attempts"]}/{current["max_attempts"]} 次尝试。', flush=True)
                    if not isinstance(error, Exception):
                        raise
                finally:
                    if start_ticket:
                        start_ticket.finish()
                    heartbeat.close()
                    store.worker_status(store.owner, name, 'disk_wait' if resource_wait else 'idle', message=resource_wait)
