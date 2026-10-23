"""Install/start an isolated PostgreSQL on Windows; no global service or PATH changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'postgresql-17.6-1-windows-x64-binaries.zip'
URL = 'https://get.enterprisedb.com/postgresql/' + PACKAGE


def ensure_running():
    """Only start an already configured project database, never install at startup."""
    if os.environ.get('VIDEO_DATABASE_URL'):
        return
    config = ROOT / '.cache/web/database.json'
    private = ROOT / '.cache/postgres/database.json'
    if not config.is_file() or not private.is_file() or config.read_bytes() != private.read_bytes():
        return
    binary = ROOT / '.tools/postgresql/pgsql/bin/pg_ctl.exe'
    state = ROOT / '.cache/postgres'
    if not binary.is_file() or not (state / 'data/PG_VERSION').is_file():
        raise RuntimeError('项目 PostgreSQL 不完整，请运行 scripts/setup_database.py 检查。')
    connection = json.loads((state / 'connection.json').read_text(encoding='utf-8'))
    options = {'creationflags': subprocess.CREATE_NO_WINDOW}
    status = subprocess.run([str(binary), '-D', str(state / 'data'), 'status'], capture_output=True, **options)
    if status.returncode:
        subprocess.run([str(binary), '-D', str(state / 'data'), '-l', str(state / 'server.log'),
                        '-o', f'-h 127.0.0.1 -p {connection["port"]}', '-w', 'start'],
                       capture_output=True, check=True, **options)


def setup(port=15432):
    if os.name != 'nt':
        raise SystemExit('非 Windows 环境请安装 PostgreSQL 17，并通过 VIDEO_DATABASE_URL 指定连接。')
    tools = ROOT / '.tools' / 'postgresql'
    binary = tools / 'pgsql' / 'bin'
    state = ROOT / '.cache' / 'postgres'
    state.mkdir(parents=True, exist_ok=True)
    if not (binary / 'pg_ctl.exe').is_file():
        package = ROOT / '.cache' / 'downloads' / PACKAGE
        package.parent.mkdir(parents=True, exist_ok=True)
        if not package.is_file():
            partial = package.with_suffix('.part')
            with urllib.request.urlopen(URL, timeout=60) as source, partial.open('wb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            partial.replace(package)
        tools.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package) as archive:
            for entry in archive.infolist():
                if not entry.filename.startswith(('pgsql/bin/', 'pgsql/lib/', 'pgsql/share/')):
                    continue
                target = (tools / entry.filename).resolve()
                if not target.is_relative_to(tools.resolve()):
                    raise RuntimeError('安装包包含无效路径。')
                archive.extract(entry, tools)
        print('PostgreSQL 17.6 binaries installed.')
    connection_file = state / 'connection.json'
    if connection_file.is_file():
        connection = json.loads(connection_file.read_text(encoding='utf-8'))
    else:
        with socket.socket() as stream:
            stream.bind(('127.0.0.1', port))
        connection = {'port': port, 'user': 'video_app', 'password': secrets.token_urlsafe(32), 'database': 'video_downloads'}
        connection_file.write_text(json.dumps(connection), encoding='utf-8')
    data = state / 'data'
    options = {'creationflags': subprocess.CREATE_NO_WINDOW}
    if not (data / 'PG_VERSION').is_file():
        with tempfile.TemporaryDirectory(dir=state) as temporary:
            password = Path(temporary) / 'password.txt'
            password.write_text(connection['password'], encoding='utf-8')
            subprocess.run([str(binary / 'initdb.exe'), '-D', str(data), '-U', connection['user'],
                            '--pwfile=' + str(password), '--encoding=UTF8', '--locale=C',
                            '--auth-host=scram-sha-256', '--auth-local=scram-sha-256'], check=True, **options)
    status = subprocess.run([str(binary / 'pg_ctl.exe'), '-D', str(data), 'status'], capture_output=True, **options)
    if status.returncode:
        subprocess.run([str(binary / 'pg_ctl.exe'), '-D', str(data), '-l', str(state / 'server.log'),
                        '-o', f'-h 127.0.0.1 -p {connection["port"]}', '-w', 'start'], check=True, **options)
    import psycopg
    from psycopg import sql
    with psycopg.connect(host='127.0.0.1', port=connection['port'], user=connection['user'],
                         password=connection['password'], dbname='postgres', autocommit=True) as db:
        if not db.execute('SELECT 1 FROM pg_database WHERE datname=%s', (connection['database'],)).fetchone():
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(connection['database'])))
    url = f'postgresql+psycopg://{connection["user"]}:{connection["password"]}@127.0.0.1:{connection["port"]}/{connection["database"]}'
    (state / 'database.json').write_text(json.dumps({'url': url}), encoding='utf-8')
    print(f'PostgreSQL ready on 127.0.0.1:{connection["port"]}; credentials stored locally.')
    return url


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=15432)
    setup(parser.parse_args().port)
