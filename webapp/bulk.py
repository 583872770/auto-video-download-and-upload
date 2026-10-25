"""Stream TXT/CSV imports with atomic chunk checkpoints and database deduplication."""
import csv
import json
from pathlib import Path
import time

from sqlalchemy import select

from webapp.database import imports
from webapp.settings import urls_from

CHUNK = 2000
REPORT_INTERVAL = 1.0
MAX_LINE = 16384


def create_import(store, import_id, path, filename, payload):
    with store.connect() as db:
        db.execute(imports.insert().values(id=import_id, path=str(path), name=filename,
                   kind=Path(filename).suffix.lower().lstrip('.'), offset=0, column_index=0,
                   processed=0, added=0, duplicates=0, invalid=0, samples='[]', finished=0))
        task_id = store._create(db, 'import', {**payload, 'import_id': import_id}, store.db.sequence(db, 'task'))
    return store.get(task_id)


def run_import(store, payload, report):
    identity = payload['import_id']
    with store.connect() as db:
        job = dict(db.execute(select(imports).where(imports.c.id == identity)).mappings().one())
    if job['finished']:
        return {'message': f'导入完成：新增 {job["added"]}，重复 {job["duplicates"]}，无效 {job["invalid"]}。',
                **{key: job[key] for key in ('processed', 'added', 'duplicates', 'invalid')},
                'samples': json.loads(job['samples']), 'batch_id': identity}
    path = Path(job['path'])
    if not path.resolve().is_relative_to((store.directory / 'imports').resolve()):
        raise ValueError('导入文件路径无效。')
    total = path.stat().st_size
    batch_payload = {key: value for key, value in payload.items()
                     if key not in ('import_id', 'urls', 'config', 'clash', '_execution_settings')}
    batch_payload.update(batch_id=identity, batch_name=job['name'])
    shared_snapshot = store.prepare_snapshot(batch_payload)
    column_index = job['column_index']
    samples = json.loads(job['samples'])

    # Reuse this connection, but commit each chunk independently. NullPool is
    # intentional elsewhere; a remote login for every chunk wastes many RTTs.
    with path.open('rb') as stream, store.db.engine.connect() as connection:
        stream.seek(job['offset'])
        report_started = time.monotonic()
        report_offset, report_processed = job['offset'], job['processed']
        last_report = None

        def lines():
            while True:
                raw = stream.readline(MAX_LINE + 1)
                if not raw:
                    return
                if len(raw) > MAX_LINE:
                    raise ValueError('导入文件中存在超过 16 KB 的行，请检查文件格式。')
                yield raw.decode('utf-8-sig')

        rows = csv.reader(lines()) if job['kind'] == 'csv' else ([line.rstrip('\r\n')] for line in lines())
        chunk, processed, invalid = [], 0, 0

        def commit(finished=False):
            nonlocal chunk, processed, invalid, job
            nonlocal report_started, report_offset, report_processed, last_report
            with connection.begin():
                db = connection
                current = db.execute(store._locked(select(imports).where(imports.c.id == identity))).mappings().one()
                if current['offset'] != job['offset']:
                    raise RuntimeError('导入检查点已改变，请等待原导入任务完成。')
                accepted = store.insert_downloads(db, chunk, deduplicate=True, shared_snapshot=shared_snapshot)
                values = dict(offset=stream.tell(), column_index=column_index,
                              processed=current['processed'] + processed, added=current['added'] + len(accepted),
                              duplicates=current['duplicates'] + len(chunk) - len(accepted),
                              invalid=current['invalid'] + invalid, samples=json.dumps(samples, ensure_ascii=False), finished=int(finished))
                db.execute(imports.update().where(imports.c.id == identity).values(**values))
            job = {**job, **values}
            stamp = time.monotonic()
            if finished or last_report is None or stamp - last_report >= REPORT_INTERVAL:
                elapsed = max(stamp - report_started, 0.001)
                speed = (job['offset'] - report_offset) / elapsed
                report({'stage': 'importing', 'title': job['name'], 'processed': job['processed'],
                        'added': job['added'], 'duplicates': job['duplicates'], 'invalid': job['invalid'],
                        'downloaded_bytes': job['offset'], 'total_bytes': total,
                        'percent': round(100 * job['offset'] / total, 1) if total else 100,
                        'records_per_second': (job['processed'] - report_processed) / elapsed,
                        'speed': speed, 'eta': 0 if finished else (total - job['offset']) / speed if speed else None})
                report_started, last_report = stamp, stamp
                report_offset, report_processed = job['offset'], job['processed']
            chunk, processed, invalid = [], 0, 0

        first = job['offset'] == 0
        for row in rows:
            if first:
                first = False
                header = [cell.strip().casefold() for cell in row]
                names = ('video_id', 'id', 'url', 'link', '视频id', '视频链接')
                if any(name in header for name in names):
                    column_index = next(header.index(name) for name in names if name in header)
                    continue
            if not row or not any(cell.strip() for cell in row):
                continue
            processed += 1
            try:
                value = row[column_index].strip()
                url = urls_from([value])[0]
                chunk.append({**batch_payload, 'urls': [url]})
            except (ValueError, IndexError):
                invalid += 1
                if len(samples) < 10:
                    samples.append(f'第 {job["processed"] + processed} 条记录格式无效')
            if processed >= CHUNK:
                commit()
        commit(finished=True)
    return {'message': f'导入完成：新增 {job["added"]}，重复 {job["duplicates"]}，无效 {job["invalid"]}。',
            **{key: job[key] for key in ('processed', 'added', 'duplicates', 'invalid')}, 'samples': samples, 'batch_id': identity}
