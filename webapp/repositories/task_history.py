"""TaskHistory contracts used by the composed TaskStore repository."""
import codecs
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import select

from webapp.database import archive, settings
from webapp.config.schema import RUNTIME_FIELDS


class TaskHistory:
    def set_runtime(self, config):
        value = json.dumps({key: config[key] for key in RUNTIME_FIELDS if key in config})
        with self.connect() as db:
            db.execute(self.db.upsert(settings, dict(key='runtime', value=value), ['key']))

    def runtime(self):
        if self.fleet:
            return self.fleet.local_settings()['config']
        with self.connect() as db:
            value = db.execute(select(settings.c.value).where(settings.c.key == 'runtime')).scalar_one_or_none()
            return json.loads(value) if value else {}

    def archive_has(self, key, job_id=None):
        if job_id:
            key = 'job:' + job_id + ':' + key
        with self.connect() as db:
            return db.execute(select(archive.c.key).where(archive.c.key == key)).first() is not None

    def archive_add(self, key, path=None, job_id=None):
        if job_id:
            key = 'job:' + job_id + ':' + key
        with self.connect() as db:
            self.db.ensure(db, archive, {'key': key, 'created_at': self._timestamp(), 'path': path})

    def import_archive(self, base):
        path = Path(base) / 'videos.txt'
        if not path.is_file():
            return
        source = (self.fleet.id + '\0' if self.fleet else '') + str(path.resolve())
        key = 'archive:' + hashlib.sha256(source.encode()).hexdigest()
        with self.connect() as db:
            offset = db.execute(select(settings.c.value).where(settings.c.key == key)).scalar_one_or_none()
        offset = int(offset or 0)
        if offset > path.stat().st_size:
            offset = 0
        with path.open('rb') as stream:
            stream.seek(offset)
            while True:
                lines = []
                for _ in range(1000):
                    line = stream.readline(16385)
                    if not line:
                        break
                    if len(line) < 16385:
                        value = line.decode('utf-8', errors='replace').strip()
                        if value:
                            lines.append({'key': value, 'created_at': self._timestamp()})
                if not lines:
                    break
                with self.connect() as db:
                    db.execute(self.db.ignore(archive, lines))
                    db.execute(self.db.upsert(settings, dict(key=key, value=str(stream.tell())), ['key']))

    def log_path(self, task_id, *, create=True):
        if len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
            raise ValueError('无效的任务 ID')
        legacy = self.directory / f'{task_id}.log'
        if legacy.is_file():
            return legacy
        path = self.directory / 'logs' / task_id[:2] / task_id[2:4] / f'{task_id}.log'
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def logs(self, task_id, offset=0, size=65536):
        path = self.log_path(task_id, create=False)
        if not path.exists():
            return {'text': '', 'offset': 0, 'state': 'missing'}
        try:
            base = int(path.with_suffix('.offset').read_text())
        except (OSError, ValueError):
            base = 0
        try:
            with path.open('rb') as stream:
                stream.seek(min(max(offset - base, 0), os.fstat(stream.fileno()).st_size))
                chunk = stream.read(min(65536, max(0, size)))
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                text = decoder.decode(chunk, final=False)
                pending, _ = decoder.getstate()
                return {'text': text, 'offset': base + stream.tell() - len(pending), 'pruned': offset < base, 'state': 'available'}
        except FileNotFoundError:
            return {'text': '', 'offset': 0, 'state': 'missing'}

