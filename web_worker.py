"""Run queued work outside Flask, serializing all media and browser operations."""
import argparse
import asyncio
from contextlib import contextmanager
import importlib
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
    if store and action in ('download', 'import'):
        payload = store.effective_download_payload(task_id, payload)
    if action != 'download':
        return _execute(action, payload, report_progress, rotation_completed, store, worker_slot)
    payload = {**payload, '_task_id': task_id}
    from Utils.download_storage import DownloadWorkspace
    config = dict(payload['config'])
    if store:
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
    from Utils.config import resolve_path
    from main import (PLATFORMS, VIDEO_SUFFIXES, build_plan, check_environment, prepare_media,
                      publish_plan, write_prepared_manifest)
    config, base = payload['config'], Path(payload['base'])
    platforms = payload.get('platforms', [])
    if action == 'import':
        from webapp.bulk import run_import
        return run_import(store, payload, report_progress or (lambda value: None))
    if action == 'index':
        from webapp.catalog import MediaCatalog
        from Utils.config import video_directory
        return MediaCatalog(store).reindex(video_directory(config, base), report_progress)
    if action == 'check':
        if payload.get('scope') == 'download':
            from webapp.environment import check_download_environment
            checker = check_download_environment
        else:
            checker = check_environment
        if checker():
            raise RuntimeError('环境检查未通过，请根据日志补齐依赖。')
        return {'message': '环境检查通过'}
    if action == 'download':
        import yt_dlp
        from webapp.downloads import DownloadFailure, DownloadProgress, DownloadLeaseLost, clean_download_error
        from webapp.environment import DownloadEnvironmentRequired, require_fallback_environment
        from webapp.yt5s import BrowserEnvironmentRequired
        from Download import build_ydl_options, download_cookie_session, require_download_tools
        progress = DownloadProgress(report_progress or (lambda value: None))
        progress.state['worker_slot'] = worker_slot
        method = config.get('download_method', 'yt_dlp')
        automatic = method == 'yt5s_fallback'
        method_two = method == 'yt5s'
        progress.state.update(download_method='yt5s' if method_two or automatic else 'yt_dlp',
                              selected_download_method=method)
        progress.save(force=True)
        failure = DownloadFailure()
        test_mode = config.get('storage_mode') == 'test'
        if store:
            runtime = store.runtime()
            if 'youtube_cookie' in runtime:
                config = {**config, 'youtube_cookie': runtime['youtube_cookie']}
        urls = payload['urls']
        if store and not test_mode:
            from webapp.store import source_identity
            video_id, _ = source_identity(urls[0])
            if len(urls) == 1 and video_id and store.archive_has('youtube ' + video_id):
                progress.state.update(stage='skipped', title=video_id, percent=100)
                progress.save(force=True)
                return {'message': '已下载过，跳过重复下载。', 'skipped': True}
        if config.get('proxy_mode') == 'clash':
            from webapp.clash import ClashManager
            proxy = payload.get('clash')
            if not proxy:
                raise RuntimeError('下载任务缺少代理节点配置，请重新创建。')
            manager = ClashManager(proxy['directory'])
            # Paused queues may contain millions of old snapshots. Resolve the
            # saved rotation here so changes also apply to existing queued work.
            manager.reload()
            proxy = dict(proxy)
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
            config = {**config, '_resolved_clash_proxy': snapshot['proxy']}
            print(f'下载代理节点：{snapshot["node"]}', flush=True)
        from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired, is_youtube
        youtube = is_youtube(urls)
        if automatic:
            progress.state.update(stage='checking_environment')
            progress.save(force=True)
            require_fallback_environment(config, base)
            if not store:
                raise DownloadEnvironmentRequired('方式三请通过 Web 下载队列执行，以验证 YouTube 登录。')
        if store and youtube and not method_two:
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
            from Utils.paths import io_path
            path = Path(io_path(filename)).resolve()
            if path.suffix.lower() not in VIDEO_SUFFIXES:
                return
            if catalog:
                catalog.index_file(path, video_directory(config, base))
            if path in existing_files or str(path) in output_files:
                return
            size = path.stat().st_size
            output_files[str(path)] = size
            if store and payload.get('_task_id'):
                if store.statistics.record_file(payload['_task_id'], path,
                        current_video.get('id', ''), current_video.get('title', progress.state.get('title', '')),
                        test_mode=test_mode):
                    if not test_mode:
                        billable_bytes += size

        print(f'开始下载，共 {len(urls)} 个地址。', flush=True)
        if automatic:
            print('方式三：环境与 YouTube 登录检查通过，优先使用 yt5s，失败时切换到 yt-dlp。', flush=True)
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
                archived = store.archive_has(self._make_archive_id(info_dict)) if store else super().in_download_archive(info_dict)
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
                        store.archive_add(identity, info_dict.get('filepath') or info_dict.get('_filename'))
                else:
                    super().record_download_archive(info_dict)

        def attempt(provider, addresses):
            nonlocal failure
            # Each provider needs its own error collector and Cookie policy.
            failure = DownloadFailure()
            provider_config = {**config, 'download_method': provider}
            options = build_ydl_options(provider_config, base)
            if automatic and provider == 'yt_dlp':
                # The providers can return different encodings for the same filename.
                # Never append native bytes to an incomplete yt5s transfer.
                options['continuedl'] = False
            if catalog:
                options['download_archive'] = None
            options.update(post_hooks=[completed_file], progress_hooks=[progress.hook],
                           postprocessor_hooks=[progress.postprocess])
            require_download_tools(addresses, options)
            progress.state.update(download_method=provider, stage='extracting', percent=0,
                                  downloaded_bytes=0, total_bytes=None, speed=None, eta=None, format='')
            progress.streams = {}
            progress.save(force=True)
            try:
                with download_cookie_session(options) as session_options:
                    with ReportingDownloader(session_options) as downloader:
                        if provider == 'yt5s':
                            from webapp.yt5s import resolve

                            def stage(value):
                                progress.state.update(stage=value, speed=None, eta=None)
                                progress.save(force=True)

                            for url in addresses:
                                info = resolve(url, provider_config, base, store.directory if store else base / '.cache', stage)
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
            if automatic:
                progress.state.update(fallback_used=False, fallback_reason='')
            try:
                skipped_any |= attempt('yt5s' if automatic or method_two else 'yt_dlp', addresses)
            except (RuntimeError, yt_dlp.utils.DownloadError) as error:
                if not automatic or isinstance(error, (BrowserEnvironmentRequired, DownloadEnvironmentRequired,
                                                       YouTubeAuthRequired, DownloadLeaseLost)):
                    raise
                # Local storage errors and loss of ownership are not provider failures.
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
                used_fallback = True
                progress.state.update(stage='switching_provider', download_method='yt_dlp', fallback_used=True,
                                      fallback_reason=reason, speed=None, eta=None)
                progress.save(force=True)
                print(f'方式三：yt5s 失败（{reason}）；切换到 yt-dlp，沿用当前代理节点。', flush=True)
                try:
                    # A browser verification/conversion may have taken several minutes.
                    YouTubeAuth(store).require(config, base, **(
                        {'proxy': snapshot['proxy'], 'node': snapshot['node']}
                        if config.get('proxy_mode') == 'clash' else {}))
                    skipped_any |= attempt('yt_dlp', addresses)
                except Exception:
                    print('方式三：yt5s 和 yt-dlp 均未完成下载，最终错误以 yt-dlp 为准。', flush=True)
                    raise
        skipped = not output_files and ((skipped_any and not progress.streams) or bool(existing_files))
        return {'message': '已下载过，跳过重复下载。' if skipped else '下载任务完成，请在素材库检查视频与封面。',
                'skipped': skipped, 'file_count': len(output_files), 'file_bytes': sum(output_files.values()),
                'billable_bytes': billable_bytes, 'download_method': progress.state['download_method'],
                'fallback_used': used_fallback}
    if action == 'login':
        platform = platforms[0]
        module = importlib.import_module(PLATFORMS[platform][0])
        target = resolve_path(base, config.get(f'{platform}_cookie', f'Cookies/{platform}.json'))
        executable = resolve_path(base, config['browser_executable']) if config.get('browser_executable') else None
        asyncio.run(getattr(module, f'{platform}_cookie_gen')(str(target), executable))
        return {'message': '登录态已保存；有效性以平台为准。'}
    print('正在扫描视频并计算排期……', flush=True)
    plans, schedule = build_plan(config, base)
    result = {
        'videos': [{'name': plan.source.name, 'parts': plan.parts, 'cover': plan.cover.name} for plan in plans],
        'schedule': [slot.isoformat() for slot in schedule], 'platforms': platforms,
    }
    print(f'共 {len(plans)} 个视频，{len(schedule)} 个分段；平台：{", ".join(platforms)}。', flush=True)
    for index, slot in enumerate(schedule, 1):
        print(f'{index:02d}  {slot:%Y-%m-%d %H:%M}', flush=True)
    if action == 'prepare':
        print('正在转码分段并生成封面……', flush=True)
        jobs = prepare_media(plans, config, platforms)
        result['manifest'] = str(write_prepared_manifest(jobs, schedule, config, base, platforms))
        print(f'预处理完成：{result["manifest"]}', flush=True)
    elif action == 'publish':
        print('开始实际投稿。收到提交回执后仍需等待平台审核。', flush=True)
        asyncio.run(publish_plan(plans, schedule, config, base, platforms))
    elif action != 'preview':
        raise ValueError('未知任务类型')
    return result


