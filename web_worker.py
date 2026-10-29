"""Run queued work outside Flask, serializing all media and browser operations."""
import argparse
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import traceback
import uuid
from webapp.diagnostics import (event, error_details, safe_text, diagnostic_context,
                                configure_diagnostics, process_diagnostics, active_task_log)

from webapp.tasks import TaskStore


@contextmanager
def worker_lock(path):
    from webapp.disk_budget import file_lock
    with file_lock(path) as acquired:
        yield acquired


def execute(action, payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0, task_id=None):
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
    lease = budget.lease('test' if workspace.test_mode else 'cache', task=task_id,
                         path=workspace.directory if workspace.test_mode else None)
    from webapp.downloads import initial_progress
    last_progress = initial_progress(config, payload.get('policy_version'))

    def report(value):
        estimate = value.get('total_bytes') or 0
        if workspace.test_mode:
            lease.check(estimate * (2 if value.get('download_method') == 'yt_dlp' else 1))
        else:
            lease.check()
        last_progress.update(value, storage_mode='test' if workspace.test_mode else 'local')
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
            result = _execute(action, payload, report, rotation_completed, store, worker_slot)
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
        result.update(message=('测试下载完成，临时文件已删除，仅保留任务记录和统计。' if deleted else
                              '测试下载完成，统计已保留；临时文件清理待重试。'),
                      test_mode=True, files_deleted=deleted, skipped=False)
        print(result['message'], flush=True)
    return result


