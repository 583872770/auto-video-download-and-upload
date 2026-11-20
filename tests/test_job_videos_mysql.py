"""Production index plans and ngram title queries on disposable MySQL databases."""
import unittest

from tests import test_job_videos
from tests.test_mysql import StandaloneMySQLFixture
from webapp.database import tasks
from webapp.job_videos import candidates


class MySQLJobVideoTests(StandaloneMySQLFixture, test_job_videos.JobVideoTests):
    setUp = StandaloneMySQLFixture.setUp

    def test_exact_candidate_uses_composite_index_for_hits_and_misses(self):
        job = self.create_job()
        with self.store.connect() as db:
            db.execute(tasks.insert(), [dict(id=f'{i:032x}', seq=i + 1, batch_id=job,
                action='download', status='queued', video_id=f'V{i:010d}', title='fixture',
                payload='{}', created_at='2026-11-18T10:00:00+08:00') for i in range(10000)])
        with self.store.db.engine.connect() as db:
            db.exec_driver_sql('ANALYZE TABLE tasks')
            for query in ('V0000009999', 'Missing_ID0'):
                sql = str(candidates(job, query=query, mysql=True).compile(
                    self.store.db.engine, compile_kwargs={'literal_binds': True}))
                plan = db.exec_driver_sql('EXPLAIN ' + sql).mappings().one()
                self.assertEqual(plan['key'], 'ix_tasks_job_video', plan)
                self.assertLessEqual(plan['rows'], 2, plan)
                self.assertNotIn('filesort', plan['Extra'] or '', plan)

    def test_ngram_title_search_and_schema_reopen(self):
        job = self.create_job('npNnbj46_ik\n2Gu7q-KUXCk')
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.batch_id == job,
                tasks.c.video_id == 'npNnbj46_ik').values(title='模型审核工作流程'))
            db.execute(tasks.update().where(tasks.c.batch_id == job,
                tasks.c.video_id == '2Gu7q-KUXCk').values(title='其他主题'))
        response = self.search(job, search='title', q='模型')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual([r['video_id'] for r in response.json['items']], ['npNnbj46_ik'])
        self.store.db.initialize()

    def test_global_id_and_sample_view_do_not_scan_task_history(self):
        import time
        from webapp.database import test_samples
        job = self.create_job()
        with self.store.connect() as db:
            db.execute(tasks.insert(), [dict(id=f'{i:032x}', seq=i + 1, batch_id=job,
                action='download', status='queued', video_id=f'V{i:010d}', title='fixture',
                payload='{}', created_at='2026-11-18T10:00:00+08:00') for i in range(10000)])
            db.execute(test_samples.insert().values(id='a'*32, job_id=job, task_id=f'{9999:032x}',
                instance_id='local', video_id='V0000009999', title='fixture', workspace='/unused',
                files='[]', size=0, state='ready', created=time.time(), expires=time.time()+3600,
                view_until=0, review='pending', revision=1))
            db.exec_driver_sql('ANALYZE TABLE tasks, test_samples')
            for options in [dict(query='V0000009999'), dict(samples_only=True)]:
                sql = str(candidates(None, mysql=True, **options).compile(
                    self.store.db.engine, compile_kwargs={'literal_binds': True}))
                plan = db.exec_driver_sql('EXPLAIN ' + sql).mappings().all()
                task_plan = next(r for r in plan if r['table'] == 'tasks')
                self.assertIn(task_plan['type'], ('ref', 'eq_ref', 'const'), plan)
                self.assertLessEqual(task_plan['rows'], 2, plan)

    def test_global_default_and_status_pages_use_ordered_task_index(self):
        job = self.create_job()
        with self.store.connect() as db:
            db.execute(tasks.insert(), [dict(id=f'{i:032x}', seq=i + 1, batch_id=job,
                action='download', status='queued', video_id=f'V{i:010d}', title='fixture',
                payload='{}', created_at='2026-11-18T10:00:00+08:00') for i in range(10000)])
            db.exec_driver_sql('ANALYZE TABLE tasks')
            for status, index in [(None, 'ix_tasks_action_seq'), ('queued', 'ix_tasks_status_seq')]:
                sql = str(candidates(None, mysql=True, status=status).compile(
                    self.store.db.engine, compile_kwargs={'literal_binds': True}))
                plan = db.exec_driver_sql('EXPLAIN ' + sql).mappings().all()
                self.assertEqual(plan[0]['table'], 'tasks', plan)
                self.assertEqual(plan[0]['key'], index, plan)
                self.assertTrue(all('filesort' not in (r['Extra'] or '') for r in plan), plan)


if __name__ == '__main__':
    unittest.main()
