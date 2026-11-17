"""DownloadQueueService operations independent of route registration."""


class DownloadQueueService:
    def __init__(self, context, preflight):
        self.context = context
        self.preflight = preflight




    def stop_downloads(self):
        from webapp.control import stop_downloads as stop_workers
        self.context.startup.cancel()
        with self.context.media_lock:
            self.context.store.set_downloads_paused(True)
            stop_workers(self.context.store)
            queue = self.context.store.download_queue()
        message = '正在暂停当前下载，排队任务已保留。' if queue['running'] else '下载已暂停，待处理清单和部分文件已保留。'
        return dict(message=message, download_queue=queue), 202 if queue['running'] else 200

    def resume_downloads(self):
        with self.context.media_lock:
            return dict(self.context.app.extensions['execution_control'].resume_system())
