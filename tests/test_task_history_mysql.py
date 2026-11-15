"""Online history index and bounded reads against a disposable MySQL database."""
import unittest

from sqlalchemy import event, select

from tests.test_mysql import StandaloneMySQLFixture
from webapp.database import tasks


class MySQLHistoryIndexTests(StandaloneMySQLFixture, unittest.TestCase):

    def test_history_branches_use_ordered_index_before_loading_page_details(self):
        engine = self.store.db.engine
        with engine.begin() as db:
            db.execute(tasks.insert(), [dict(id=f'{index:032x}', seq=index + 1,
                action='download', status=('succeeded', 'failed', 'interrupted')[index % 3],
                created_at='2026-11-15T10:00:00+08:00',
                finished_at=f'2026-11-15T10:{index // 60 % 60:02d}:{index % 60:02d}+08:00',
                payload='{}', progress='{"stage":"completed"}') for index in range(12000)])
            db.exec_driver_sql('ANALYZE TABLE tasks')
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                statements.append((statement, parameters))

        event.listen(engine, 'before_cursor_execute', capture)
        try:
            page = self.store.page(limit=50, finished_only=True, actions=['download'], compact=True)
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        self.assertEqual(len(statements), 2)
        candidate_sql, parameters = statements[0]
        self.assertEqual(candidate_sql.count('FORCE INDEX (ix_tasks_history_order)'), 3)
        with engine.connect() as db:
            plan = db.exec_driver_sql('EXPLAIN ' + candidate_sql, parameters).mappings().all()
        branches = [row for row in plan if row['table'] == 'tasks']
        self.assertEqual(len(branches), 3)
        for row in branches:
            self.assertEqual(row['key'], 'ix_tasks_history_order', dict(row))
            self.assertNotIn('filesort', row['Extra'] or '', dict(row))
        second = self.store.page(limit=50, finished_only=True, actions=['download'],
                                 compact=True, cursor=page['next_cursor'])
