"""Account multipart spooling before Flask exposes request.files."""
from pathlib import Path

from flask import Request, current_app
from werkzeug.exceptions import RequestEntityTooLarge

from webapp.disk_budget import DiskBudget, MIB


class UploadStream:
    def __init__(self, budget, maximum):
        self.lease = budget.lease('imports')
        self.lease.__enter__()
        self.stream = (self.lease.path / 'upload').open('w+b')
        self.maximum = maximum
        self.length = 0
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, value):
        needed = self.length + len(value)
        if needed > self.maximum:
            raise RequestEntityTooLarge('上传文件超过本实例允许的大小。')
        self.lease.check(needed, force=True)
        count = self.stream.write(value)
        self.length += count
        return count

    def close(self):
        if not self.closed:
            self.closed = True
            self.stream.close()
            self.lease.__exit__(None, None, None)


class BudgetRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        store = current_app.extensions.get('task_store')
        if store is None:
            return super()._get_file_stream(total_content_length, content_type, filename, content_length)
        config = current_app.extensions['disk_config']()
        maximum = int(config.get('disk_upload_limit_mb', 256)) * MIB if self.path.endswith('/imports') or self.path == '/api/downloads/import' else current_app.config['MAX_CONTENT_LENGTH']
        stream = UploadStream(DiskBudget(store.directory, config), maximum)
        if not hasattr(self, '_budget_streams'):
            self._budget_streams = []
        self._budget_streams.append(stream)
        return stream

    def close(self):
        try:
            super().close()
        finally:
            for stream in getattr(self, '_budget_streams', []):
                stream.close()


def save_import(source, path, store, config):
    from werkzeug.exceptions import BadRequest
    budget = DiskBudget(store.directory, config)
    maximum = int(config.get('disk_upload_limit_mb', 256)) * MIB
    with budget.lease('imports') as lease:
        temporary = lease.path / 'source'
        length = 0
        with temporary.open('wb') as target:
            while chunk := source.stream.read(128 * 1024):
                length += len(chunk)
                if length > maximum:
                    raise RequestEntityTooLarge('导入文件超过本实例上传上限。')
                lease.check(length, force=True)
                target.write(chunk)
        if not length:
            raise BadRequest('导入文件不能为空。')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        temporary.replace(path)
        budget.snapshot(force=True)
