"""DashboardService operations independent of route registration."""
import importlib.util
import shutil
import sys
import time
from Utils.config import resolve_path, video_directory


class DashboardService:
    def __init__(self, context):
        self.context = context
        self._capability_cache = None

    def _capabilities(self, config, base):
        """Cache static executable checks while keeping config changes visible."""
        executable = str(resolve_path(base, config['browser_executable'])) if config.get('browser_executable') else ''
        key = (executable, config.get('browser_executable', ''))
        now = time.monotonic()
        cached = self._capability_cache
        if cached and cached[0] > now - 60 and cached[1] == key:
            return cached[2]
        tools = {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe', 'node', 'deno')}
        tools['playwright'] = importlib.util.find_spec('playwright') is not None
        from webapp.browser import browser_executable, display_available
        try:
            tools['login_browser'] = bool(browser_executable(executable))
        except RuntimeError:
            tools['login_browser'] = False
        tools['login_display'] = display_available()
        self._capability_cache = (now, key, tools)
        return tools

    def status(self):
        config, base = self.context.settings()
        tools = self._capabilities(config, base)
        youtube = {'login': self.context.app.extensions['youtube_login'].status(),
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
            waiting_tasks=snapshot['waiting_tasks'],
            global_limit=snapshot['budget'], global_running=snapshot['global_running'],
            paused=snapshot['paused'], pending_count=snapshot['pending_count'])
        from webapp.risk_diagnostics import observed, local_health
        concurrency['local_health'] = local_health(self.context.store.directory, config, self.context.clash.directory)
        concurrency['local_health']['progress_cache'] = self.context.store.progress_cache.health()
        concurrency['risks'] = observed(config, concurrency, youtube['auth'], stamp,
                                       error=snapshot['error'])
        concurrency['auth_checked_at'] = youtube['auth'].get('checked_at')
        concurrency['local_processing'] = snapshot['local_processing']
        return dict(tools=tools, startup=self.context.startup.status(), python=sys.version.split()[0], database=self.context.store.db.engine.dialect.name,
                       throughput=rate, concurrency=concurrency,
                       workers=self.context.store.workers(), download_queue=queue,
                       execution=self.context.app.extensions['execution_control'].status(queue),
                       connection_busy=self.context.store.download_connection_busy(),
                       youtube=youtube, storage_mode=config.get('storage_mode', 'local'),
                       media_directory=str(video_directory(config, base)), timezone='服务器本地时间')
