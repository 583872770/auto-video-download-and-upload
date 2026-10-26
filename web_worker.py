"""Run queued work outside Flask, serializing all media and browser operations."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import traceback

from webapp.tasks import TaskStore


@contextmanager
def worker_lock(path):
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def execute(action, payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0, task_id=None):
    if store and action == 'download':
        payload = store.effective_download_payload(task_id, payload)
    if action != 'download':
        return _execute(action, payload, report_progress, rotation_completed, store, worker_slot)
    payload = {**payload, '_task_id': task_id}
    from Utils.download_storage import DownloadWorkspace
    config = dict(payload['config'])
    if store and not payload.get('_execution_settings'):
        runtime = store.runtime()
        if 'storage_mode' in runtime:
            config['storage_mode'] = runtime['storage_mode']
    workspace = DownloadWorkspace(config, store.directory if store else Path(payload['base']) / '.cache', task_id)
    last_progress = {}

    def report(value):
        last_progress.update(value, storage_mode='test' if workspace.test_mode else 'local')
        if report_progress:
            report_progress(dict(last_progress))

    try:
        report({'stage': 'preparing_workspace'})
        result = _execute(action, {**payload, 'config': workspace.prepare()}, report,
                          rotation_completed, store, worker_slot)
        if workspace.test_mode:
            report({**last_progress, 'stage': 'cleaning', 'speed': None, 'eta': None})
    finally:
        workspace.cleanup()
    if workspace.test_mode:
        result.update(message='测试下载完成，本次视频、封面、字幕及元数据已删除，仅保留任务记录和日志。',
                      test_mode=True, files_deleted=True, skipped=False)
        print(result['message'], flush=True)
    return result


def _execute(action, payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0):
    from Utils.runtime import configure_runtime
    configure_runtime()
    from webapp.media import VIDEO_SUFFIXES
    from webapp.environment import check_download_environment
    config, base = payload.get('config', {}), Path(payload['base'])
    if action == 'import':
        from webapp.bulk import run_import
        return run_import(store, payload, report_progress or (lambda value: None))
    if action == 'index':
        from webapp.catalog import MediaCatalog
        from Utils.config import video_directory
        return MediaCatalog(store).reindex(video_directory(config, base), report_progress)
    if action == 'check':
        if check_download_environment(config):
            raise RuntimeError('环境检查未通过，请根据日志补齐依赖。')
        return {'message': '环境检查通过'}
    if action == 'download':
        import yt_dlp
        from webapp.downloads import DownloadFailure, DownloadProgress, DownloadLeaseLost, clean_download_error
        from webapp.environment import DownloadEnvironmentRequired, require_fallback_environment
        from webapp.yt5s import BrowserEnvironmentRequired
        from webapp.downloader import build_ydl_options, download_cookie_session, require_download_tools
        progress = DownloadProgress(report_progress or (lambda value: None))
        progress.state['worker_slot'] = worker_slot
        from webapp.settings import PROVIDERS, download_methods, validate_method_urls
        methods = download_methods(config)
        automatic = len(methods) > 1
        validate_method_urls(config, payload['urls'])
        progress.state.update(download_method=methods[0], selected_download_methods=methods)
        progress.save(force=True)
        method_label = ' → '.join(PROVIDERS[method] for method in methods)
        print(f'本任务下载方式：{method_label}（开始执行时读取页面配置）。', flush=True)
        failure = DownloadFailure()
        test_mode = config.get('storage_mode') == 'test'
        if store and not payload.get('_execution_settings'):
            runtime = store.runtime()
            if 'youtube_cookie' in runtime:
                config = {**config, 'youtube_cookie': runtime['youtube_cookie']}
        urls = payload['urls']
        if store and not test_mode:
            from webapp.store import source_identity
            video_id, _ = source_identity(urls[0])
            if len(urls) == 1 and video_id and store.archive_has('youtube ' + video_id, **({'job_id': payload['_business_job']} if payload.get('_business_job') else {})):
                progress.state.update(stage='skipped', title=video_id, percent=100)
                progress.save(force=True)
                return {'message': '已下载过，跳过重复下载。', 'skipped': True}
        if config.get('proxy_mode') == 'clash':
            progress.state.update(stage='preparing_proxy')
            progress.save(force=True)
            from webapp.clash import ClashManager
            proxy = payload.get('clash')
            if not proxy:
                raise RuntimeError('下载任务缺少代理节点配置，请重新创建。')
            manager = ClashManager(proxy['directory'])
            # Paused queues may contain millions of old snapshots. Resolve the
            # saved rotation here so changes also apply to existing queued work.
            manager.reload()
            proxy = dict(proxy) if proxy.get('node') else manager.snapshot()
            previous_rotation = proxy.pop('rotation', None)
            current_rotation = manager.rotation()
            if current_rotation['enabled']:
                proxy['rotation'] = current_rotation
            elif previous_rotation or proxy.get('subscription_id', '') != manager.config.get('subscription_id', ''):
                # Subscription updates while paused may replace the saved node.
                # Use the current selection without rewriting millions of tasks.
                proxy['node'] = manager.config.get('selected') or proxy['node']
            if worker_slot:
                manager = manager.for_worker(worker_slot)
                if not proxy.get('rotation'):
                    manager.select(proxy['node'], automatic=True)
            rotation = proxy.get('rotation')
            completed = 0
            if rotation:
                if rotation_completed is None:
                    raise RuntimeError('自动轮换需要通过下载队列执行。')
                rotation_id = rotation['id'] + (f':{worker_slot}' if worker_slot else '')
                completed = rotation_completed(rotation_id, rotation['count_failures'])
            snapshot = manager.download_snapshot(proxy, completed)
            progress.state.update(proxy_node=snapshot['node'])
            if rotation:
                progress.state['rotation_id'] = rotation_id
                print(f'自动轮换：已计数 {completed} 个视频，每 {rotation["every"]} 个切换节点。', flush=True)
            progress.save(force=True)
            config = {**config, '_resolved_clash_proxy': snapshot['proxy'],
                      '_resolved_clash_node': snapshot.get('node', '')}
            print(f'下载代理节点：{snapshot["node"]}', flush=True)
        from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired, is_youtube
        youtube = is_youtube(urls)
        progress.state.update(stage='checking_environment')
        progress.save(force=True)
        if 'yt5s' in methods and 'yt_dlp' in methods:
            progress.state.update(stage='checking_environment')
            progress.save(force=True)
            require_fallback_environment(config, base)
            if not store:
                raise DownloadEnvironmentRequired('含 yt-dlp 的自动切换请通过 Web 下载队列执行，以验证 YouTube 登录。')
        elif 'yt5s' in methods:
            from webapp.yt5s import require_browser_environment
            require_browser_environment(config, base)
        if 'downmp3' in methods:
            from webapp.environment import require_downmp3_environment
            require_downmp3_environment()
        if store and youtube and 'yt_dlp' in methods:
            progress.state.update(stage='checking_login')
            progress.save(force=True)
            auth = YouTubeAuth(store)
            auth.require(config, base, **({'proxy': snapshot['proxy'], 'node': snapshot['node']}
                                        if config.get('proxy_mode') == 'clash' else {}))
            progress.state.update(stage='extracting')
            progress.save(force=True)
        output_files, existing_files, current_video = {}, set(), {}
        billable_bytes = 0
        catalog = None
        if store and not test_mode:
            from webapp.catalog import MediaCatalog
            from Utils.config import video_directory
            catalog = MediaCatalog(store)

        def completed_file(filename):
            nonlocal billable_bytes
            if store and payload.get('_business_job') and not store.heartbeat(payload['_task_id'], store.owner):
                from webapp.downloads import DownloadLeaseLost
                raise DownloadLeaseLost('任务清单已变更，当前执行已取消。')
            from Utils.paths import io_path
            path = Path(io_path(filename)).resolve()
            if path.suffix.lower() not in VIDEO_SUFFIXES:
                return
            if progress.state.get('download_method') == 'downmp3':
                from webapp.yt5s import verify_media
                try:
                    verify_media(path)
                except RuntimeError:
                    # A rejected HTTP error page may already have its final MP4
                    # name. Do not let another provider mistake it for a video.
                    if path not in existing_files and str(path) not in output_files:
                        path.unlink(missing_ok=True)
                    raise
            if catalog and not payload.get('_business_job'):
                catalog.index_file(path, video_directory(config, base))
            if path in existing_files or str(path) in output_files:
                return
            size = path.stat().st_size
            output_files[str(path)] = size
            if store and payload.get('_task_id'):
                if store.statistics.record_file(payload['_task_id'], path,
                        current_video.get('id', ''), current_video.get('title', progress.state.get('title', '')),
                        test_mode=test_mode):
                    if catalog and payload.get('_business_job'):
                        catalog.index_file(path, Path(config['_job_media_root']))
                    if not test_mode:
                        billable_bytes += size

        print(f'开始下载，共 {len(urls)} 个地址。', flush=True)
        if automatic:
            print(f'按优先级尝试：{method_label}；成功后不再尝试后续下载器。', flush=True)
        class ReportingDownloader(yt_dlp.YoutubeDL):
            skipped = False

            def report_warning(self, message, *args, **kwargs):
                failure.warning(message)
                return super().report_warning(message, *args, **kwargs)

            def report_error(self, message, *args, **kwargs):
                failure.error(message)
                return super().report_error(message, *args, **kwargs)

            def process_info(self, info_dict):
                # yt-dlp removes requested_formats before individual stream hooks.
                from Utils.paths import io_path
                current_video.update(id=info_dict.get('id', ''), title=info_dict.get('title', ''))
                for filename in (self.prepare_filename(info_dict), info_dict.get('filepath')):
                    if filename:
                        path = Path(io_path(filename)).resolve()
                        if path.is_file() and str(path) not in output_files:
                            existing_files.add(path)
                progress.begin_video(info_dict)
                return super().process_info(info_dict)

            def in_download_archive(self, info_dict):
                if test_mode:
                    return False
                archived = store.archive_has(self._make_archive_id(info_dict), **({'job_id': payload['_business_job']} if payload.get('_business_job') else {})) if store else super().in_download_archive(info_dict)
                if archived:
                    self.skipped = True
                    progress.state.update(stage='skipped', title=str(info_dict.get('title') or info_dict.get('id') or ''),
                                          percent=100, speed=None, eta=None)
                    progress.save(force=True)
                return archived

            def record_download_archive(self, info_dict):
                if test_mode:
                    return
                if store:
                    identity = self._make_archive_id(info_dict)
                    if identity:
                        store.archive_add(identity, info_dict.get('filepath') or info_dict.get('_filename'), **({'job_id': payload['_business_job']} if payload.get('_business_job') else {}))
                else:
                    super().record_download_archive(info_dict)

        def attempt(provider, addresses):
            nonlocal failure
            # Each provider needs its own error collector and Cookie policy.
            failure = DownloadFailure()
            provider_config = {**config, 'download_method': provider}
            options = build_ydl_options(provider_config, base)
            if payload.get('_restart_transfer') or automatic:
                # The providers can return different encodings for the same filename.
                # Never append bytes from another provider to an incomplete transfer.
                options['continuedl'] = False
            if catalog:
                options['download_archive'] = None
            options.update(post_hooks=[completed_file], progress_hooks=[progress.hook],
                           postprocessor_hooks=[progress.postprocess])
            require_download_tools(addresses, options)
            progress.state.update(download_method=provider, stage='extracting', percent=0, provider_percent=None,
                                  downloaded_bytes=0, total_bytes=None, speed=None, eta=None, format='')
            progress.streams = {}
            progress.save(force=True)
            try:
                with download_cookie_session(options) as session_options:
                    with ReportingDownloader(session_options) as downloader:
                        if provider == 'downmp3':
                            from webapp.downmp3 import resolve

                            def api_stage(value, percent):
                                progress.state.update(stage=value, provider_percent=percent, speed=None, eta=None)
                                progress.save(force=True)

                            for url in addresses:
                                info = resolve(url, provider_config, base, api_stage)
                                downloader.process_ie_result(info, download=True)
                            failed = downloader._download_retcode
                        elif provider == 'yt5s':
                            from webapp.yt5s import resolve

                            def stage(value):
                                progress.state.update(stage=value, speed=None, eta=None)
                                progress.save(force=True)

                            def browser_capacity():
                                runtime = store.runtime() if store else config
                                return int(runtime.get('yt5s_browser_count', config.get('yt5s_browser_count', 2)))

                            for url in addresses:
                                info = resolve(url, provider_config, base, store.directory if store else base / '.cache', stage,
                                               worker_slot=worker_slot, browser_capacity=browser_capacity)
                                downloader.process_ie_result(info, download=True)
                            failed = downloader._download_retcode
                        else:
                            failed = downloader.download(addresses)
            except yt_dlp.utils.DownloadError as error:
                if store and youtube and provider == 'yt_dlp' and (failure.cookie_rejected or '要求登录验证' in failure.message(error)):
                    raise YouTubeAuthRequired(YouTubeAuth(store).reject(failure.message(error))) from error
                raise RuntimeError(failure.message(error)) from error
            if store and youtube and provider == 'yt_dlp' and (failure.cookie_rejected or (failed and '要求登录验证' in failure.message())):
                raise YouTubeAuthRequired(YouTubeAuth(store).reject(failure.message()))
            if failed:
                raise RuntimeError(failure.message())
            return downloader.skipped

        skipped_any = False
        used_fallback = False
        for addresses in ([url] for url in urls) if automatic else [urls]:
            progress.state.update(fallback_used=False, fallback_reason='', fallback_from='')
            for index, provider in enumerate(methods):
                try:
                    if index and provider == 'yt_dlp' and store and youtube:
                        # Website conversion may have taken several minutes.
                        YouTubeAuth(store).require(config, base, **(
                            {'proxy': snapshot['proxy'], 'node': snapshot['node']}
                            if config.get('proxy_mode') == 'clash' else {}))
                    skipped_any |= attempt(provider, addresses)
                    break
                except (RuntimeError, yt_dlp.utils.DownloadError) as error:
                    if index == len(methods) - 1:
                        raise
                    # Local storage errors, missing dependencies and loss of
                    # ownership require intervention rather than another transfer.
                    cause = error
                    while cause:
                        if isinstance(cause, (BrowserEnvironmentRequired, DownloadEnvironmentRequired,
                                              YouTubeAuthRequired, DownloadLeaseLost)):
                            raise cause
                        if isinstance(cause, OSError) and cause.errno in (5, 13, 28, 30, 122):
                            raise error
                        cause = cause.__cause__ or cause.__context__
                    if any(term in str(error).lower() for term in ('no space left', 'disk full', 'permission denied', 'read-only file system')):
                        raise
                    reason = clean_download_error(error)
                    next_provider = methods[index + 1]
                    used_fallback = True
                    if store and payload.get('_task_id'):
                        store.statistics.methods.fallback(payload['_task_id'], next_provider)
                    progress.state.update(stage='switching_provider', download_method=next_provider, fallback_used=True,
                                          fallback_reason=reason, fallback_from=PROVIDERS[provider], speed=None, eta=None)
                    progress.save(force=True)
                    print(f'{PROVIDERS[provider]} 失败（{reason}）；切换到 {PROVIDERS[next_provider]}，沿用当前代理节点。', flush=True)
        skipped = not output_files and ((skipped_any and not progress.streams) or bool(existing_files))
        return {'message': '已下载过，跳过重复下载。' if skipped else '下载任务完成，请在素材库检查视频与封面。',
                'skipped': skipped, 'file_count': len(output_files), 'file_bytes': sum(output_files.values()),
                'billable_bytes': billable_bytes, 'download_method': progress.state['download_method'],
                'fallback_used': used_fallback}
    raise ValueError('当前 Web 服务只支持 YouTube 下载、导入、索引和环境检查。')


def drain(directory, slot=0, role='all'):
    from webapp.downmp3 import Downmp3Error
    from webapp.youtube_auth import YouTubeAuthRequired
    from webapp.environment import DownloadEnvironmentRequired
    from webapp.downloads import DownloadLeaseLost
    from webapp.yt5s import BrowserEnvironmentRequired, WebsiteHTTPError, WebsiteTimeoutError
    store = TaskStore(directory, os.environ.get('VIDEO_WORKER_DATABASE_URL'))
    name = 'maintenance' if slot < 0 else str(slot)
    with worker_lock(store.directory / f'worker-{name}.lock') as acquired:
        if not acquired:
            return
        store.recover(expired_only=True)
        actions = ('import', 'index') if role == 'maintenance' else ('download',) if role == 'download' else None
        exclude = ('import', 'index') if role == 'regular' else None
        while task := store.claim(store.owner, actions=actions, exclude=exclude):
            config, base = task['payload'].get('config', {}), Path(task['payload']['base'])
            runtime = store.runtime()
            if slot > 0 and slot >= int(runtime.get('worker_count', 1)):
                store.release(task['id'], store.owner, '下载进程数量已调整，任务等待其他进程继续。', delay=0)
                break
            if task['action'] == 'download':
                from Utils.config import video_directory
                from Utils.download_storage import DownloadWorkspace
                workspace = DownloadWorkspace(config, store.directory, task['id'])
                target = video_directory(workspace.config, base)
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
            heartbeat_stop = threading.Event()
            lease_deadline = [time.monotonic() + 45]

            def abandon():
                from webapp.control import terminate_own_descendants
                try:
                    terminate_own_descendants()
                finally:
                    os._exit(2)

            def heartbeat(task_id=task['id'], stop=heartbeat_stop, deadline=lease_deadline):
                while not stop.wait(10):
                    try:
                        renewal_started = time.monotonic()
                        owned = store.heartbeat(task_id, store.owner)
                        if stop.is_set():
                            return
                        if not owned:
                            abandon()
                        deadline[0] = renewal_started + 45
                        store.worker_status(store.owner, name, 'running', task_id)
                    except Exception:
                        pass  # The independent watchdog also covers blocked SQL calls.

            def watchdog(stop=heartbeat_stop, deadline=lease_deadline):
                while not stop.wait(1):
                    if time.monotonic() >= deadline[0]:
                        abandon()  # Stop before the 60-second lease can be reassigned.

            heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
            heartbeat_thread.start()
            watchdog_thread = threading.Thread(target=watchdog, daemon=True) if store.fleet else None
            if watchdog_thread:
                watchdog_thread.start()

            def report(value):
                if not store.update_progress(task['id'], value, store.owner):
                    raise DownloadLeaseLost('任务租约已失效，当前执行已停止。')
            # Redirect OS handles too, so FFmpeg stderr and Python logs stay together.
            sys.stdout.flush()
            sys.stderr.flush()
            with store.log_path(task['id']).open('ab', buffering=0) as output:
                os.dup2(output.fileno(), sys.stdout.fileno())
                os.dup2(output.fileno(), sys.stderr.fileno())
                try:
                    print(f'[{task["action"]}] 任务开始', flush=True)
                    result = execute(task['action'], task['payload'],
                                     report,
                                     rotation_completed=store.rotation_completed, store=store, worker_slot=max(0, slot), task_id=task['id'])
                    print('任务执行完成。', flush=True)
                    store.finish(task['id'], result=result, worker_id=store.owner)
                except (BrowserEnvironmentRequired, DownloadEnvironmentRequired) as error:
                    from webapp.execution import isolate_download_failure
                    isolate_download_failure(store, error)
                    store.release(task['id'], store.owner, str(error), delay=0)
                    print(f'下载环境检查未通过，本机停止领取，任务运行意愿保留：{error}', flush=True)
                    break
                except YouTubeAuthRequired as error:
                    from webapp.youtube_auth import YouTubeAuth
                    from webapp.execution import isolate_download_failure
                    isolate_download_failure(store, error)
                    if not store.fleet:
                        YouTubeAuth(store).block(error.result)
                    store.release(task['id'], store.owner, str(error), delay=0)
                    print(f'登录检查未通过，队列已停止，任务保留：{error}', flush=True)
                    break
                except BaseException as error:
                    traceback.print_exc()
                    print(f'任务失败：{error}', flush=True)
                    message = str(error) or type(error).__name__
                    transient = (error.retryable if isinstance(error, (WebsiteHTTPError, Downmp3Error)) else
                                 isinstance(error, WebsiteTimeoutError) or any(term in message.lower() for term in ('timed out', 'timeout', 'connection reset',
                                    'http error 429', 'http error 500', 'http error 502', 'http error 503', 'http error 504')))
                    store.finish(task['id'], error=message, worker_id=store.owner, retryable=transient)
                    current = store.get(task['id'])
                    if current and current['status'] == 'queued':
                        print(f'临时错误，已安排自动重试；本次为第 {current["attempts"]}/{current["max_attempts"]} 次尝试。', flush=True)
                    if not isinstance(error, Exception):
                        raise
                finally:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=2)
                    if watchdog_thread:
                        watchdog_thread.join(timeout=2)
                    store.worker_status(store.owner, name, 'idle')
            from webapp.maintenance import trim_log
            trim_log(store, task['id'], runtime.get('max_log_mb', config.get('max_log_mb', 10)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--role', choices=('all', 'regular', 'download', 'maintenance'), default='all')
    args = parser.parse_args()
    drain(args.state, args.slot, args.role)

