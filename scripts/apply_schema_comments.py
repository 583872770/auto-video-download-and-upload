"""Apply the declared table and column comments to an existing MySQL schema.

This is deliberately a separate one-off operation.  Normal schema validation
does not treat comments as a compatibility boundary, and applying comments to
an existing database should be observable rather than hidden in worker startup.
"""
import argparse
import os
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from webapp.database import metadata


def database_url():
    raw = os.environ.get('VIDEO_DATABASE_URL')
    if raw:
        return raw.replace('mysql://', 'mysql+pymysql://', 1)
    return URL.create('mysql+pymysql', username=os.environ['MYSQL_USER'],
                      password=os.environ['MYSQL_PASSWORD'], host=os.environ.get('MYSQL_HOST', 'mysql'),
                      port=int(os.environ.get('MYSQL_PORT', '3306')),
                      database=os.environ.get('MYSQL_DATABASE', 'youtube'),
                      query={'charset': 'utf8mb4'}).render_as_string(hide_password=False)


def ident(value):
    if not re.fullmatch(r'[A-Za-z0-9_]+', value):
        raise ValueError('invalid MySQL identifier')
    return '`' + value + '`'


def literal(value):
    if value is None:
        return 'NULL'
    return "'" + str(value).replace('\\', '\\\\').replace("'", "''") + "'"


def default_sql(value, column_type=''):
    if value is None:
        return None
    value = str(value)
    if value.upper() in {'CURRENT_TIMESTAMP', 'CURRENT_TIMESTAMP()', 'CURRENT_DATE', 'CURRENT_TIME'}:
        return value
    if value.upper().startswith(('CURRENT_TIMESTAMP(', 'CURRENT_DATE(', 'CURRENT_TIME(')):
        return value
    rendered = literal(value)
    # MySQL 8 permits defaults on TEXT/JSON/BLOB columns only as an
    # expression. SHOW FULL COLUMNS reports the expression's value without
    # its parentheses, so restore the required expression form here.
    if re.search(r'\b(?:tiny|medium|long)?text\b|\b(?:tiny|medium|long)?blob\b|\bjson\b|\bgeometry\b',
                 str(column_type), re.I):
        return '(' + rendered + ')'
    return rendered


def column_definition(info, comment):
    parts = [ident(info['Field']), info['Type']]
    if info.get('Collation'):
        charset = info['Collation'].split('_', 1)[0]
        parts.extend(['CHARACTER SET', ident(charset), 'COLLATE', ident(info['Collation'])])
    parts.append('NOT NULL' if info['Null'] == 'NO' else 'NULL')
    default = default_sql(info.get('Default'), info.get('Type', ''))
    if default is not None:
        parts.extend(['DEFAULT', default])
    extra = str(info.get('Extra') or '')
    if 'auto_increment' in extra.lower():
        parts.append('AUTO_INCREMENT')
    update = re.search(r'on update [^ ]+(?:\([^)]*\))?', extra, re.I)
    if update:
        parts.append(update.group(0))
    parts.extend(['COMMENT', literal(comment)])
    return ' '.join(parts)


def apply(engine, dry_run=False):
    changed_tables = changed_columns = 0
    with engine.begin() as db:
        schema = db.execute(text('SELECT DATABASE()')).scalar_one()
        for table in metadata.tables.values():
            table_name = table.name
            current_table_comment = db.execute(text(
                'SELECT TABLE_COMMENT FROM information_schema.TABLES '
                'WHERE TABLE_SCHEMA=:schema AND TABLE_NAME=:table'),
                {'schema': schema, 'table': table_name}).scalar_one_or_none()
            table_sql = None
            if current_table_comment != table.comment:
                table_sql = f'ALTER TABLE {ident(table_name)} COMMENT={literal(table.comment)}'
                changed_tables += 1

            rows = {row['Field']: row for row in db.exec_driver_sql(
                f'SHOW FULL COLUMNS FROM {ident(table_name)}').mappings()}
            clauses = []
            for column in table.c:
                info = rows[column.name]
                if info.get('Comment', '') != (column.comment or ''):
                    clauses.append(column_definition(info, column.comment or ''))
            if clauses:
                changed_columns += len(clauses)
                column_sql = f'ALTER TABLE {ident(table_name)} ' + ', '.join(
                    'MODIFY COLUMN ' + clause for clause in clauses)
                if dry_run:
                    print(column_sql)
                else:
                    db.exec_driver_sql(column_sql)
            if table_sql:
                if dry_run:
                    print(table_sql)
                else:
                    db.exec_driver_sql(table_sql)
    return changed_tables, changed_columns


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    url = database_url()
    if not url.startswith('mysql+pymysql://'):
        raise SystemExit('此脚本仅用于 MySQL 数据库。')
    engine = create_engine(url, hide_parameters=True, pool_pre_ping=True,
                           connect_args={'charset': 'utf8mb4', 'connect_timeout': 10,
                                         'read_timeout': 1800, 'write_timeout': 1800})
    tables, columns = apply(engine, dry_run=args.dry_run)
    print(f'数据库注释处理完成：表注释 {tables} 个，字段注释 {columns} 个。')


if __name__ == '__main__':
    main()