def drain(directory, slot=0, role='all'):
    from webapp.youtube_auth import YouTubeAuthRequired
    from webapp.environment import DownloadEnvironmentRequired
    from webapp.downloads import DownloadLeaseLost
    from webapp.yt5s import BrowserEnvironmentRequired, WebsiteHTTPError, WebsiteTimeoutError
    store = TaskStore(directory)
    name = 'maintenance' if slot < 0 else str(slot)
    with worker_lock(store.directory / f'worker-{name}.lock') as acquired:
        if not acquired:
            return
        store.recover(expired_only=True)
        actions = ('import', 'index') if role == 'maintenance' else ('download',) if role == 'download' else None
        exclude = ('import', 'index') if role == 'regular' else None
        while task := store.claim(store.owner, actions=actions, exclude=exclude):
            config, base = task['payload']['config'], Path(task['payload']['base'])
            runtime = store.runtime()
            if slot > 0 and slot >= int(runtime.get('worker_count', 1)):
                store.release(task['id'], store.owner, '下载进程数量已调整，任务等待其他进程继续。', delay=0)
                break
            if task['action'] == 'download':
                from Utils.config import video_directory
                from Utils.download_storage import DownloadWorkspace
                current_config = {**config, 'storage_mode': runtime.get('storage_mode', config.get('storage_mode', 'local'))}
                workspace = DownloadWorkspace(current_config, store.directory, task['id'])
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

            def heartbeat(task_id=task['id']):
                while not heartbeat_stop.wait(10):
                    try:
                        if not store.heartbeat(task_id, store.owner):
                            return
                        store.worker_status(store.owner, name, 'running', task_id)
                    except Exception:
                        pass

            heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
            heartbeat_thread.start()

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
                    store.set_downloads_paused(True)
                    store.release(task['id'], store.owner, str(error), delay=0)
                    print(f'下载环境检查未通过，队列已停止，任务保留：{error}', flush=True)
                    break
                except YouTubeAuthRequired as error:
                    from webapp.youtube_auth import YouTubeAuth
                    YouTubeAuth(store).block(error.result)
                    store.release(task['id'], store.owner, str(error), delay=0)
                    print(f'登录检查未通过，队列已停止，任务保留：{error}', flush=True)
                    break
                except BaseException as error:
                    traceback.print_exc()
                    print(f'任务失败：{error}', flush=True)
                    message = str(error) or type(error).__name__
                    transient = (error.retryable if isinstance(error, WebsiteHTTPError) else
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
                    store.worker_status(store.owner, name, 'idle')
            from webapp.maintenance import cleanup_logs, trim_log
            trim_log(store, task['id'], runtime.get('max_log_mb', config.get('max_log_mb', 10)))
            cleanup_logs(store, runtime.get('log_retention_days', config.get('log_retention_days', 0)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--role', choices=('all', 'regular', 'download', 'maintenance'), default='all')
    args = parser.parse_args()
    drain(args.state, args.slot, args.role)
