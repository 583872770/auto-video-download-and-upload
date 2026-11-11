"""TaskCodec contracts used by the composed TaskStore repository."""
import hashlib
import json

from sqlalchemy import select

from webapp.database import snapshots
from webapp.store_utils import source_identity


_LIVE_UNSET = object()


class TaskCodec:
    def decode(self, row, private=False, db=None, *, live=_LIVE_UNSET):
        if row is None:
            return None
        task = dict(row)
        payload = json.loads(task.pop('payload'))
        task['result'] = json.loads(task['result']) if task.get('result') else None
        task['progress'] = json.loads(task['progress']) if task.get('progress') else None
        if live is _LIVE_UNSET:
            live = self.progress_cache.read(task)
        if live:
            task['progress'] = live['progress']
            task['progress_updated_at'] = live['updated_at']
        url = task.get('url') or (payload.get('urls') or [None])[0]
        task.update(batch_id=task.get('batch_id') or payload.get('batch_id'), url=url,
                    video_id=task.get('video_id') or source_identity(url)[0])
        if private:
            snapshot_id = payload.pop('snapshot_id', None)
            if snapshot_id:
                if db is None:
                    with self.connect() as connection:
                        saved = connection.execute(select(snapshots.c.payload).where(snapshots.c.id == snapshot_id)).scalar_one()
                else:
                    saved = db.execute(select(snapshots.c.payload).where(snapshots.c.id == snapshot_id)).scalar_one()
                payload = {**json.loads(saved), **payload}
            task['payload'] = payload
        return task

    def decode_many(self, rows):
        rows = list(rows)
        values = self.progress_cache.read_many(rows)
        return [self.decode(row, live=values.get(row['id'])) for row in rows]

    def prepare_snapshot(self, payload):
        common = {key: value for key, value in payload.items() if key not in ('urls', 'batch_id', 'import_id')}
        encoded = json.dumps(common, ensure_ascii=False, sort_keys=True)
        identity = hashlib.sha256(encoded.encode()).hexdigest()
        return identity, encoded

    def _pack(self, db, payload):
        identity, encoded = self.prepare_snapshot(payload)
        self.db.ensure(db, snapshots, {'id': identity, 'payload': encoded})
        return json.dumps({**{key: value for key, value in payload.items() if key in ('urls', 'batch_id', 'import_id')},
                           'snapshot_id': identity}, ensure_ascii=False)

