"""Initialize persistent config and pass a correctly escaped MySQL URL to workers."""
import os
from pathlib import Path
import shutil
import sys

from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]


def database_url(environ):
    required = ('MYSQL_DATABASE', 'MYSQL_USER', 'MYSQL_PASSWORD')
    if any(not environ.get(key) for key in required):
        raise ValueError('MYSQL_DATABASE, MYSQL_USER and MYSQL_PASSWORD are required')
    if environ['MYSQL_USER'].lower() == 'root':
        raise ValueError('Use a dedicated application account instead of root')
    return URL.create(
        'mysql+pymysql', username=environ['MYSQL_USER'],
        password=environ['MYSQL_PASSWORD'],
        host=environ.get('MYSQL_HOST', 'mysql'),
        port=int(environ.get('MYSQL_PORT', '3306')),
        database=environ['MYSQL_DATABASE'], query={'charset': 'utf8mb4'},
    ).render_as_string(hide_password=False)


def initialize_config(directory, template=ROOT / 'docker/config.yaml'):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'Cookies').mkdir(exist_ok=True)
    target = directory / 'config.yaml'
    # Preserve configuration and cookie uploads across every container restart.
    if not target.exists():
        temporary = directory / '.docker-config.tmp'
        shutil.copyfile(template, temporary)
        temporary.replace(target)
    return target


def main():
    os.umask(0o077)
    os.environ['VIDEO_DATABASE_URL'] = database_url(os.environ)
    initialize_config('/data/config')
    command = sys.argv[1:]
    if not command:
        raise ValueError('An application command is required')
    os.chdir(ROOT)
    os.execvpe(command[0], command, os.environ)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError):
        # Never include environment values or connection strings in startup logs.
        raise SystemExit('Container startup failed; check database variables and volume permissions.') from None
