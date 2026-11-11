"""Download orchestration: configuration, route selection, fallback and delivery."""
from contextlib import ExitStack
from pathlib import Path
import time
from webapp.adapters.execution import ProviderExecution
from webapp.diagnostics import event, error_details
from webapp.download_policy import failure_evidence
from webapp.worker.session import DownloadSession
from webapp.worker.formatting import format_bytes


def run_download(payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0):
    config, base = payload.get('config', {}), Path(payload['base'])
    from webapp.disk_budget import DiskPressure
    from webapp.media import VIDEO_SUFFIXES
    import yt_dlp
    from webapp.downloads import DownloadFailure, DownloadProgress, DownloadLeaseLost, clean_download_error
    from webapp.environment import DownloadEnvironmentRequired, require_fallback_environment
    from webapp.yt5s import BrowserEnvironmentRequired
    from webapp.downloader import build_ydl_options, download_cookie_session, require_download_tools
    progress = DownloadProgress(report_progress or (lambda value: None))
    progress.state['worker_slot'] = worker_slot
    from webapp.config.settings import PROVIDERS, download_methods, validate_method_urls
    methods = download_methods(config)
    from webapp.channels import backend_key, execution_config, registry
    from webapp.channel_scheduler import ChannelScheduler
    from webapp.download_metrics import meter_for
    channels = registry(config)
    def route_key(channel):
        return ('cobalt:' + backend_key(channel) if config.get('scheduler_mode') == 'auto'
                and channel.adapter == 'bergung' else channel.adapter)
    automatic = len(channels) > 1
    scheduler = ChannelScheduler(store, config, payload.get('_task_id'), base=base)
    meter = meter_for(store.directory if store else base / '.cache', worker_slot)
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
    from webapp.throughput import PipelineBusy, automatic as automatic_throughput, runtime_limits, transfer_slot
    route_health = None
    test_mode = config.get('storage_mode') == 'test'
    oss_mode = config.get('storage_mode') == 'oss'
    from Utils.oss_storage import archive_key
    oss_storage = None
    if oss_mode:
        from Utils.oss_storage import OSSStorage
        if payload.get('_business_job'):
            config = {**config, '_oss_namespace': 'jobs/' + payload['_business_job'] + '/' + payload['_task_id']}
        oss_storage = OSSStorage(config)
    if store and not payload.get('_execution_settings') and not config.get('_cookie_pool_selected'):
        runtime = store.runtime()
        if 'youtube_cookie' in runtime:
            config = {**config, 'youtube_cookie': runtime['youtube_cookie']}
    if store and 'yt_dlp' in methods and (config.get('youtube_cookie_usage', 'auto') != 'never'
                                          or config.get('_cookie_pool_selected')):
        from webapp.cookie_profiles import CookieProfiles
        from webapp.cookie_binding import CookieBindingConflict
        try:
            CookieProfiles(base, store).restore_selected(config.get('youtube_cookie'))
        except CookieBindingConflict as error:
            from webapp.youtube_auth import YouTubeAuthRequired
            # Reuse the auth isolation path: preserve the task and stop this
            # instance, rather than charging failures to every queued video.
            raise YouTubeAuthRequired({'state': 'invalid', 'message': str(error)}) from None
    urls = payload['urls']
    if store and not test_mode:
        from webapp.store import source_identity
        video_id, _ = source_identity(urls[0])
        if len(urls) == 1 and video_id and store.archive_has(archive_key(config, 'youtube ' + video_id), **({'job_id': payload['_business_job']} if payload.get('_business_job') else {})):
            progress.state.update(stage='skipped', title=video_id, percent=100)
            progress.save(force=True)
            return {'message': '已下载过，跳过重复下载。', 'skipped': True}
    from webapp.worker.connection import prepare_connection
    config, channels, methods, route_health, snapshot = prepare_connection(
        config, payload, channels, methods, progress, worker_slot=worker_slot,
        rotation_completed=rotation_completed, route_key=route_key)
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
    use_cookie = (config.get('youtube_cookie_usage', 'auto') == 'always'
                  or config.get('_cookie_pool_selected'))
    if store and youtube and 'yt_dlp' in methods and use_cookie:
        progress.state.update(stage='checking_login')
        progress.save(force=True)
        auth = YouTubeAuth(store)
        try:
            auth.require(config, base, publish_latest=not bool(config.get('_cookie_pool_selected')),
                         **({'proxy': snapshot['proxy'], 'node': snapshot['node']}
                                        if config.get('proxy_mode') == 'clash' else {}))
        except YouTubeAuthRequired as error:
            if route_health and error.connection_failure:
                # Preflight failures occur before provider attempts, but must
                # still cool a repeatedly failing route in automatic rotation.
                try:
                    route_health.record(snapshot['node'], 'yt_dlp', error=error)
                except OSError as health_error:
                    event('route_health_unavailable', error_type=type(health_error).__name__)
            raise
        progress.state.update(stage='extracting')
        progress.save(force=True)
    catalog = None
    if store and not test_mode:
        from webapp.catalog import MediaCatalog
        from Utils.config import video_directory
        catalog = MediaCatalog(store)


    session = DownloadSession(
        config=config,
        base=base,
        payload=payload,
        store=store,
        worker_slot=worker_slot,
        progress=progress,
        scheduler=scheduler,
        meter=meter,
        youtube=youtube,
        test_mode=test_mode,
        oss_mode=oss_mode,
        automatic=automatic,
        catalog=catalog,
        failure=DownloadFailure())
    output_files = session.output_files
    executor = ProviderExecution(session)
    attempt = executor.attempt

    print(f'开始下载，共 {len(urls)} 个地址。', flush=True)
    if config.get('scheduler_mode') == 'auto':
        print('自动调度将根据可用渠道的负载和健康状态选择下载方式。', flush=True)
    elif automatic:
        print(f'按优先级尝试：{method_label}；成功后不再尝试后续下载器。', flush=True)
    from webapp.parallel_http import ParallelYoutubeDL, fragment_connection_budget, transfer_connection_units
    from webapp.local_processing import local_processing


    skipped_any = False
    used_fallback = False

    def record_route(channel, **outcome):
        error = outcome.get('error')
        if error and config.get('_cookie_pool_selected'):
            value = failure_evidence(error)
            if value.kind in ('account_rate_limit', 'request_rate_limit'):
                return  # A session limit is not evidence that this route failed.
        if route_health:
            try:
                route_health.record(config['_resolved_clash_node'], route_key(channel), **outcome)
            except OSError as error:
                event('route_health_unavailable', error_type=type(error).__name__)

    for addresses in ([url] for url in urls) if automatic else [urls]:
        progress.state.update(fallback_used=False, fallback_reason='', fallback_from='')
        pending = list(channels)
        previous_error = None
        budget = min(len(channels), int(config.get('auto_channel_attempts', 3))) if config.get('scheduler_mode') == 'auto' else len(channels)
        for index in range(budget):
            assignment = ExitStack()
            try:
                channel = assignment.enter_context(scheduler.assignment(pending, allow_fallback=previous_error is not None))
            except PipelineBusy:
                if previous_error:
                    raise previous_error
                raise
            pending.remove(channel)
            provider = channel.adapter
            session.failure_phase = 'resolve'
            provider_started = time.monotonic()
            before_bytes = sum(output_files.values())
            event('provider_start', provider=provider, channel_id=channel.id,
                  upstream_group=channel.upstream_group, provider_attempt=index + 1,
                  proxy_node=config.get('_resolved_clash_node', ''))
            try:
                if store and payload.get('_task_id'):
                    if config.get('scheduler_mode') == 'auto':
                        store.statistics.methods.select_provider(payload['_task_id'], provider, fallback=bool(index))
                    elif index:
                        store.statistics.methods.fallback(payload['_task_id'], provider)
                if index and provider == 'yt_dlp' and store and youtube and use_cookie:
                    # Website conversion may have taken several minutes.
                    YouTubeAuth(store).require(config, base, **(
                        {'proxy': snapshot['proxy'], 'node': snapshot['node']}
                        if config.get('proxy_mode') == 'clash' else {}))
                skipped_any |= attempt(channel, addresses,
                    without_cookies=(config.get('youtube_cookie_usage', 'auto') == 'auto'
                                     and not config.get('_cookie_pool_selected')
                                     and provider == 'yt_dlp' and youtube))
                scheduler.outcome(channel, success=True, elapsed=time.monotonic() - provider_started,
                                  size=sum(output_files.values()) - before_bytes,
                                  resolve_elapsed=meter.resolve_seconds, transfer_elapsed=session.transfer_seconds)
                meter.finish(True)
                if route_health and progress.state.get('downloaded_bytes', 0) > 0:
                    record_route(channel,
                        transferred_bytes=progress.state['downloaded_bytes'],
                        elapsed=progress.state.get('stage_seconds', {}).get('downloading', 0))
                transferred = progress.state.get('downloaded_bytes', 0)
                output_bytes = sum(output_files.values()) - before_bytes
                print(f'{PROVIDERS[provider]} 完成：传输 {format_bytes(transferred)}，生成文件 {format_bytes(output_bytes)}。', flush=True)
                event('provider_end', provider=provider, outcome='completed',
                      warning_counts=dict(session.failure.warnings),
                      transferred_bytes=transferred, output_bytes=output_bytes,
                      elapsed_seconds=round(time.monotonic() - provider_started, 3))
                break
            except PipelineBusy:
                raise
            except (RuntimeError, yt_dlp.utils.DownloadError) as error:
                previous_error = error
                phase = session.failure_phase
                observed_phase = ('resolve' if progress.state.get('stage') in (
                    'extracting', 'waiting_resolver', 'waiting_browser', 'waiting_verification', 'converting') else phase)
                failure_evidence(error, phase=observed_phase,
                    source='youtube' if provider == 'yt_dlp' else 'provider')
                scheduler.outcome(channel, success=False, error=error, phase=phase,
                                  upstream_group=progress.state.get('upstream_group'))
                evidence = failure_evidence(error, phase=phase,
                    source='youtube' if provider == 'yt_dlp' else 'provider')
                progress.state['last_failure'] = evidence.public()
                progress.save(force=True)
                from webapp.channel_scheduler import throttle_details
                status, _ = throttle_details(error)
                meter.finish(False, upstream_failure=phase == 'transfer' and status in (429, 503))
                record_route(channel, error=error)
                event('provider_end', provider=provider, outcome='failed',
                      warning_counts=dict(session.failure.warnings),
                      transferred_bytes=progress.state.get('downloaded_bytes', 0),
                      stage=progress.state.get('stage'),
                      elapsed_seconds=round(time.monotonic() - provider_started, 3),
                      proxy_node=config.get('_resolved_clash_node', ''), **error_details(error))
                if evidence.kind == 'account_rate_limit':
                    raise
                if not pending or index + 1 >= budget:
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
                next_provider = pending[0].adapter
                used_fallback = True
                progress.state.update(stage='switching_provider', download_method=next_provider, fallback_used=True,
                                      fallback_reason=reason, fallback_from=PROVIDERS[provider], speed=None, eta=None)
                progress.save(force=True)
                print(f'{PROVIDERS[provider]} 失败（{reason}）；切换到 {PROVIDERS[next_provider]}，沿用当前代理节点。', flush=True)
                event('provider_fallback', source=provider, target=next_provider,
                      reason=reason, node_changed=False)
            finally:
                assignment.close()
    from webapp.worker.results import finish_delivery
    return finish_delivery(session, oss_storage, skipped_any, used_fallback)
