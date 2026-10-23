"""Create PostgreSQL custom-format backups; restore only into an empty database."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url
from Utils.config import load_config, resolve_path

ROOT = Path(__file__).resolve().parents[1]


def connection_options(connection_file):
    url = make_url(json.loads(Path(connection_file).read_text(encoding='utf-8'))['url'])
    if url.get_backend_name() != 'postgresql':
        raise ValueError('备份工具仅用于 PostgreSQL。')
    env = dict(os.environ, PGPASSWORD=url.password or '', PGUSER=url.username or '',
               PGHOST=url.host or '127.0.0.1', PGPORT=str(url.port or 5432), PGDATABASE=url.database or '')
    for key in ('sslmode', 'sslrootcert', 'sslcert', 'sslkey'):
        if key in url.query:
            env['PG' + key.upper()] = url.query[key]
    return env


def binary(name):
    local = ROOT / '.tools/postgresql/pgsql/bin' / (name + ('.exe' if os.name == 'nt' else ''))
    command = str(local) if local.is_file() else shutil.which(name)
    if not command:
        raise ValueError('未找到 PostgreSQL 工具：' + name)
    return command


def backup(connection_file, output=None, config_path=None, state=None):
    output = Path(output or ROOT / '.cache/backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')).resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = connection_options(connection_file)
    partial = output / 'database.dump.partial'
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    subprocess.run([binary('pg_dump'), '--format=custom', '--file', str(partial)],
                   env=env, check=True, capture_output=True, **options)
    partial.replace(output / 'database.dump')
    shutil.copy2(connection_file, output / 'database.json')
    config_path = Path(config_path or ROOT / 'config.yaml')
    state = Path(state or ROOT / '.cache/web')
    if config_path.is_file():
        shutil.copy2(config_path, output / 'config.yaml')
        config, base = load_config(config_path)
        if config.get('youtube_cookie'):
            cookie = resolve_path(base, config['youtube_cookie'])
            if cookie.is_file():
                shutil.copy2(cookie, output / 'youtube-cookie.txt')
    for name in ('settings.json', 'nodes.yaml'):
        source = state / 'clash' / name
        if source.is_file():
            (output / 'clash').mkdir(exist_ok=True)
            shutil.copy2(source, output / 'clash' / name)
    (output / 'manifest.json').write_text(json.dumps({'created_at': datetime.now().astimezone().isoformat(),
        'format': 'PostgreSQL custom', 'includes_media_files': False, 'includes_task_logs': False}, indent=2), encoding='utf-8')
    print('Backup ready:', output)
    return output


def restore(dump, connection_file):
    import psycopg
    env = connection_options(connection_file)
    options = {key: env['PG' + key.upper()] for key in ('host', 'port', 'user', 'password', 'database')}
    options['dbname'] = options.pop('database')
    for key in ('sslmode', 'sslrootcert', 'sslcert', 'sslkey'):
        if 'PG' + key.upper() in env:
            options[key] = env['PG' + key.upper()]
    with psycopg.connect(**options) as db:
        exists = db.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                            "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S') LIMIT 1").fetchone()
        if exists:
            raise ValueError('目标数据库非空。请新建空数据库后恢复，原数据库不会被覆盖。')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    subprocess.run([binary('pg_restore'), '--exit-on-error', '--single-transaction', '--no-owner', '--no-privileges',
                    '--dbname', env['PGDATABASE'], str(Path(dump).resolve())],
                   env=env, check=True, capture_output=True, **options)
    print('Database restored. Update the application connection file only after verification.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connection-file', type=Path, default=ROOT / '.cache/web/database.json')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--config', type=Path, default=ROOT / 'config.yaml')
    parser.add_argument('--state', type=Path, default=ROOT / '.cache/web')
    parser.add_argument('--restore', type=Path, help='Restore dump into the empty database in --connection-file')
    args = parser.parse_args()
    try:
        if args.restore:
            restore(args.restore, args.connection_file)
        else:
            backup(args.connection_file, args.output, args.config, args.state)
    except (ValueError, subprocess.CalledProcessError):
        # PostgreSQL errors may contain server details; do not print credentials or env.
        raise SystemExit('操作失败；请检查目标数据库、连接配置、权限和磁盘空间。未自动切换应用连接。')
