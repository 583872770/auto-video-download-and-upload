"""Load local database settings before Flask and its workers are started."""
import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]


def configure_database_environment(env_file=None):
    path = Path(env_file) if env_file is not None else ROOT / '.env'
    if env_file is not None and not path.is_file():
        raise ValueError('指定的 --env-file 文件不存在。')
    if path.is_file():
        # Preserve passwords containing $ or ${...}; process variables take priority.
        for key, value in dotenv_values(path, encoding='utf-8-sig', interpolate=False).items():
            if value is not None:
                os.environ.setdefault(key, value)
    if os.environ.get('VIDEO_DATABASE_URL'):
        return
    required = ('MYSQL_HOST', 'MYSQL_DATABASE', 'MYSQL_USER', 'MYSQL_PASSWORD')
    if not any(os.environ.get(key) for key in (*required, 'MYSQL_PORT')):
        return
    if any(not os.environ.get(key) for key in required):
        raise ValueError('MySQL 配置不完整，请在 .env 或环境变量中填写 MYSQL_HOST、MYSQL_DATABASE、MYSQL_USER、MYSQL_PASSWORD。')
    try:
        port = int(os.environ.get('MYSQL_PORT', '3306'))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError('MYSQL_PORT 必须是 1 到 65535 之间的整数。') from None
    os.environ['VIDEO_DATABASE_URL'] = URL.create(
        'mysql+pymysql', host=os.environ['MYSQL_HOST'], port=port,
        username=os.environ['MYSQL_USER'], password=os.environ['MYSQL_PASSWORD'],
        database=os.environ['MYSQL_DATABASE'], query={'charset': 'utf8mb4'},
    ).render_as_string(hide_password=False)
