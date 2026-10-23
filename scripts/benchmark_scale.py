"""Reproducible metadata-only capacity check in a disposable PostgreSQL database."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import statistics
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil
import psycopg
from psycopg import sql
from sqlalchemy import func, select

from app import create_app
from webapp.bulk import create_import, run_import
from webapp.catalog import MediaCatalog
from webapp.database import media, roots, tasks
from webapp.store import TaskStore

ROOT = Path(__file__).resolve().parents[1]


def benchmark(rows):
    private = json.loads((ROOT / '.cache/postgres/connection.json').read_text(encoding='utf-8'))
    name = 'bench_video_' + uuid.uuid4().hex[:12]
    output = ROOT / '.cache/validation' / name
    output.mkdir(parents=True)
    admin = psycopg.connect(host='127.0.0.1', port=private['port'], user=private['user'], password=private['password'],
                           dbname='postgres', autocommit=True)
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    stop, memory = threading.Event(), []

    def monitor():
        process = psutil.Process()
        while not stop.wait(0.2):
            memory.append(process.memory_info().rss)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    report = {'rows': rows, 'metadata_only': True, 'actual_videos_downloaded': 0, 'database': 'PostgreSQL 17'}
    try:
        state = output / 'state'
        state.mkdir()
        url = f'postgresql+psycopg://{private["user"]}:{private["password"]}@127.0.0.1:{private["port"]}/{name}'
        (state / 'database.json').write_text(json.dumps({'url': url}))
        store = TaskStore(state)
        source = state / 'imports' / 'input.txt'
        source.parent.mkdir()
        with source.open('w', encoding='utf-8') as file:
            for start in range(0, rows, 10000):
                file.write(''.join(f'{index:011d}\n' for index in range(start, min(start + 10000, rows))))
        identity = uuid.uuid4().hex
        task = create_import(store, identity, source, 'input.txt', {'config': {'proxy_mode': 'direct'}, 'base': str(output)})
        payload = store.get(task['id'], private=True)['payload']
        begin = time.perf_counter()
        last = 0

        def progress(value):
            nonlocal last
            if value['processed'] - last >= 100000:
                print('Imported:', value['processed'], flush=True)
                last = value['processed']
        result = run_import(store, payload, progress)
        report['import_seconds'] = round(time.perf_counter() - begin, 3)
        report['import_rows_per_second'] = round(rows / report['import_seconds'])
        assert result['added'] == rows and store.counts(actions=['download'])['total'] == rows
        catalog = MediaCatalog(store)
        root = output / 'videos'
        begin = time.perf_counter()
        for start in range(0, rows, 500):
            items = []
            for index in range(start, min(start + 500, rows)):
                identity = hashlib.sha256(str(index).encode()).hexdigest()
                items.append(dict(id=identity, root=str(root), name=f'{index:011d}/video.mp4', logical_key=str(index),
                                  title=f'Benchmark {index:011d}', search_title=f'benchmark {index:011d}',
                                  video_id=f'{index:011d}', size=100000000, modified=float(index), cover=None,
                                  playable=1, prepared=0, status='ready', scan_id=None, indexed_at=time.time()))
            catalog.upsert(items)
        report['media_index_seconds'] = round(time.perf_counter() - begin, 3)
        config = output / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        app = create_app(config, state, start_worker=False)
        client = app.test_client()
        report['requests'] = {}
        for endpoint in ('/api/tasks?scope=download&limit=50', '/api/media?limit=24', '/api/overview'):
            timings = []
            for _ in range(20):
                begin = time.perf_counter()
                response = client.get(endpoint)
                assert response.status_code == 200, response.text[:200]
                timings.append((time.perf_counter() - begin) * 1000)
            report['requests'][endpoint] = {'p50_ms': round(statistics.median(timings), 2),
                                            'p95_ms': round(sorted(timings)[18], 2), 'response_bytes': len(response.data)}
        assert client.get('/api/media').json['total'] == rows
        with store.connect() as db:
            report['database_bytes'] = db.exec_driver_sql('SELECT pg_database_size(current_database())').scalar_one()
            report['task_page_plan'] = '\n'.join(row[0] for row in db.exec_driver_sql(
                'EXPLAIN SELECT id FROM tasks ORDER BY seq DESC LIMIT 50'))
            report['media_page_plan'] = '\n'.join(row[0] for row in db.exec_driver_sql(
                'EXPLAIN SELECT id FROM media_catalog WHERE root=%s ORDER BY modified DESC, id DESC LIMIT 24', (str(root),)))
        report['peak_python_rss_mb'] = round(max(memory or [0]) / 1024 ** 2, 1)
        (output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        print('SCALE_BENCHMARK:', output / 'report.json', flush=True)
    finally:
        stop.set()
        thread.join()
        admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, default=100000)
    args = parser.parse_args()
    if not 1 <= args.rows <= 10000000:
        parser.error('rows must be between 1 and 10000000')
    benchmark(args.rows)
