"""Provider resolution and transfer attempts using one explicit task session."""
from contextlib import contextmanager, nullcontext
from functools import partial
import time
import yt_dlp
from webapp.adapters.reporting import build_reporting_downloader
from webapp.channels import execution_config
from webapp.diagnostics import event
from webapp.downloads import DownloadFailure
from webapp.worker.results import record_completed_file
from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired


class ProviderExecution:
    def __init__(self, session):
        self.session = session

    @contextmanager
    def transmitting(self, channel, info, streams):
        from webapp.throughput import transfer_slot
        session = self.session
        base = session.base
        config = session.config
        meter = session.meter
        progress = session.progress
        scheduler = session.scheduler
        store = session.store

        last_wait = [0]
        def waiting():
            # A ready URL must not retain an idle browser while waiting for
            # network capacity; ownership/heartbeats stay with this task.
            if time.monotonic() - last_wait[0] < 5:
                return
            last_wait[0] = time.monotonic()
            from webapp.yt5s_browser import close_all
            close_all()
            progress.state.update(stage='waiting_transfer', speed=None, eta=None,
                                  transfer_wait_resource=getattr(scheduler, 'blocked_resource', None) or 'local-transfers')
            progress.save(force=True)

        def ready():
            progress.state.update(stage='extracting', speed=None, eta=None,
                                  transfer_admitted=True, transfer_wait_resource=None)
            progress.save(force=True)

        directory = store.directory if store else base / '.cache'
        from webapp.channels import observed_group
        progress.state.update(stage='waiting_transfer', upstream_group=observed_group(channel, info),
                              transfer_admitted=False, transfer_wait_resource=None, speed=None, eta=None)
        progress.save(force=True)
        with scheduler.permit(channel, 'transfer', info=info, streams=streams, on_wait=waiting, timeout=60) as actual:
            meter.group = actual.upstream_group
            progress.state['upstream_group'] = actual.upstream_group
            with transfer_slot(directory, config, waiting, ready) if not scheduler.enabled else nullcontext():
                ready()
                # Initial media requests can be throttled before yt-dlp's
                # first progress event; the displayed stage is still extracting.
                session.failure_phase = 'transfer'
                started = time.monotonic()
                try:
                    yield
                finally:
                    session.transfer_seconds += time.monotonic() - started

    def attempt(self, channel, addresses, *, without_cookies=False, cookie_retry=False):
        if self.session.config.get('_delivery_contract'):
            from webapp.delivery.executor import attempt
            return attempt(self, channel, addresses, without_cookies=without_cookies, cookie_retry=cookie_retry)
        from webapp.downloader import build_ydl_options, download_cookie_session, require_download_tools
        from webapp.parallel_http import fragment_connection_budget, transfer_connection_units
        from webapp.throughput import automatic as automatic_throughput, runtime_limits
        session = self.session
        automatic = session.automatic
        base = session.base
        catalog = session.catalog
        config = session.config
        meter = session.meter
        output_files = session.output_files
        payload = session.payload
        progress = session.progress
        scheduler = session.scheduler
        store = session.store
        worker_slot = session.worker_slot
        youtube = session.youtube
        attempt = self.attempt
        transmitting = self.transmitting
        completed_file = partial(record_completed_file, session)
        ReportingDownloader = build_reporting_downloader(session)

        # Each provider needs its own error collector and Cookie policy.
        session.failure = DownloadFailure()
        session.failure_phase = 'resolve'
        session.transfer_seconds = 0
        provider = channel.adapter
        provider_config = execution_config(config, channel)
        if not cookie_retry:
            meter.begin(channel)
        options = build_ydl_options({**provider_config, '_use_youtube_cookie': not without_cookies}, base)
        if (without_cookies or (config.get('youtube_cookie_usage', 'auto') == 'never'
                                and not config.get('_cookie_pool_selected'))):
            options.pop('cookiefile', None)
        transfer_count = runtime_limits(config, store.directory if store else base / '.cache')['transfers']
        fragment_budget = fragment_connection_budget(config, channel.connection_limit, transfer_count)
        options['concurrent_fragment_downloads'] = fragment_budget
        options.update(retries=2, fragment_retries=2, extractor_retries=1)
        if payload.get('_restart_transfer') or automatic:
            # The providers can return different encodings for the same filename.
            # Never append bytes from another provider to an incomplete transfer.
            options['continuedl'] = False
        if catalog:
            options['download_archive'] = None
        options.update(post_hooks=[completed_file], progress_hooks=[progress.hook, meter.hook],
                       postprocessor_hooks=[progress.postprocess])
        require_download_tools(addresses, options)
        progress.state.update(download_method=provider, channel_id=channel.id, channel_label=channel.label,
                              upstream_group=channel.upstream_group, stage='extracting', percent=0, provider_percent=None,
                              downloaded_bytes=0, total_bytes=None, speed=None, eta=None, format='',
                              transfer_admitted=False, transfer_wait_resource=None)
        progress.streams = {}
        progress.save(force=True)
        @contextmanager
        def resolving():

            session.failure_phase = 'resolve'
            started = time.monotonic()
            def waiting():
                # Resolver/quota admission is shared by all providers,
                # including yt-dlp; browser slots are acquired separately.
                changed = progress.state.get('stage') != 'waiting_resolver'
                progress.state.update(stage='waiting_resolver', speed=None, eta=None)
                progress.save(force=changed)
            with scheduler.permit(channel, 'resolve', on_wait=waiting):
                progress.state.update(stage='extracting', speed=None, eta=None)
                progress.save(force=True)
                yield
            meter.resolved(time.monotonic() - started)
            scheduler.prepared()

        def transfer(info):
            return transmitting(channel, info, transfer_connection_units(info, fragment_budget,
                                native=provider == 'yt_dlp'))

        def retry_without_cookies(error=None):
            # Some authenticated sessions get a misleading "Video unavailable"
            # for public videos. Try once with a fresh anonymous cookie jar,
            # before any media was received, on the same provider and proxy.
            if (not config.get('youtube_cookie_fallback', False)
                    or cookie_retry or without_cookies or provider != 'yt_dlp' or not youtube or len(addresses) != 1
                    or not options.get('cookiefile') or output_files or progress.streams
                    or progress.state.get('downloaded_bytes') or session.failure.cookie_rejected
                    or 'your account has been rate-limited' in session.failure.message(error).lower()
                    or 'video unavailable' not in session.failure.message(error).lower()):
                return False
            print('当前 Cookie 会话返回 Video unavailable；使用同一节点不带 Cookie 重试一次。已上传的 Cookie 保持不变。', flush=True)
            event('youtube_cookie_fallback', provider=provider, reason='video_unavailable',
                  node_changed=False, cookie_file_preserved=True)
            return True

        def retry_with_cookies(error=None):
            # On-demand mode deliberately starts public videos anonymously.
            # If the extractor says that this video needs an account, retry
            # once with the selected Cookie before publishing a login error.
            if (not without_cookies or provider != 'yt_dlp' or not youtube
                    or cookie_retry or config.get('_cookie_pool_selected')
                    or not config.get('youtube_cookie')
                    or config.get('youtube_cookie_usage', 'auto') != 'auto'
                    or output_files or progress.streams or progress.state.get('downloaded_bytes')):
                return False
            text = session.failure.message(error).lower()
            challenge_text = text + ' ' + ' '.join([str(error or ''), *session.failure.errors]).lower()
            if any(marker in challenge_text for marker in ('bot', 'captcha', '机器人', '验证码')):
                return False
            markers = ('sign in to confirm', 'sign in to watch', 'login required',
                       'log in to watch', 'age-restricted', 'members-only',
                       'private video',
                       '需要登录', '要求登录', '仅限会员', '私密')
            if not any(marker in text for marker in markers):
                return False
            print('当前视频需要登录，使用已选 Cookie 重试一次。', flush=True)
            event('youtube_cookie_on_demand', provider=provider, reason='authentication_required',
                  cookie_file_preserved=True)
            return True

        def wait_request():
            progress.state.update(stage='waiting_request', speed=None, eta=None,
                                  request_wait_reason='video_start')
            progress.save()

        if youtube and (config.get('_cookie_pool_selected') or not (
                provider == 'yt_dlp' and config.get('youtube_pacing_mode') == 'adaptive')):
            scheduler.wait_youtube_start(channel, on_wait=wait_request)
            progress.state.update(stage='extracting')
            progress.save(force=True)

        def reject_auth(message):
            return YouTubeAuth(store).reject(message,
                request_mode='anonymous_fallback' if without_cookies else 'authenticated',
                node=config.get('_resolved_clash_node', ''), profile_id=config.get('_cookie_pool_selected'))

        try:
            with download_cookie_session(options) as session_options:
                with ReportingDownloader(session_options) as downloader:
                    if provider == 'downmp3':
                        from webapp.downmp3 import resolve

                        def api_stage(value, percent):
                            progress.state.update(stage=value, provider_percent=percent, speed=None, eta=None)
                            progress.save(force=True)

                        for url in addresses:
                            with resolving():
                                info = resolve(url, provider_config, base, api_stage)
                            with transfer(info):
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
                            with resolving():
                                info = resolve(url, provider_config, base, store.directory if store else base / '.cache', stage,
                                               worker_slot=worker_slot, browser_capacity=browser_capacity)
                            downloader.params['format'] = 'cobalt_video+cobalt_audio' if info.get('_cobalt_merge') else options['format']
                            downloader.format_selector = downloader.build_format_selector(downloader.params['format'])
                            with transfer(info):
                                downloader.process_ie_result(info, download=True)
                        failed = downloader._download_retcode
                    else:
                        if automatic_throughput(config) or config.get('youtube_cookie_pool') or config.get('youtube_pacing_mode') == 'adaptive':
                            for address in addresses:
                                with resolving():
                                    if (youtube and config.get('youtube_pacing_mode') == 'adaptive'
                                            and not config.get('_cookie_pool_selected')):
                                        scheduler.wait_youtube_start(channel, on_wait=wait_request)
                                    info = downloader.extract_info(address, download=False)
                                if info is None:
                                    raise yt_dlp.utils.DownloadError(session.failure.message())
                                with transfer(info):
                                    downloader.process_ie_result(info, download=True)
                            failed = downloader._download_retcode
                        else:
                            with transmitting(channel, {}, fragment_budget):
                                failed = downloader.download(addresses)
        except yt_dlp.utils.DownloadError as error:
            if retry_without_cookies(error):
                return attempt(channel, addresses, without_cookies=True, cookie_retry=True)
            if retry_with_cookies(error):
                return attempt(channel, addresses, without_cookies=False, cookie_retry=True)
            if store and youtube and provider == 'yt_dlp' and (session.failure.cookie_rejected or '要求登录验证' in session.failure.message(error)):
                raise YouTubeAuthRequired(reject_auth(session.failure.message(error))) from error
            raise session.failure.as_error(error) from error
        if store and youtube and provider == 'yt_dlp' and (session.failure.cookie_rejected or (failed and '要求登录验证' in session.failure.message())):
            raise YouTubeAuthRequired(reject_auth(session.failure.message()))
        if failed:
            if retry_without_cookies():
                return attempt(channel, addresses, without_cookies=True, cookie_retry=True)
            if retry_with_cookies():
                return attempt(channel, addresses, without_cookies=False, cookie_retry=True)
            cause = getattr(downloader, 'http_range_error', None)
            raise session.failure.as_error(cause) from cause
        return downloader.skipped
