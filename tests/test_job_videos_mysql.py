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


if __name__ == '__main__':
    unittest.main()
