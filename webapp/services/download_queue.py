"""DownloadQueueService operations independent of route registration."""


class DownloadQueueService:
    def __init__(self, context):
        self.context = context

    def stop_downloads(self):
        from webapp.control import stop_downloads as stop_workers
        from webapp.disk_budget import DiskBudget
        self.context.startup.cancel()
        with self.context.media_lock:
            self.context.store.set_downloads_paused(True, discard_temporary=True)
            stop_workers(self.context.store)
            queue = self.context.store.download_queue()
            cleaned = not queue['running'] and DiskBudget(
                self.context.store.directory, self.context.store.runtime()).cleanup_stopped_downloads(self.context.store)
        message = '下载已停止，未完成视频的临时文件已清理。' if cleaned else '正在停止下载并清理未完成视频的临时文件。'
        return dict(message=message, download_queue=queue), 202 if queue['running'] else 200
