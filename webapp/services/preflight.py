"""DownloadPreflight operations independent of route registration."""
from flask import g, has_request_context
from webapp.config.settings import download_methods, validate_method_urls
from webapp.youtube_auth import YouTubeAuthRequired, is_youtube
from webapp.yt5s import BrowserEnvironmentRequired
from webapp.environment import DownloadEnvironmentRequired


class DownloadPreflight:
    def __init__(self, context):
        self.context = context

    def verify_youtube(self, config, base, urls, snapshot=None, *, force=False):
        methods = download_methods(config)
        if 'yt1d' in methods:
            self.context.dependencies.require_yt1d_environment(config, base)
        if 'bergung' in methods:
            from webapp.bergung import require_environment
            require_environment(config, base)
        if 'downmp3' in methods:
            self.context.dependencies.require_downmp3_environment()
        if 'yt5s' in methods and 'yt_dlp' in methods:
            self.context.dependencies.require_fallback_environment(config, base)
        elif 'yt5s' in methods:
            self.context.dependencies.require_browser_environment(config, base)
        if 'yt_dlp' not in methods:
            return None
        if not is_youtube(urls):
            return None
        if config.get('youtube_cookie_pool') and not force:
            # Each admitted account is checked by its worker. The selected
            # legacy/default Cookie may be cooling while other accounts work.
            return None
        # In on-demand mode public videos start anonymously.  The worker will
        # retry once with the selected Cookie only when yt-dlp reports that
        # authentication is required.  Explicit checks from the settings page
        # still call this function with force=True and therefore remain useful.
        if (config.get('youtube_cookie_usage', 'auto') in ('auto', 'never')
                and not config.get('youtube_cookie_pool') and not force):
            return None
        options = {'force': force or len(methods) > 1}
        if config.get('proxy_mode') == 'clash':
            snapshot = snapshot or self.context.clash.snapshot()
            options.update(proxy=snapshot['proxy'], node=snapshot['node'])
        return self.context.youtube_auth.require(config, base, **options)

    def activate_downloads(self, stage, cancelled):
        stage('checking_database')
        if self.context.store.media_clear_busy():
            raise ValueError('素材库正在清空，请完成后再开始下载。')
        queue = self.context.store.download_queue()
        if queue['paused'] and queue['running']:
            raise ValueError('下载进程正在停止，请稍后再开始。')
        if not queue['queued'] and not queue['importing']:
            raise ValueError('没有待处理的视频，请先添加清单。')
        if self.context.fleet and self.context.fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            preflight_instances = self.context.dependencies.preflight_instances
            stage('checking_instances')
            policy_version = preflight_instances(self.context.fleet, stage, cancelled)
            if cancelled.is_set():
                raise InterruptedError('启动已取消。')
            stage('releasing_queue')
            self.context.store.activate_download_lists(expected_policy_version=policy_version)
            return
        config, base = self.context.settings()
        stage('reading_queue')
        upcoming = self.context.store.page(limit=1, actions=('download',), status='queued', order='asc')['items']
        urls = [upcoming[0]['url']] if upcoming else ['https://www.youtube.com/']
        if upcoming:
            validate_method_urls(config, urls)
        stage('preparing_proxy')
        snapshot = self.context.clash.snapshot() if config.get('proxy_mode') == 'clash' else None
        stage('checking_environment')
        methods = download_methods(config)
        if 'yt1d' in methods:
            self.context.dependencies.require_yt1d_environment(config, base)
        if 'bergung' in methods:
            from webapp.bergung import require_environment
            require_environment(config, base)
        if 'downmp3' in methods:
            self.context.dependencies.require_downmp3_environment()
        if 'yt5s' in methods:
            (self.context.dependencies.require_fallback_environment if 'yt_dlp' in methods else self.context.dependencies.require_browser_environment)(config, base)
        elif 'yt_dlp' in methods:
            from webapp.environment import require_native_environment
            require_native_environment(config)
        if ('yt_dlp' in methods and is_youtube(urls) and not config.get('youtube_cookie_pool')
                and config.get('youtube_cookie_usage', 'auto') == 'always'):
            stage('checking_login')
            self.context.youtube_auth.require(config, base, force=True, **(
                {'proxy': snapshot['proxy'], 'node': snapshot['node']} if snapshot else {}))
        with self.context.media_lock:
            if cancelled.is_set():
                raise InterruptedError('启动已取消，清单仍在待处理队列。')
            if self.context.settings()[0] != config:
                raise ValueError('启动检查期间配置发生变化，请按已保存的新配置重新开始。')
            stage('releasing_queue')
            self.context.youtube_auth.clear_block()
            self.context.store.activate_download_lists()

    def job_preflight(self, config, base):
        if self.context.fleet and self.context.fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            rpc = self.context.dependencies.rpc
            nodes = [node for node in self.context.fleet.list()['items'] if node['role'] in ('worker', 'hybrid') and node['online'] and not node['paused']]
            warnings = []
            if not nodes:
                return ['任务已开始，等待在线且启用的下载实例。']
            for node in nodes:
                try:
                    rpc(self.context.fleet, node['id'], '/api/fleet/preflight', method='POST', data={})
                except (ValueError, YouTubeAuthRequired, BrowserEnvironmentRequired, DownloadEnvironmentRequired) as error:
                    self.context.fleet.quarantine(node['id'], str(error), boot_id=node['boot_id'])
                    warnings.append(f'{node["name"]} 已暂停领取：{error}')
            return warnings
        methods = download_methods(config)
        if 'yt1d' in methods:
            self.context.dependencies.require_yt1d_environment(config, base)
        if 'bergung' in methods:
            from webapp.bergung import require_environment
            require_environment(config, base)
        if 'downmp3' in methods:
            self.context.dependencies.require_downmp3_environment()
        if 'yt5s' in methods:
            (self.context.dependencies.require_fallback_environment if 'yt_dlp' in methods else self.context.dependencies.require_browser_environment)(config, base)
        elif 'yt_dlp' in methods:
            from webapp.environment import require_native_environment
            require_native_environment(config)
        if ('yt_dlp' in methods and not config.get('youtube_cookie_pool') and
                config.get('youtube_cookie_usage', 'auto') == 'always'):
            self.context.youtube_auth.require(config, base, force=True)

    def prepare_system_resume(self):
        if self.context.fleet:
            return  # Nodes are checked on enable/start and isolate their own failures.
        from sqlalchemy import select
        from webapp.database import tasks as task_table
        with self.context.store.connect() as db:
            upcoming = db.execute(select(task_table.c.batch_id, task_table.c.url).where(task_table.c.action == 'download',
                task_table.c.status == 'queued', self.context.store.released_condition()).order_by(task_table.c.seq).limit(1)).first()
        if upcoming:
            config, base = self.context.settings()
            self.verify_youtube(config, base,
                [upcoming.url] if upcoming.url else ['https://www.youtube.com/'],
                force=not config.get('youtube_cookie_pool') and config.get('youtube_cookie_usage', 'auto') == 'always')
        self.context.youtube_auth.clear_block()
