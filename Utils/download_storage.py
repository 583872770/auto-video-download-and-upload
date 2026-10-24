"""Isolate disposable downloads from the user's permanent media directory."""
from pathlib import Path
import re
import shutil
import uuid


def storage_mode(config):
    mode = config.get('storage_mode', 'local')
    if mode == 'cloud':
        raise ValueError('指定网盘保存暂未开发，请选择指定目录或测试模式。')
    if mode not in ('local', 'test'):
        raise ValueError('请选择有效的视频保存方式。')
    return mode


class DownloadWorkspace:
    def __init__(self, config, state_directory, task_id=None):
        self.test_mode = storage_mode(config) == 'test'
        self.config = dict(config)
        self.directory = None
        if self.test_mode:
            identity = task_id or uuid.uuid4().hex
            if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
                raise ValueError('测试下载任务标识无效。')
            self.state_directory = Path(state_directory).resolve()
            self.root = self.state_directory / 'test-downloads'
            self.directory = self.root / identity
            self._verify()
            self.config.update(path=str(self.directory), dir='')

    def _verify(self):
        # Check the absolute target before creating or recursively removing it.
        if (self.root.is_symlink() or self.root.resolve() != self.root
                or self.directory.is_symlink() or self.directory.resolve() != self.directory):
            raise ValueError('测试下载目录无效，已停止文件操作。')

    def prepare(self):
        if self.test_mode:
            self._verify()
            self.directory.mkdir(parents=True, exist_ok=True)
        return self.config

    def cleanup(self):
        if self.test_mode:
            self._verify()
            if self.directory.exists():
                shutil.rmtree(self.directory)

