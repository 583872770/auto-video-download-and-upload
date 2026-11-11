"""DownloadQueueService operations independent of route registration."""
from pathlib import Path
import uuid
from flask import g, has_request_context
from webapp.config.settings import DEFAULTS, urls_from, validate_settings, validate_method_urls


ACTIONS = ('download', 'check')


class DownloadQueueService:
    def __init__(self, context, preflight):
        self.context = context
        self.preflight = preflight

    def create_task(self, data, *, batch=False):
        action = 'download' if batch else data.get('action')
        if action not in ACTIONS:
            raise ValueError('不支持的任务类型。')
        config, base = self.context.settings()
        # Check the current environment; pending downloads bind settings at claim.
        config = {**DEFAULTS, **config}
        validate_settings(config, download_only=action == 'download' or (action == 'check' and data.get('scope') == 'download'))
        payload = {'config': config, 'base': str(base)}
        if data.get('scope') == 'download':
            payload['scope'] = 'download'
        if action == 'download':
            payload['urls'] = urls_from(data.get('urls') or config.get('url'))
            validate_method_urls(config, payload['urls'])
        with self.context.media_lock:
            local_download = action == 'download' and not (self.context.fleet and self.context.fleet.can_manage and not (has_request_context() and g.fleet_rpc))
            if local_download and config.get('proxy_mode') == 'clash':
                payload['clash'] = self.context.clash.snapshot()
            if action == 'download':
                if local_download:
                    self.preflight.verify_youtube(config, base, payload['urls'], payload.get('clash'))
                payload.pop('config', None)
                payload.pop('clash', None)
            if batch:
                batch_id = uuid.uuid4().hex
                payloads = [{**payload, 'urls': [url], 'batch_id': batch_id} for url in payload['urls']]
                return dict(batch_id=batch_id, items=self.context.store.create_batch(payloads)), 202
            task = self.context.store.create(action, payload)
        return dict(task), 202

    def enqueue_downloads(self, data):
        urls = urls_from(data.get('urls'))
        identity = uuid.uuid4().hex
        _, base = self.context.settings()
        with self.context.store.connect() as db:
            self.context.store.lists.create(db, identity, data)
            ids = self.context.store.insert_downloads(db, [{'base': str(base), 'scope': 'download', 'batch_id': identity,
                'urls': [url]} for url in urls], deduplicate=True)
        return dict(batch_id=identity, added=len(ids), duplicates=len(urls) - len(ids),
                       message='已添加到待处理清单，点击开始下载后执行。'), 202

    def import_downloads(self, source, list_data, config):
        if not source or not source.filename or Path(source.filename).suffix.lower() not in ('.txt', '.csv'):
            raise ValueError('请选择 UTF-8 编码的 TXT 或 CSV 文件。')
        _, base = self.context.settings()
        payload = {'base': str(base), 'scope': 'download'}
        identity = uuid.uuid4().hex
        directory = self.context.store.directory / 'imports'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (identity + Path(source.filename).suffix.lower())
        try:
            from webapp.disk_uploads import save_import
            save_import(source, path, self.context.store, config)
            with self.context.media_lock:
                from webapp.bulk import create_import
                task = create_import(self.context.store, identity, path, Path(source.filename).name, payload, list_data=list_data)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return dict(task), 202

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
