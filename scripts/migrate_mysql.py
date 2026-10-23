"""Copy a stopped SQL database to empty MySQL, with transactional checkpoints.

Both connection files are private JSON objects with a `url` field. No source
data is changed, and activation is opt-in after every table count is verified.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func, select, tuple_
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from webapp.database import Database, metadata, settings, tasks

ROOT = Path(__file__).resolve().parents[1]
MARKER = '_mysql_migration_v1'


def normalized_url(value):
    parsed = make_url(value)
    driver = {'mysql': 'mysql+pymysql', 'postgresql': 'postgresql+psycopg'}.get(parsed.drivername, parsed.drivername)
    if driver not in ('mysql+pymysql', 'postgresql+psycopg', 'sqlite'):
        raise ValueError('迁移源必须是 MySQL、PostgreSQL 或当前格式的 SQLite。')
    return parsed.set(drivername=driver)


def row_filter(table):
    return settings.c.key != MARKER if table is settings else True


def counts(connection):
    return {table.name: connection.execute(select(func.count()).select_from(table).where(row_filter(table))).scalar_one()
            for table in metadata.sorted_tables}


def migrate(source_url, target_url, state, *, chunk_size=500, activate=False):
    if not 1 <= chunk_size <= 1000:
        raise ValueError('每批迁移条数必须为 1 到 1000。')
    source_url, target_url = normalized_url(source_url), normalized_url(target_url)
    if target_url.drivername != 'mysql+pymysql':
        raise ValueError('迁移目标必须是 MySQL。')
    source_location = (source_url.get_backend_name(), source_url.host, source_url.port or 3306, source_url.database)
    target_location = (target_url.get_backend_name(), target_url.host, target_url.port or 3306, target_url.database)
    if source_location == target_location:
        raise ValueError('迁移源和目标不能是同一数据库。')
    fingerprint = hashlib.sha256(source_url.set(password='').render_as_string().encode()).hexdigest()
    state = Path(state).resolve()
    source_options = {} if source_url.drivername == 'sqlite' else {'isolation_level': 'REPEATABLE READ'}
    source = create_engine(source_url, poolclass=NullPool, hide_parameters=True, **source_options)
    target = Database(state, target_url.render_as_string(hide_password=False))
    if not target.mysql:
        raise ValueError('迁移目标必须是 MySQL。')
    try:
        with source.connect() as reader, reader.begin():
            if source_url.drivername == 'sqlite':
                # Python's legacy SQLite transaction mode otherwise starts a
                # transaction only on writes, giving inconsistent table reads.
                reader.exec_driver_sql('BEGIN')
            paused = reader.execute(select(settings.c.value).where(settings.c.key == 'downloads_paused')).scalar_one_or_none()
            running = reader.execute(select(func.count()).select_from(tasks).where(tasks.c.status == 'running')).scalar_one()
            if paused != 'true' or running:
                raise ValueError('请先在页面停止全部下载，停止 Web 和后台进程后再迁移。')
            expected = counts(reader)
            with target.engine.begin() as writer:
                saved = writer.execute(select(settings.c.value).where(settings.c.key == MARKER)).scalar_one_or_none()
                progress = json.loads(saved) if saved else {'source': fingerprint, 'tables': {}, 'complete': False}
                if progress['source'] != fingerprint:
                    raise ValueError('目标库包含其他来源的迁移记录，请使用空数据库。')
                if not saved:
                    existing = counts(writer)
                    if any(n for name, n in existing.items() if name != settings.name) or existing[settings.name] != 1:
                        raise ValueError('目标数据库非空，请新建空 MySQL 数据库后重试。')
                    writer.execute(target.upsert(settings, dict(key=MARKER, value=json.dumps(progress)), ['key']))
                    writer.execute(settings.update().where(settings.c.key == 'downloads_paused').values(value='true'))

            for table in metadata.sorted_tables:
                checkpoint = progress['tables'].get(table.name, {})
                if checkpoint.get('done'):
                    continue
                primary = list(table.primary_key)
                last = checkpoint.get('last')
                copied = checkpoint.get('count', 0)
                while True:
                    statement = select(table).where(row_filter(table)).order_by(*primary).limit(chunk_size)
                    if last:
                        statement = statement.where(primary[0] > last[0] if len(primary) == 1 else tuple_(*primary) > tuple_(*last))
                    rows = [dict(row) for row in reader.execute(statement).mappings()]
                    if table.name in ('media_roots', 'media_catalog') and any(len(row['root']) > 512 for row in rows):
                        raise ValueError('源素材根目录超过 MySQL 的 512 字符限制，请先缩短目录并同步索引。')
                    if rows:
                        last = [rows[-1][column.name] for column in primary]
                        copied += len(rows)
                    progress['tables'][table.name] = dict(last=last, count=copied, done=len(rows) < chunk_size)
                    with target.engine.begin() as writer:
                        if rows:
                            # The target is exclusively owned by this migration.
                            # Replace only the initialized pause setting; chunk
                            # checkpoints and inserts commit in one transaction.
                            if table is settings:
                                writer.execute(target.upsert(table, rows, ['key']))
                            else:
                                writer.execute(target.ignore(table, rows))
                        writer.execute(target.upsert(settings, dict(key=MARKER, value=json.dumps(progress)), ['key']))
                    if len(rows) < chunk_size:
                        break
                print(f'{table.name}: {copied} rows verified for copying', flush=True)

            with target.engine.begin() as writer:
                if counts(writer) != expected:
                    raise ValueError('迁移数量核对失败，尚未切换连接；请保持目标服务停止并检查数据。')
                progress.update(complete=True, counts=expected)
                writer.execute(target.upsert(settings, dict(key=MARKER, value=json.dumps(progress)), ['key']))
        # Re-open outside the source snapshot to detect new inserts/deletes.
        with source.connect() as current:
            if counts(current) != expected:
                raise ValueError('迁移期间源数据发生变化，尚未切换连接。')
    finally:
        source.dispose()

    if activate:
        connection = state / 'database.json'
        if connection.is_file():
            backup = ROOT / '.cache/backups' / datetime.now().strftime('mysql-%Y%m%d-%H%M%S-%f')
            backup.mkdir(parents=True)
            shutil.copy2(connection, backup / 'database.json')
        temporary = state / '.mysql-connection.tmp'
        temporary.write_text(json.dumps({'url': target.url}), encoding='utf-8')
        temporary.replace(connection)
    print('MySQL migration verified; source database retained; download queue remains paused.', flush=True)
    return {'counts': expected, 'activated': activate}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-connection-file', type=Path, required=True)
    parser.add_argument('--target-connection-file', type=Path, required=True)
    parser.add_argument('--state', type=Path, default=ROOT / '.cache/web')
    parser.add_argument('--chunk-size', type=int, default=500)
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    try:
        source_url = json.loads(args.source_connection_file.read_text(encoding='utf-8'))['url']
        target_url = json.loads(args.target_connection_file.read_text(encoding='utf-8'))['url']
        migrate(source_url, target_url, args.state, chunk_size=args.chunk_size, activate=args.activate)
    except ValueError as error:
        # Driver/connection exceptions can contain private URLs; only expose
        # the explicit validation messages raised above.
        if str(error).startswith(('迁移', '目标', '请先', '源素材', '每批')):
            raise SystemExit(str(error)) from None
        raise SystemExit('迁移失败，请检查连接文件和数据库；未自动切换连接。') from None
    except Exception:
        raise SystemExit('迁移失败，请检查连接、表结构和磁盘空间；可在原目标库断点重试。') from None
