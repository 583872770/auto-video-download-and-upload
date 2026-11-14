"""Disk reservations, Cookie assignment and temporary workspace lifecycle."""
from pathlib import Path
import time
import uuid
from webapp.diagnostics import event


def execute_task(action, payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0, task_id=None,
            start_ticket=None, *, pipeline):
    _execute = pipeline
    if action == 'subtitles':
        payload = {**payload, '_task_id': task_id}
    if store and action == 'download':
        payload = store.effective_download_payload(task_id, payload)
    if action != 'download':
        return _execute(action, payload, report_progress, rotation_completed, store, worker_slot)
    payload = {**payload, '_task_id': task_id}
    from Utils.download_storage import DownloadWorkspace
    from webapp.disk_budget import DiskBudget, DISK_FIELDS
    config = dict(payload['config'])
    runtime = store.runtime() if store else config
    config.update({key: runtime[key] for key in DISK_FIELDS if key in runtime})
    if store and not payload.get('_execution_settings'):
        runtime = store.runtime()
        if 'storage_mode' in runtime:
            config['storage_mode'] = runtime['storage_mode']
    state = store.directory if store else Path(payload['base']) / '.cache'
    workspace = DownloadWorkspace(config, state, task_id, uuid.uuid4().hex)
    budget = DiskBudget(state, config)
    if workspace.test_mode and store and task_id:
        recovered = budget.recover_test_result(store, task_id)
        if recovered:
            return recovered
    # Production downloads are disposable media workspaces, not diagnostic
    # cache. Keep their reservation on the global temporary-file budget so
    # large videos can progress even when the small cache budget is full.
    lease = budget.lease('test' if workspace.test_mode else 'downloads', task=task_id,
                         path=workspace.directory if workspace.test_mode else None)
    from webapp.downloads import initial_progress
    last_progress = initial_progress(config, payload.get('policy_version'))
    if payload.get('_retry_cookie_profile_id'):
        last_progress['retry_cookie_profile_id'] = payload['_retry_cookie_profile_id']

    def report(value):
        estimate = value.get('total_bytes') or 0
        if workspace.test_mode:
            lease.check(estimate * (2 if value.get('download_method') == 'yt_dlp' else 1))
        else:
            lease.check()
        last_progress.update(value, storage_mode=config.get('storage_mode', 'local'))
        if report_progress:
            report_progress(dict(last_progress))

    with lease:
        if not workspace.test_mode:
            from Utils.config import video_directory
            lease.media_directory = video_directory(config, payload['base'])
            lease.media_directory.mkdir(parents=True, exist_ok=True)
        def stop_children():
            from webapp.control import terminate_own_descendants
            terminate_own_descendants()
        lease.start_monitor(stop_children)
        report({'stage': 'preparing_workspace'})
        prepared = workspace.prepare()
        prepared['_temporary_directory'] = str(lease.path / '_aux')
        prepared['_disk_state'] = str(state)
        payload = {**payload, 'config': prepared, '_disk_workspace': str(lease.path), '_disk_state': str(state)}
        try:
            from webapp.youtube_auth import is_youtube
            if payload.get('_retry_cookie_profile_id') and not prepared.get('youtube_cookie_pool'):
                raise RuntimeError('本任务重试绑定的 Cookie 池已关闭，请检查配置后手动重试。')
            if prepared.get('youtube_cookie_pool') and is_youtube(payload['urls']):
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
                    if not result.get('skipped') and result.get('file_count', 0) > 0:
                        pool.outcome(selected['_cookie_pool_scope'], None,
                                     admitted_at=selected['_cookie_pool_admitted_at'])
            else:
                if start_ticket:
                    start_ticket.finish(0)
                # A mutable marker follows configuration copies inside _execute.
                # Only a granted source permit starts the end-of-task rest clock.
                activity = prepared['_youtube_task_activity'] = {}
                try:
                    result = _execute(action, payload, report, rotation_completed, store, worker_slot)
                finally:
                    if store and task_id and is_youtube(payload['urls']) and activity.get('started'):
                        from webapp.channel_scheduler import ChannelScheduler
                        ChannelScheduler(store, prepared, task_id).finish_youtube_task()
            if result.get('file_count'):
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
            if workspace.test_mode:
                last_progress.update(stage='cleaning', speed=None, eta=None)
                if report_progress:
                    report_progress(dict(last_progress))
                cleanup_started = time.monotonic()
        except BaseException:
            receipt = lease.path / '.completed-files.json'
            if workspace.test_mode and receipt.exists() and store:
                try:
                    lease.preserve = not budget.receipts_recorded(store, task_id, receipt)
                except Exception:
                    lease.preserve = True
            raise
    if workspace.test_mode:
        deleted = not workspace.directory.exists()
        result['local_cleanup_seconds'] = round(time.monotonic() - cleanup_started, 3)
        event('local_cleanup_end', elapsed_seconds=result['local_cleanup_seconds'],
              files_deleted=deleted, file_bytes=result.get('file_bytes', 0))
        result.update(message=('测试下载完成，临时文件已删除，仅保留任务记录和统计。' if deleted else
                              '测试下载完成，统计已保留；临时文件清理待重试。'),
                      test_mode=True, files_deleted=deleted, skipped=False)
        print(result['message'], flush=True)
    return result
