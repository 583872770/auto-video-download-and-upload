"""Check actual filtered SQL plans and searches against one million synthetic rows."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg import sql
from sqlalchemy import event
from app import create_app
from webapp.store import TaskStore

ROOT = Path(__file__).resolve().parents[1]


def benchmark(rows):
    output = ROOT / '.cache/validation' / ('query-scale-' + uuid.uuid4().hex[:10])
    state = output / 'state'
    state.mkdir(parents=True)
    c = json.loads((ROOT / '.cache/postgres/connection.json').read_text(encoding='utf-8'))
    name = 'query_video_' + uuid.uuid4().hex[:12]
    admin = psycopg.connect(host='127.0.0.1', port=c['port'], user=c['user'], password=c['password'],
                           dbname='postgres', autocommit=True)
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        url = f'postgresql+psycopg://{c["user"]}:{c["password"]}@127.0.0.1:{c["port"]}/{name}'
        (state / 'database.json').write_text(json.dumps({'url': url}), encoding='utf-8')
        store = TaskStore(state)
        root = str((output / 'videos').resolve())
        with store.connect() as db:
            db.exec_driver_sql("INSERT INTO tasks (id,seq,action,status,created_at,payload,video_id,title,url) "
                "SELECT md5(i::text),i+1,'download','queued','2026-10-23T00:00:00+08:00','{}',"
                "lpad(i::text,11,'0'),'benchmark '||lpad(i::text,11,'0'),"
                "'https://www.youtube.com/watch?v='||lpad(i::text,11,'0') FROM generate_series(0,%s) i", (rows - 1,))
            db.exec_driver_sql("INSERT INTO task_counters VALUES ('action:download','queued',%s)", (rows,))
            db.exec_driver_sql("INSERT INTO media_catalog (id,root,name,logical_key,title,search_title,video_id,size,"
                "modified,playable,prepared,status,indexed_at) SELECT md5(i::text)||md5(i::text),%s,"
                "lpad(i::text,11,'0')||'/video.mp4',i::text,'Benchmark '||lpad(i::text,11,'0'),"
                "'benchmark '||lpad(i::text,11,'0'),lpad(i::text,11,'0'),100000000,i,1,0,'ready',1 "
                "FROM generate_series(0,%s) i", (root, rows - 1))
            db.exec_driver_sql('INSERT INTO media_roots (root,count,bytes) VALUES (%s,%s,%s)', (root, rows, rows * 100000000))
            db.exec_driver_sql('ANALYZE tasks')
            db.exec_driver_sql('ANALYZE media_catalog')
        config = output / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        client = create_app(config, state, start_worker=False).test_client()
        captured = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('SELECT') and ('FROM media_catalog' in statement or 'FROM tasks' in statement):
                captured.append((statement, parameters))
        event.listen(store.db.engine, 'before_cursor_execute', capture)
        report = {'rows_per_table': rows, 'metadata_only': True, 'actual_videos_downloaded': 0,
                  'source': 'synthetic SQL rows; import performance is measured separately', 'requests': {}}
        for endpoint in ('/api/tasks?scope=download&limit=50', '/api/media?limit=24',
                         '/api/tasks?q=00000000123', '/api/media?q=00000000123', '/api/media?q=Benchmark%2000000000123',
                         '/api/overview'):
            timings = []
            captured.clear()
            for _ in range(20):
                begin = time.perf_counter()
                response = client.get(endpoint)
                assert response.status_code == 200, endpoint
                timings.append((time.perf_counter() - begin) * 1000)
            if '?q=' in endpoint:
                assert len(response.json['items']) == 1, endpoint
            statement, parameters = captured[0]
            with store.connect() as db:
                plan = '\n'.join(row[0] for row in db.exec_driver_sql('EXPLAIN ' + statement, parameters))
            report['requests'][endpoint] = {'p50_ms': round(statistics.median(timings), 2),
                                           'p95_ms': round(sorted(timings)[18], 2), 'plan': plan}
        media_plan = report['requests']['/api/media?limit=24']['plan']
        assert 'ix_media_root_recent' in media_plan and 'Seq Scan' not in media_plan, media_plan
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('FILTERED_QUERY_BENCHMARK:', output / 'report.json')
    finally:
        admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, default=1000000)
    args = parser.parse_args()
    if not 124 <= args.rows <= 10000000:
        parser.error('rows must be between 124 and 10000000')
    benchmark(args.rows)
