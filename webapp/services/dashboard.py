"""DashboardService operations independent of route registration."""
import importlib.util
import shutil
import sys
from flask import g, has_request_context
from Utils.config import resolve_path, video_directory
from webapp.config.settings import download_methods


class DashboardService:
    def __init__(self, context):
        self.context = context

    def status(self):
        config, base = self.context.settings()
        tools = {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe', 'node', 'deno')}
        tools['playwright'] = importlib.util.find_spec('playwright') is not None
        from webapp.yt5s import browser_executable, display_available
        try:
            tools['yt5s_browser'] = bool(browser_executable(
                str(resolve_path(base, config['browser_executable'])) if config.get('browser_executable') else ''))
        except RuntimeError:
            tools['yt5s_browser'] = False
        tools['yt5s_display'] = display_available()
        youtube_file = resolve_path(base, config['youtube_cookie']) if config.get('youtube_cookie') else None
        youtube = {'saved': bool(youtube_file and youtube_file.is_file() and youtube_file.stat().st_size),
                   'path': str(youtube_file) if youtube_file else None, 'login': self.context.app.extensions['youtube_login'].status(),
                   'auth': self.context.youtube_auth.status(), 'auth_block': self.context.youtube_auth.block_reason()}
        queue = self.context.store.download_queue()
        from webapp.throughput import runtime_limits
        from webapp.concurrency_diagnostics import diagnose
        from webapp.repositories.dashboard import concurrency_inputs
        rate = runtime_limits(config, self.context.store.directory)
        snapshot = concurrency_inputs(self.context.store, config, base, self.context.fleet, queue)
        stamp = snapshot['stamp']
        concurrency = diagnose(config, snapshot['identity'], snapshot['progress'], rate,
            snapshot['context'], stamp, cookie_profiles=snapshot['profiles'], current_tasks=snapshot['current_tasks'],
            global_limit=snapshot['budget'], global_running=snapshot['global_running'],
            paused=snapshot['paused'])
        concurrency['subtitles'] = snapshot['subtitles']
        from webapp.risk_diagnostics import observed, local_health
        concurrency['local_health'] = local_health(self.context.store.directory, config, self.context.clash.directory)
        concurrency['local_health']['progress_cache'] = self.context.store.progress_cache.health()
        concurrency['risks'] = observed(config, concurrency, youtube['auth'], stamp,
                                       error=snapshot['error'])
        concurrency['auth_checked_at'] = youtube['auth'].get('checked_at')
        return dict(tools=tools, accounts={}, startup=self.context.startup.status(), python=sys.version.split()[0], database=self.context.store.db.engine.dialect.name,
                       throughput=rate, concurrency=concurrency,
                       workers=self.context.store.workers(), worker_count=config.get('worker_count', 1),
                       yt5s_browser_count=config.get('yt5s_browser_count', 2), download_queue=queue,
                       execution=self.context.app.extensions['execution_control'].status(queue),
                       connection_busy=self.context.store.download_connection_busy(),
                       youtube=youtube, storage_mode=config.get('storage_mode', 'local'),
                       download_method=config.get('download_method', 'yt_dlp'),
                       download_methods=download_methods(config),
                       media_directory=str(video_directory(config, base)), timezone='服务器本地时间')

    def overview(self):
        config, base = self.context.settings()
        root = video_directory(config, base)
        accounting = self.context.store.statistics.dashboard()
        if self.context.fleet and self.context.fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            from webapp.repositories.dashboard import fleet_media_overview
            total, recent = fleet_media_overview(self.context.store)
            return dict(counts=self.context.store.counts(actions=('download', 'subtitles', 'check', 'import', 'index', 'clear_media')),
                download_counts=self.context.store.counts(actions=('download',)), accounting=accounting,
                media={'count': int(total)}, recent_media=recent,
                recent_tasks=self.context.store.page(limit=5, actions=('download', 'subtitles', 'check', 'import', 'index', 'clear_media'), finished_only=True)['items'])
        return dict(counts=self.context.store.counts(actions=('download', 'subtitles', 'check', 'import', 'index', 'clear_media')),
                       download_counts=self.context.store.counts(actions=('download',)), accounting=accounting,
                       media=self.context.catalog.root_status(root), recent_media=self.context.catalog.page(root, limit=4)['items'],
                       recent_tasks=self.context.store.page(limit=5, actions=('download', 'subtitles', 'check', 'import', 'index', 'clear_media'), finished_only=True)['items'])
