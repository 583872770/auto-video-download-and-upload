"""Exact job lookups, bounded page hydration and isolated title search."""
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.dialects.mysql import dialect

from tests import test_scale
from webapp.database import delivery_runs, job_profiles, tasks
from webapp.job_videos import candidates, search_value


class SearchInputTests(unittest.TestCase):
    def test_ids_and_links_preserve_case(self):
        for value in ('npNnbj46_ik', '  npNnbj46_ik  ',
                      'https://youtu.be/npNnbj46_ik?t=1',
                      'https://www.youtube.com/watch?v=npNnbj46_ik&list=other',
                      'https://m.youtube.com/shorts/npNnbj46_ik',
                      'https://www.youtube.com/live/npNnbj46_ik',
                      'https://www.youtube.com/embed/npNnbj46_ik'):
            with self.subTest(value=value):
                self.assertEqual(search_value(value, 'video_id'), 'npNnbj46_ik')

    def test_invalid_input_does_not_fall_back_to_title_search(self):
        for value in ('partial', 'https://example.com/watch?v=npNnbj46_ik',
                      'https://youtube.com.evil.test/watch?v=npNnbj46_ik',
                      'ftp://youtube.com/watch?v=npNnbj46_ik', 'https://[broken'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                search_value(value, 'video_id')
        for query, mode in (('a', 'title'), ('x' * 201, 'title'), ('', 'unknown')):
            with self.assertRaises(ValueError):
                search_value(query, mode)

    def test_mysql_id_and_fulltext_queries_are_separate(self):
        exact = str(candidates('job', query='npNnbj46_ik', mysql=True).compile(dialect=dialect()))
        self.assertIn('FORCE INDEX (ix_tasks_job_video)', exact)
        self.assertIn('tasks.video_id =', exact)
        self.assertNotIn('LIKE', exact)
        self.assertNotIn('tasks.title', exact)
        title = str(candidates('job', query='模型', search='title', mysql=True).compile(dialect=dialect()))
        self.assertIn('MATCH (tasks.title) AGAINST', title)
        self.assertNotIn('LIKE', title)


class JobVideoTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def create_job(self, urls='', name='Search fixture'):
        response = self.client.post('/api/jobs', json={
            'customer': 'Client', 'name': name, 'urls': urls}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['id']

    def search(self, job, **params):
        return self.client.get(f'/api/jobs/{job}/videos', query_string=params)

    def test_exact_id_ignores_titles_other_jobs_and_wildcards(self):
        job = self.create_job('npNnbj46_ik\nnpNnbj46Xik\nnpnnbj46_ik')
        self.create_job('npNnbj46_ik', name='Other customer task')
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.batch_id == job,
                tasks.c.video_id != 'npNnbj46_ik').values(title='npNnbj46_ik'))
        for query in ('npNnbj46_ik', 'https://youtu.be/npNnbj46_ik?t=5'):
            response = self.search(job, q=query)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual([r['video_id'] for r in response.json['items']], ['npNnbj46_ik'])
        self.assertEqual(self.search(job, q='Missing_ID0').json['items'], [])
        self.assertEqual(self.search(job, q='npNn').status_code, 400)
        title = self.search(job, q='npNnbj46_ik', search='title')
        self.assertEqual(len(title.json['items']), 2)

    def test_filters_and_removed_rows_apply_to_exact_lookup(self):
        job = self.create_job('npNnbj46_ik')
        task = self.search(job).json['items'][0]
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='succeeded'))
            db.execute(delivery_runs.insert().values(task_id=task['id'], job_id=job,
                version=1, spec='{}', status='review', test_mode=0))
        self.assertEqual(len(self.search(job, q=task['video_id'], status='succeeded',
            delivery_status='review').json['items']), 1)
        self.assertEqual(self.search(job, q=task['video_id'], status='failed').json['items'], [])
        self.assertEqual(self.search(job, q=task['video_id'], delivery_status='complete').json['items'], [])
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='removed'))
        self.assertEqual(self.search(job, q=task['video_id']).json['items'], [])
        self.assertEqual(len(self.search(job, q=task['video_id'], status='removed').json['items']), 1)

    def test_list_reads_only_one_page_without_loading_job_detail_or_payloads(self):
        job = self.create_job('\n'.join(f'{i:011d}' for i in range(75)))
        statements = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('SELECT'):
                statements.append(statement)
        event.listen(self.store.db.engine, 'before_cursor_execute', capture)
        try:
            with patch('webapp.jobs.Jobs.detail', side_effect=AssertionError('must not load job detail')), \
                    patch.object(self.store.progress_cache, 'read', side_effect=AssertionError('must batch cache reads')), \
                    patch.object(self.store.progress_cache, 'read_many', wraps=self.store.progress_cache.read_many) as batch:
                first = self.search(job).json
                batch.assert_called_once()
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', capture)
        self.assertEqual(len(first['items']), 50)
        self.assertFalse(any('tasks.payload' in s for s in statements), statements)
        task_reads = [s for s in statements if '\nFROM tasks' in s]
        self.assertEqual(len(task_reads), 2, statements)
        self.assertTrue(task_reads[0].startswith('SELECT tasks.id, tasks.seq'), task_reads[0])
        self.assertIn('tasks.id IN', task_reads[1])
        second = self.search(job, cursor=first['next_cursor']).json
        self.assertEqual(len(second['items']), 25)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len({r['id'] for r in first['items'] + second['items']}), 75)

    def test_live_progress_and_delivery_requirements_are_preserved(self):
        job = self.create_job('npNnbj46_ik')
        task_id = self.search(job).json['items'][0]['id']
        live = {task_id: {'progress': {'stage': 'downloading', 'speed': 123}, 'updated_at': 1}}
        with patch.object(self.store.progress_cache, 'read_many', return_value=live):
            row = self.search(job, q='npNnbj46_ik').json['items'][0]
        self.assertEqual(row['progress']['speed'], 123)
        self.assertIn('requirements', row['delivery'])

    def test_deleted_and_missing_jobs_are_not_readable_but_archived_jobs_are(self):
        job = self.create_job('npNnbj46_ik')
        with self.store.connect() as db:
            db.execute(job_profiles.update().where(job_profiles.c.id == job).values(archived=1))
        self.assertEqual(self.search(job).status_code, 200)
        with self.store.connect() as db:
            db.execute(job_profiles.update().where(job_profiles.c.id == job).values(deleted=1))
        self.assertEqual(self.search(job).status_code, 404)
        self.assertEqual(self.search('f' * 32).status_code, 404)

    def test_exact_candidate_uses_composite_index_for_hits_and_misses(self):
        job = self.create_job('npNnbj46_ik')
        for query in ('npNnbj46_ik', 'Missing_ID0'):
            sql = str(candidates(job, query=query).compile(
                self.store.db.engine, compile_kwargs={'literal_binds': True}))
            with self.store.connect() as db:
                plan = db.exec_driver_sql('EXPLAIN QUERY PLAN ' + sql).all()
            self.assertTrue(any('ix_tasks_job_video' in row[-1] and 'video_id=?' in row[-1]
                for row in plan), plan)


if __name__ == '__main__':
    unittest.main()
