"""Exercise the real ID candidate query on disposable million-row SQLite data.

This verifies bounded index reads, not production MySQL latency. Run MySQL plans
with VIDEO_TEST_MYSQL_URL and tests.test_job_videos_mysql separately.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

from sqlalchemy.dialects.sqlite import dialect

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from webapp.job_videos import candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=1_000_000)
    args = parser.parse_args()
    if not 1 <= args.rows <= 10_000_000:
        parser.error('--rows must be between 1 and 10000000')
    with sqlite3.connect(':memory:') as db:
        db.executescript('''
            CREATE TABLE tasks(id TEXT PRIMARY KEY, seq INTEGER UNIQUE,
                batch_id TEXT, action TEXT, status TEXT, video_id TEXT, title TEXT);
        ''')
        for start in range(0, args.rows, 10000):
            db.executemany('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',
                ((f'{i:032x}', i + 1, 'job', 'download', 'succeeded', f'V{i:010d}', 'Example title')
                 for i in range(start, min(start + 10000, args.rows))))
        db.executescript('''
            CREATE INDEX ix_tasks_batch_seq ON tasks(batch_id,seq);
            CREATE INDEX ix_tasks_video ON tasks(video_id);
            CREATE INDEX ix_tasks_job_video ON tasks(batch_id,action,video_id,seq);
        ''')
        for query in (f'V{args.rows - 1:010d}', 'Missing_ID0'):
            sql = str(candidates('job', query=query).compile(dialect=dialect(),
                compile_kwargs={'literal_binds': True}))
            plan = [row[-1] for row in db.execute('EXPLAIN QUERY PLAN ' + sql)]
            assert any('ix_tasks_job_video' in row and 'video_id=?' in row for row in plan), plan
            operations = 0
            def tick():
                nonlocal operations
                operations += 1
                # Abort instead of silently accepting a million-row scan.
                return int(operations > 1000)
            db.set_progress_handler(tick, 1)
            started = time.perf_counter()
            found = db.execute(sql).fetchall()
            elapsed = (time.perf_counter() - started) * 1000
            db.set_progress_handler(None, 0)
            assert len(found) == (0 if query == 'Missing_ID0' else 1)
            print(json.dumps(dict(engine='isolated SQLite, not MySQL', rows=args.rows,
                query=query, matches=len(found), milliseconds=round(elapsed, 3),
                vm_operations=operations, plan=plan)))


if __name__ == '__main__':
    main()