def _execute(action, payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0):
    from webapp.disk_budget import DiskPressure
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
        if check_download_environment(config, base):
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
        event('execution_policy', business_job=payload.get('_business_job'),
              policy_version=payload.get('policy_version'), download_methods=methods,
              yt5s_quality=config.get('yt5s_quality', 'best'),
              downmp3_quality=config.get('downmp3_quality', 'best'),
              yt1d_quality=config.get('yt1d_quality', 'best'),
              yt1d_timeout=config.get('yt1d_timeout', 180),
              bergung_quality=config.get('bergung_quality', 'best'),
              bergung_timeout=config.get('bergung_timeout', 180),
              storage_mode=config.get('storage_mode', 'local'),
              fragment_concurrency=config.get('fragment_concurrency', 4),
              download_format=config.get('download_format') or 'bestvideo*+bestaudio/best',
              downmp3_timeout=config.get('downmp3_timeout', 300),
              download_attempts=config.get('download_attempts', 3),
              proxy_mode=config.get('proxy_mode', 'manual'))
        failure = DownloadFailure()
        from webapp.throughput import PipelineBusy, automatic as automatic_throughput, runtime_limits, transfer_slot
        route_health = None
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
            proxy['health_providers'] = methods
            snapshot = manager.download_snapshot(proxy, completed)
            progress.state.update(proxy_node=snapshot['node'])
            if rotation:
                progress.state['rotation_id'] = rotation_id
                print(f'自动轮换：已计数 {completed} 个视频，每 {rotation["every"]} 个切换节点。', flush=True)
            progress.save(force=True)
            config = {**config, '_resolved_clash_proxy': snapshot['proxy'],
                      '_resolved_clash_node': snapshot.get('node', '')}
            from webapp.proxy_health import ProxyHealth, ProxyCoolingDown
            route_health = ProxyHealth(manager.directory)
            eligible = [provider for provider in methods if route_health.eligible(snapshot['node'], provider)]
            for provider in methods:
                if provider not in eligible:
                    event('provider_cooling_down', provider=provider, proxy_node=snapshot['node'])
            if not eligible:
                raise ProxyCoolingDown('当前出口的下载方式均在冷却，稍后重新选择健康线路。')
            methods = eligible
            print(f'下载代理节点：{snapshot["node"]}', flush=True)
        event('connection_selected', proxy_mode=config.get('proxy_mode', 'manual'),
              proxy_node=config.get('_resolved_clash_node', ''))
        from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired, is_youtube
        youtube = is_youtube(urls)
        progress.state.update(stage='checking_environment')
        progress.save(force=True)
        if 'yt1d' in methods:
            from webapp.yt1d import require_environment
            require_environment(config, base)
        if 'bergung' in methods:
            from webapp.bergung import require_environment
            require_environment(config, base)
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
            if progress.state.get('download_method') in ('downmp3', 'yt1d', 'bergung'):
                from webapp.yt5s import verify_media
                try:
                    verified = verify_media(path)
                    streams = verified.get('streams', [])
                    video = next((stream for stream in streams if stream.get('codec_type') == 'video'), {})
                    event('media_verified', provider=progress.state.get('download_method'), video_id=current_video.get('id', ''),
                          width=video.get('width'), height=video.get('height'),
                          frame_rate=video.get('avg_frame_rate'),
                          video_codec=video.get('codec_name'),
                          duration_seconds=verified.get('format', {}).get('duration'))
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
            if test_mode and payload.get('_disk_workspace'):
                receipt = Path(payload['_disk_workspace']) / '.completed-files.json'
                temporary = receipt.with_suffix('.tmp')
                with temporary.open('w', encoding='utf-8') as stream:
                    json.dump([{'path': name, 'size': amount, 'complete': True,
                                'method': progress.state.get('download_method'),
                                'video_id': current_video.get('id', ''),
                                'title': current_video.get('title', progress.state.get('title', ''))}
                               for name, amount in output_files.items()], stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(receipt)
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
                event('format_selected', provider=progress.state.get('download_method'),
                      video_id=info_dict.get('id'), format_id=info_dict.get('format_id'),
                      width=info_dict.get('width'), height=info_dict.get('height'), fps=info_dict.get('fps'),
                      video_codec=info_dict.get('vcodec'), audio_codec=info_dict.get('acodec'),
                      quality_source='requested' if progress.state.get('download_method') == 'downmp3' else 'provider_reported')
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

        @contextmanager
        def transmitting():
            def waiting():
                # A ready URL must not retain an idle browser while waiting for
                # network capacity; ownership/heartbeats stay with this task.
                from webapp.yt5s_browser import close_all
                close_all()
                progress.state.update(stage='waiting_transfer', speed=None, eta=None)
                progress.save(force=True)

            def ready():
                progress.state.update(stage='extracting', speed=None, eta=None)
                progress.save(force=True)

            directory = store.directory if store else base / '.cache'
            with transfer_slot(directory, config, waiting, ready):
                yield

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
                                with transmitting():
                                    downloader.process_ie_result(info, download=True)
                            failed = downloader._download_retcode
                        elif provider in ('yt5s', 'yt1d', 'bergung'):
                            if provider == 'yt1d':
                                from webapp.yt1d import resolve
                            elif provider == 'bergung':
                                from webapp.bergung import resolve
                            else:
                                from webapp.yt5s import resolve

                            def stage(value):
                                progress.state.update(stage=value, speed=None, eta=None)
                                progress.save(force=True)

                            def browser_capacity():
                                runtime = store.runtime() if store else config
                                directory = store.directory if store else base / '.cache'
                                return runtime_limits(runtime, directory)['browsers']

                            for url in addresses:
                                info = resolve(url, provider_config, base, store.directory if store else base / '.cache', stage,
                                               worker_slot=worker_slot, browser_capacity=browser_capacity)
                                with transmitting():
                                    downloader.process_ie_result(info, download=True)
                            failed = downloader._download_retcode
                        else:
                            if automatic_throughput(config):
                                for address in addresses:
                                    info = downloader.extract_info(address, download=False)
                                    if info is None:
                                        raise RuntimeError(failure.message())
                                    with transmitting():
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

        def record_route(provider, **outcome):
            if route_health:
                try:
                    route_health.record(config['_resolved_clash_node'], provider, **outcome)
                except OSError as error:
                    event('route_health_unavailable', error_type=type(error).__name__)

        for addresses in ([url] for url in urls) if automatic else [urls]:
            progress.state.update(fallback_used=False, fallback_reason='', fallback_from='')
            for index, provider in enumerate(methods):
                provider_started = time.monotonic()
                event('provider_start', provider=provider, provider_attempt=index + 1,
                      proxy_node=config.get('_resolved_clash_node', ''))
                try:
                    if index and provider == 'yt_dlp' and store and youtube:
                        # Website conversion may have taken several minutes.
                        YouTubeAuth(store).require(config, base, **(
                            {'proxy': snapshot['proxy'], 'node': snapshot['node']}
                            if config.get('proxy_mode') == 'clash' else {}))
                    skipped_any |= attempt(provider, addresses)
                    if route_health and progress.state.get('downloaded_bytes', 0) > 0:
                        record_route(provider,
                            transferred_bytes=progress.state['downloaded_bytes'],
                            elapsed=progress.state.get('stage_seconds', {}).get('downloading', 0))
                    event('provider_end', provider=provider, outcome='completed',
                          elapsed_seconds=round(time.monotonic() - provider_started, 3))
                    break
                except PipelineBusy:
                    raise
                except (RuntimeError, yt_dlp.utils.DownloadError) as error:
                    record_route(provider, error=error)
                    event('provider_end', provider=provider, outcome='failed',
                          stage=progress.state.get('stage'),
                          elapsed_seconds=round(time.monotonic() - provider_started, 3),
                          proxy_node=config.get('_resolved_clash_node', ''), **error_details(error))
                    if index == len(methods) - 1:
                        raise
                    # Local storage errors, missing dependencies and loss of
                    # ownership require intervention rather than another transfer.
                    cause = error
                    while cause:
                        if isinstance(cause, (BrowserEnvironmentRequired, DownloadEnvironmentRequired,
                                              YouTubeAuthRequired, DownloadLeaseLost, DiskPressure)):
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
                    event('provider_fallback', source=provider, target=next_provider,
                          reason=reason, node_changed=False)
        skipped = not output_files and ((skipped_any and not progress.streams) or bool(existing_files))
        return {'message': '已下载过，跳过重复下载。' if skipped else '下载任务完成，请在素材库检查视频与封面。',
                'skipped': skipped, 'file_count': len(output_files), 'file_bytes': sum(output_files.values()),
                'billable_bytes': billable_bytes, 'download_method': progress.state['download_method'],
                'fallback_used': used_fallback}
    raise ValueError('当前 Web 服务只支持 YouTube 下载、导入、索引和环境检查。')


def drain(directory, slot=0, role='all'):
    from webapp.disk_budget import DiskPressure, is_disk_full, MIB
    from webapp.downmp3 import Downmp3Error
    from webapp.bergung import BergungError
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
            configure_diagnostics(runtime)
            task_context = dict(task_id=task['id'], video_id=task.get('video_id'),
                batch_id=task.get('batch_id'), business_job=task['payload'].get('_business_job'),
                instance_id=task.get('instance_id') or 'local', worker_id=store.owner, worker_slot=slot,
                attempt=task.get('attempts'), max_attempts=task.get('max_attempts'),
                execution_id=uuid.uuid4().hex)
            from webapp.throughput import PipelineBusy, runtime_limits
            if slot > 0 and slot >= runtime_limits(runtime, store.directory)['workers']:
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
            heartbeat_stop = threading.Event()
            lease_deadline = [time.monotonic() + 45]

            def abandon(reason):
                from webapp.control import terminate_own_descendants
                try:
                    event('lease_abandoned', context=task_context, reason=reason, console=False)
                    terminate_own_descendants()
                finally:
                    os._exit(2)

            def heartbeat(task_id=task['id'], stop=heartbeat_stop, deadline=lease_deadline):
                last_error = 0
                while not stop.wait(10):
                    try:
                        renewal_started = time.monotonic()
                        owned = store.heartbeat(task_id, store.owner)
                        if stop.is_set():
                            return
                        if not owned:
                            abandon('heartbeat_ownership_lost')
                        deadline[0] = renewal_started + 45
                        store.worker_status(store.owner, name, 'running', task_id)
                    except Exception as error:
                        if time.monotonic() - last_error >= 30:
                            last_error = time.monotonic()
                            event('heartbeat_error', context=task_context, console=False, **error_details(error))

            def watchdog(stop=heartbeat_stop, deadline=lease_deadline):
                while not stop.wait(1):
                    if time.monotonic() >= deadline[0]:
                        abandon('heartbeat_deadline_exceeded')  # Stop before the lease can be reassigned.

            heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
            heartbeat_thread.start()
            watchdog_thread = threading.Thread(target=watchdog, daemon=True) if store.fleet else None
            if watchdog_thread:
                watchdog_thread.start()
            resource_wait = None
            last_progress = {}
            started = time.monotonic()

            def report(value):
                last_progress.update(value)
                if not store.update_progress(task['id'], value, store.owner):
                    raise DownloadLeaseLost('任务租约已失效，当前执行已停止。')
            # Redirect OS handles too, so FFmpeg stderr and Python logs stay together.
            sys.stdout.flush()
            sys.stderr.flush()
            from webapp.maintenance import bounded_task_log
            maximum = min(int(runtime.get('max_log_mb', 2)) * MIB,
                          int(runtime.get('disk_log_budget_mb', 64)) * MIB // 64)
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
                                     rotation_completed=store.rotation_completed, store=store, worker_slot=max(0, slot), task_id=task['id'])
                    print('任务执行完成。', flush=True)
                    committed = store.finish(task['id'], result=result, worker_id=store.owner)
                    event('attempt_end', outcome='succeeded' if committed else 'lease_lost',
                          elapsed_seconds=round(time.monotonic() - started, 3),
                          test_mode=bool(result.get('test_mode')), skipped=bool(result.get('skipped')),
                          file_count=result.get('file_count'), file_bytes=result.get('file_bytes'),
                          stage_seconds=last_progress.get('stage_seconds', {}))
                except PipelineBusy as error:
                    store.release(task['id'], store.owner, str(error), delay=error.retry_after)
                    event('attempt_released', reason='pipeline_backpressure', message=str(error))
                    continue
                except DiskPressure as error:
                    resource_wait = str(error)
                    store.release(task['id'], store.owner, str(error), delay=60, resource=True)
                    store.worker_status(store.owner, name, 'disk_wait', message=str(error))
                    print(f'磁盘资源等待：{error}', flush=True)
                    event('attempt_released', reason='disk_pressure', message=str(error), **error_details(error))
                    break
                except (BrowserEnvironmentRequired, DownloadEnvironmentRequired) as error:
                    from webapp.execution import isolate_download_failure
                    isolate_download_failure(store, error)
                    store.release(task['id'], store.owner, str(error), delay=0)
                    print(f'下载环境检查未通过，本机停止领取，任务运行意愿保留：{error}', flush=True)
                    event('attempt_released', reason='environment_required', **error_details(error))
                    break
                except YouTubeAuthRequired as error:
                    from webapp.youtube_auth import YouTubeAuth
                    from webapp.execution import isolate_download_failure
                    isolate_download_failure(store, error)
                    if not store.fleet:
                        YouTubeAuth(store).block(error.result)
                    store.release(task['id'], store.owner, str(error), delay=0)
                    print(f'登录检查未通过，队列已停止，任务保留：{error}', flush=True)
                    event('attempt_released', reason='authentication_required', **error_details(error))
                    break
                except BaseException as error:
                    if is_disk_full(error) or any(term in str(error).lower() for term in ('no space left', 'disk full', 'disk quota exceeded')):
                        resource_wait = '磁盘空间或配额不足，等待释放空间。'
                        store.release(task['id'], store.owner, resource_wait, delay=60, resource=True)
                        store.worker_status(store.owner, name, 'disk_wait', message='磁盘空间或配额不足。')
                        event('attempt_released', reason='disk_full', **error_details(error))
                        break
                    traceback.print_exc()
                    print(f'任务失败：{error}', flush=True)
                    message = safe_text(str(error) or type(error).__name__)[:2000]
                    transient = (error.retryable if isinstance(error, (WebsiteHTTPError, Downmp3Error, BergungError)) else
                                 isinstance(error, WebsiteTimeoutError) or any(term in message.lower() for term in ('timed out', 'timeout', 'connection reset',
                                    'http error 429', 'http error 500', 'http error 502', 'http error 503', 'http error 504')))
                    store.finish(task['id'], error=message, worker_id=store.owner, retryable=transient)
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
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=2)
                    if watchdog_thread:
                        watchdog_thread.join(timeout=2)
                    store.worker_status(store.owner, name, 'disk_wait' if resource_wait else 'idle', message=resource_wait)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--role', choices=('all', 'regular', 'download', 'maintenance'), default='all')
    args = parser.parse_args()
    with process_diagnostics(args.state, args.slot):
        event('worker_boot', worker_slot=args.slot, role=args.role, pid=os.getpid())
        try:
            drain(args.state, args.slot, args.role)
        except BaseException as error:
            event('worker_crash', worker_slot=args.slot, **error_details(error))
            raise
        else:
            event('worker_stopped', worker_slot=args.slot)

