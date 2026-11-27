"""Disk reservations, Cookie assignment and temporary workspace lifecycle."""
from pathlib import Path
import time
import uuid
from webapp.diagnostics import event


def execute_task(action, payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0, task_id=None,
            start_ticket=None, *, pipeline):
    _execute = pipeline
    if store and payload.get('_processing'):
        from sqlalchemy import select
        from webapp.database import delivery_work
        from webapp.delivery.processing import run
        from webapp.downloads import DownloadLeaseLost
        # 复查磁盘后读取最新处理计划。上次可能已经登记了产物，并回收了对应的源文件。
        with store.connect() as db:
            work = db.execute(select(delivery_work).where(delivery_work.c.task_id == task_id)).mappings().first()
        if not work:
            raise DownloadLeaseLost('本地处理计划已注销，当前执行已停止。')
        return run(store, task_id, work, report_progress or (lambda value: None), slot=worker_slot)
    if store and action == 'download':
        payload = store.effective_download_payload(task_id, payload)
    if action != 'download':
        return _execute(action, payload, report_progress, rotation_completed, store, worker_slot)
    payload = {**payload, '_task_id': task_id}
    from Utils.download_storage import DownloadWorkspace
    from Utils.config import video_directory
    from webapp.disk_budget import DiskBudget, DiskCoordinationBusy, DiskPressure, DISK_FIELDS
    config = dict(payload['config'])
    runtime = store.runtime() if store else config
    config.update({key: runtime[key] for key in DISK_FIELDS if key in runtime})
    if 'min_free_gb' in runtime:
        config['min_free_gb'] = runtime['min_free_gb']
    if 'postprocess_limit' in runtime:
        config['postprocess_limit'] = runtime['postprocess_limit']
    state = store.directory if store else Path(payload['base']) / '.cache'
    if config.get('storage_mode', 'local') != 'test':
        # Keep disposable media workspaces on the same bind mount as their
        # final files. Publication can then rename completed files atomically
        # without a second full-file copy across Docker mounts.
        config['_media_root'] = str(video_directory(config, payload['base']))
    workspace = DownloadWorkspace(config, state, task_id, uuid.uuid4().hex)
    budget = DiskBudget(state, config)
    # 立即停止后的任务要重新下载，即使旧目录还有等待清理的产物凭据。
    if (workspace.test_mode and store and task_id and not
            (payload.get('_force_test_sample') or payload.get('_redownload_after_stop'))):
        recovered = budget.recover_test_result(store, task_id)
        if recovered:
            return recovered
    # Production downloads are disposable media workspaces, not diagnostic
    # cache. Keep their reservation on the global temporary-file budget so
    # large videos can progress even when the small cache budget is full.
    lease = budget.lease('test' if workspace.test_mode else 'downloads', task=task_id,
                         path=workspace.directory if workspace.test_mode else None, wait_for_budget=True,
                         defer_reservation=True)
    lease.media_directory = state if workspace.test_mode else video_directory(config, payload['base'])
    from webapp.downloads import initial_progress
    last_progress = initial_progress(config, payload.get('policy_version'))
    if payload.get('_force_test_sample'):
        last_progress['_sample_requested'] = True
    if payload.get('_retry_cookie_profile_id'):
        last_progress['retry_cookie_profile_id'] = payload['_retry_cookie_profile_id']

    def stop_children():
        from webapp.control import terminate_own_descendants
        terminate_own_descendants()

    def report(value):
        forecast = value.get('space_plan') or {}
        if value.get('stage') != 'waiting_processing':
            # 准备目录、分配账号和解析来源只上报进度。解析得出空间计划后，首次检查水位并申请临时额度。
            if forecast:
                first_plan = lease.planned is None
                peak = forecast['peak_bytes']
                try:
                    lease.plan(peak)
                except DiskPressure as error:
                    if not isinstance(error, DiskCoordinationBusy):
                        # 放开账号后，复查只读空间账本。真实申请成功后才允许写文件。
                        error.download_space_ready = lambda: budget.download_space_ready(
                            lease.media_directory, lease.category, peak)
                    raise
                if first_plan:
                    lease.start_monitor(stop_children)
            elif lease.planned is not None:
                lease.check()
        # 本地处理计划提交后的最后一帧只上报状态，空间检查由接手的进程负责。
        last_progress.update(value, storage_mode=config.get('storage_mode', 'local'))
        if report_progress:
            report_progress(dict(last_progress))

    with lease:
        if not workspace.test_mode:
            if store:
                from webapp.catalog import MediaCatalog
                MediaCatalog(store).register_root(lease.media_directory)
        report({'stage': 'preparing_workspace'})
        prepared = workspace.prepare()
        prepared['_temporary_directory'] = str(lease.path / '_aux')
        prepared['_disk_state'] = str(state)
        payload = {**payload, 'config': prepared, '_disk_workspace': str(lease.path),
                   '_disk_lease': lease.id, '_disk_state': str(state),
                   '_disk_monitor_error': lambda: lease.error}
        try:
            if not store or not task_id or not prepared.get('youtube_cookie_pool'):
                from webapp.throughput import PipelineBusy
                error = PipelineBusy('当前实例未分配可用账号，请在账号管理中分配并登录账号。')
                error.wait_code = 'account_review'
                raise error
            from webapp.cookie_pool import CookiePool
            pool = CookiePool(store, prepared, payload['base'], task_id, strict=False)
            retry_profile = payload.get('_retry_cookie_profile_id')
            if retry_profile and (retry_profile not in pool.profile_names or
                    retry_profile not in {identity for identity, _, _ in pool.accounts} | set(pool.aliases) or
                    payload.get('_retry_cookie_instance_id') != pool.router.instance):
                raise RuntimeError('本任务重试绑定的 Cookie 已不可用或实例不匹配，请检查账号池后手动重试。')
            report({'stage': 'waiting_cookie'})
            with pool.assignment(profile_id=retry_profile, count_success=False,
                                 manual_retry=bool(payload.get('_manual_retry'))) as selected:
                if start_ticket:
                    start_ticket.finish(selected['_cookie_pool_next_at'] - selected['_cookie_pool_admitted_at'])
                cookie_profile_id = selected['_cookie_pool_selected']
                cookie_profile_name = selected.get('_cookie_pool_profile_name', cookie_profile_id)
                report({'cookie_profile_id': cookie_profile_id,
                        'cookie_profile_name': cookie_profile_name})
                selected['_cookie_pool_profile_name'] = cookie_profile_name
                event('cookie_pool_selected', profile_id=selected['_cookie_pool_selected'],
                      source_address=selected.get('youtube_source_address', ''), identity_retry=bool(retry_profile))
                result = _execute(action, {**payload, 'config': selected}, report,
                                  rotation_completed, store, worker_slot)
                if not result.get('skipped') and (result.get('file_count', 0) > 0 or result.get('processing_pending')):
                    pool.outcome(selected['_cookie_pool_scope'], None,
                                 admitted_at=selected['_cookie_pool_admitted_at'])
            if result.get('file_count') and not result.get('processing_pending'):
                try:
                    budget.record_video_size(result['file_bytes'] // result['file_count'],
                        merged=result.get('download_method') == 'yt_dlp')
                except Exception as error:
                    event('disk_estimate_unavailable', error_type=type(error).__name__)
            lease.stop.set()
            if lease.monitor:
                lease.monitor.join(timeout=20)
            if not lease.monitor or not lease.monitor.is_alive():
                # The pipeline returned a verified final outcome. A watchdog
                # observation during its final bookkeeping must not requeue it.
                lease.error = None
            if result.get('processing_pending'):
                lease.preserve = True
                return result
            if workspace.test_mode:
                last_progress.update(stage='cleaning', speed=None, eta=None)
                if report_progress:
                    report_progress(dict(last_progress))
                cleanup_started = time.monotonic()
                # Publish samples only after the task's terminal commit. The
                # normal settlement path owns and retires this lease afterwards.
                lease.preserve = True
                result['_processing_workspace'] = dict(task_id=task_id, disk_lease=lease.id,
                    workspace=str(lease.path))
        except BaseException:
            if store:
                from sqlalchemy import select
                from webapp.database import delivery_work
                with store.connect() as db:
                    lease.preserve = bool(db.execute(select(delivery_work.c.task_id).where(
                        delivery_work.c.task_id == task_id, delivery_work.c.disk_lease == lease.id)).first())
            receipt = lease.path / '.completed-files.json'
            if workspace.test_mode and receipt.exists() and store:
                try:
                    lease.preserve = lease.preserve or not budget.receipts_recorded(store, task_id, receipt)
                except Exception:
                    lease.preserve = True
            raise
    if workspace.test_mode:
        result['_sample_requested'] = bool(payload.get('_force_test_sample'))
        deleted = not workspace.directory.exists()
        result['local_cleanup_seconds'] = round(time.monotonic() - cleanup_started, 3)
        event('local_cleanup_end', elapsed_seconds=result['local_cleanup_seconds'],
              files_deleted=deleted, file_bytes=result.get('file_bytes', 0))
        result.update(message='测试下载完成，结果已登记；结算后按留样设置整理文件。',
                      test_mode=True, files_deleted=deleted, skipped=False)
        print(result['message'], flush=True)
    return result
