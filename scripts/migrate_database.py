"""Copy legacy SQLite into PostgreSQL, verify counts, then atomically activate it."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
from webapp.database import archive, batches, counters, reservations, rotations, sequences, settings, tasks
from webapp.store import TaskStore, now, source_identity
from webapp.catalog import MediaCatalog
from Utils.config import load_config, video_directory

ROOT = Path(__file__).resolve().parents[1]


def migrate(state, url, config_path, *, activate=False):
    state, config_path = Path(state).resolve(), Path(config_path).resolve()
    source = state / 'tasks.sqlite3'
    if not source.is_file():
        raise ValueError('未找到需要迁移的 SQLite 数据库。')
    backup = ROOT / '.cache' / 'backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True)
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(backup / 'tasks.sqlite3')) as saved:
        original.backup(saved)
    shutil.copy2(config_path, backup / 'config.yaml')
    if (state / 'database.json').is_file():
        shutil.copy2(state / 'database.json', backup / 'database.json')
    print('Backup:', backup)
    store = TaskStore(state, url)
    if not store.db.postgres:
        raise ValueError('迁移目标必须是 PostgreSQL。')
    original = sqlite3.connect(backup / 'tasks.sqlite3')
    original.row_factory = sqlite3.Row
    expected = original.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    reader = original.execute('SELECT rowid AS legacy_seq, * FROM tasks ORDER BY rowid')
    copied = 0
    while chunk := reader.fetchmany(500):
        with store.connect() as db:
            for raw in chunk:
                raw = dict(raw)
                if db.execute(select(tasks.c.id).where(tasks.c.id == raw['id'])).first():
                    continue  # Safe to resume a migration interrupted after a committed chunk.
                payload = json.loads(raw['payload'])
                url_value = (payload.get('urls') or [None])[0]
                video_id, key = source_identity(url_value)
                progress = json.loads(raw['progress']) if raw.get('progress') else {}
                result = json.loads(raw['result']) if raw.get('result') else {}
                status = raw['status'] if raw['status'] != 'running' else 'interrupted'
                row = dict(id=raw['id'], seq=raw.get('seq') or raw['legacy_seq'], action=raw['action'], status=status,
                           created_at=raw['created_at'], started_at=raw.get('started_at'), finished_at=raw.get('finished_at'),
                           payload=store._pack(db, payload), result=raw.get('result'), error=raw.get('error'),
                           progress=raw.get('progress'), batch_id=payload.get('batch_id'), video_id=video_id, source_key=key,
                           url=url_value, title=str(progress.get('title') or '').casefold(), attempts=raw.get('attempts', 0),
                           max_attempts=raw.get('max_attempts', 3), available_at=0)
                db.execute(tasks.insert().values(**row))
                store._transition(db, {**row, 'status': None}, status)
                if row['batch_id']:
                    store.db.ensure(db, batches, {'id': row['batch_id'], 'seq': store.db.sequence(db, 'batch'),
                                                 'created_at': row['created_at'], 'name': ''})
                if row['action'] == 'download' and status not in ('failed', 'interrupted'):
                    store.db.ensure(db, reservations, {'key': key, 'task_id': row['id']})
                if progress.get('rotation_id') and status in ('failed', 'succeeded') and not result.get('skipped'):
                    identity, field = progress['rotation_id'], 'failed' if status == 'failed' else 'succeeded'
                    store.db.ensure(db, rotations, {'id': identity, 'succeeded': 0, 'failed': 0})
                    db.execute(rotations.update().where(rotations.c.id == identity).values({field: rotations.c[field] + 1}))
                copied += 1
    original.close()
    with store.connect() as db:
        actual = db.execute(select(func.count()).select_from(tasks)).scalar_one()
        maximum = db.execute(select(func.coalesce(func.max(tasks.c.seq), 0))).scalar_one()
        store.db.ensure(db, sequences, {'key': 'task', 'value': maximum})
        db.execute(sequences.update().where(sequences.c.key == 'task').values(value=maximum))
    if actual != expected:
        raise RuntimeError(f'迁移数量核对失败：源 {expected}，目标 {actual}；尚未切换数据库。')
    config, base = load_config(config_path)
    store.import_archive(base)
    catalog_result = MediaCatalog(store).reindex(video_directory(config, base))
    print('Tasks verified:', actual, '; new rows copied:', copied)
    print(catalog_result['message'])
    if activate:
        temporary = state / 'database.json.tmp'
        temporary.write_text(json.dumps({'url': store.db.url}), encoding='utf-8')
        temporary.replace(state / 'database.json')
        print('PostgreSQL activated. Original SQLite and media files were retained.')
    return {'tasks': actual, 'backup': str(backup)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=ROOT / '.cache/web')
    parser.add_argument('--connection-file', type=Path, default=ROOT / '.cache/postgres/database.json')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.yaml')
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    migrate(args.state, json.loads(args.connection_file.read_text(encoding='utf-8'))['url'], args.config, activate=args.activate)
