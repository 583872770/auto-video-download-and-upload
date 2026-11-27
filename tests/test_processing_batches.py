"""Real caption batches, atomic receipts, dependency lifetime and queue fairness."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests import test_delivery, test_scale, test_statistics
from webapp.delivery.batches import Dependencies, select_work
from webapp.delivery.verify import convert, convert_subtitles
from webapp.database import deliveries, delivery_work


class CaptionBatchTests(unittest.TestCase):
    def test_real_batch_matches_individual_conversions_with_one_process(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('FFmpeg required')
        with tempfile.TemporaryDirectory() as tmp:
            pairs = []
            for index in range(8):
                source = Path(tmp) / f'{index}.vtt'
                source.write_text(f'WEBVTT\n\n00:00:00.000 --> 00:00:00.400\nCaption {index}.\n')
                pairs.append((source, source.with_suffix('.srt')))
            with patch('webapp.delivery.verify.subprocess.run', wraps=subprocess.run) as process:
                self.assertEqual(convert_subtitles(pairs), {})
            self.assertEqual(process.call_count, 1)
            for source, target in pairs:
                expected = source.with_suffix('.single.srt')
                convert([source], expected, 'subtitle')
                self.assertEqual(target.read_bytes(), expected.read_bytes())

    def test_bad_caption_does_not_discard_good_neighbour(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('FFmpeg required')
        with tempfile.TemporaryDirectory() as tmp:
            good, bad = Path(tmp) / 'good.vtt', Path(tmp) / 'bad.vtt'
            good.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:00.400\nGood.\n')
            bad.write_text('not subtitles')
            output = good.with_suffix('.srt')
            failures = convert_subtitles([(good, output), (bad, bad.with_suffix('.srt'))])
            self.assertEqual(set(failures), {str(bad.with_suffix('.srt'))})
            self.assertIn('Good.', output.read_text())

    def test_shared_source_survives_until_last_consumer_and_retained_files_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'shared'; source.write_bytes(b'source')
            jobs = [dict(inputs=[str(source)]) for _ in range(2)]
            dependencies = Dependencies(jobs, tmp)
            dependencies.retire(jobs[:1], {})
            self.assertTrue(source.exists())
            dependencies.retire(jobs[1:], {})
            self.assertFalse(source.exists())
            source.write_bytes(b'published')
            dependencies = Dependencies(jobs[:1], tmp)
            dependencies.retire(jobs[:1], {str(source): 9})
            self.assertTrue(source.exists())

    def test_release_priority_has_a_hard_aging_limit(self):
        def row(seq, created, writes):
            return dict(seq=seq, _work_created=created, _work_plan=json.dumps(dict(
                jobs=[dict(kind='av', output_estimate=writes)],
                progress=dict(space_plan=dict(source_bytes=100 * 1024**2)))))
        expensive, cheap = row(1, 100, 500 * 1024**2), row(2, 110, 0)
        self.assertIs(select_work([expensive, cheap], 200), cheap)
        self.assertIs(select_work([expensive, row(3, 270, 0)], 270), expensive)
        self.assertIs(select_work([expensive, cheap], 280), expensive)


class AtomicBatchTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp
    job = test_statistics.StatisticsContract.job
    final_file = test_statistics.StatisticsContract.final_file

    def test_batch_rolls_back_all_counters_and_retry_is_idempotent(self):
        self.job()
        task = self.store.claim(self.store.owner)
        items = [dict(path=self.final_file(f'{i}.mp4', 100 + i)) for i in range(3)]
        original = self.store.statistics._record_file
        def fail(task_id, path, *args, **kwargs):
            if path.name == '1.mp4':
                raise RuntimeError('interrupted batch')
            return original(task_id, path, *args, **kwargs)
        with patch.object(self.store.statistics, '_record_file', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'interrupted batch'):
                self.store.statistics.record_files(task['id'], items)
        with self.store.connect() as db:
            self.assertEqual(len(db.execute(select(deliveries)).all()), 0)
        self.assertEqual(self.store.statistics.record_files(task['id'], items), [True] * 3)
        self.assertEqual(self.store.statistics.record_files(task['id'], items), [False] * 3)
        session = self.store.statistics.session(task['payload']['download_session_id'])
        self.assertEqual((session['file_count'], session['file_bytes']), (3, 303))


class ProcessingBatchTests(unittest.TestCase):
    setUp = test_delivery.DeliveryTests.setUp
    info = test_delivery.DeliveryTests.info
    create = test_delivery.DeliveryTests.create
    execute = test_delivery.DeliveryTests.execute
    run_task = test_delivery.DeliveryTests.run_task
    cleanup_result = test_delivery.DeliveryTests.cleanup_result

    def fixture(self):
        self.storage = {'storage_mode': 'test'}
        self.job_id = self.create(['video_only', 'audio_only', 'subtitle'],
                    {'subtitles': {'languages': ['en', 'fr', 'de'], 'format': 'srt'}})
        self.sample_policy(False)
        info = self.info()
        for language in ('fr', 'de'):
            info['subtitles'][language] = [{'url': self.url + '/en.vtt', 'ext': 'vtt'}]
        return info

    def sample_policy(self, enabled):
        url = f'/api/jobs/{self.job_id}/sample-policy'
        policy = self.client.get(url).json
        response = self.client.put(url, json={**policy, 'enabled': enabled}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)

    def test_retained_sample_keeps_all_media_and_languages(self):
        info = self.fixture()
        self.sample_policy(True)
        task, result = self.execute(info)
        samples = self.client.get(f'/api/jobs/{self.job_id}/samples').json['items']
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(samples[0]['files']), 5)
        self.assertEqual({f['language'] for f in samples[0]['files'] if f['kind'] == 'subtitle'}, {'en', 'fr', 'de'})
        for item in samples[0]['files']:
            with self.client.get(item['content_url']) as response:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.data)

    def test_enabling_samples_after_quick_test_reclaim_does_not_publish_partial_sample(self):
        from webapp.delivery.executor import commit_publications
        info = self.fixture()
        def commit(session, items):
            if items[0]['artifact']['kind'] == 'subtitle':
                self.sample_policy(True)
            return commit_publications(session, items)
        with patch('webapp.delivery.executor.commit_publications', side_effect=commit):
            task, result = self.execute(info)
        self.assertTrue(self.store.get(task['id'])['progress']['_test_files_reclaimed'])
        self.assertEqual(self.client.get(f'/api/jobs/{self.job_id}/samples').json['items'], [])

    def test_quick_test_reclaims_media_before_caption_batch_and_registers_once(self):
        from webapp.delivery.executor import commit_publications
        info = self.fixture()
        calls = []
        def commit(session, items):
            if items[0]['artifact']['kind'] == 'subtitle':
                self.assertEqual(len(items), 3)
                # Earlier media receipts remain counted, but their quick-test
                # destinations no longer consume disk while captions run.
                self.assertTrue(session.output_files)
                self.assertTrue(all(not Path(p).exists() for p in session.output_files))
            calls.append(len(items))
            return commit_publications(session, items)
        with patch('webapp.delivery.executor.commit_publications', side_effect=commit):
            task, result = self.execute(info)
        self.assertEqual(calls, [1, 1, 3])
        self.assertEqual(result['file_count'], 5)
        self.assertTrue(self.store.get(task['id'])['progress']['_test_files_reclaimed'])

    def test_failed_batch_commit_keeps_inputs_and_recovers_without_source_requests(self):
        from web_worker import execute
        info = self.fixture()
        task = self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info', return_value=info):
            with patch.object(self.store.statistics, 'record_files', side_effect=RuntimeError('batch commit failed')):
                with self.assertRaisesRegex(RuntimeError, 'batch commit failed'):
                    self.run_task(task)
        with self.store.connect() as db:
            work = dict(db.execute(select(delivery_work)).mappings().one())
        plan = json.loads(work['plan'])
        captions = [j for j in plan['jobs'] if j['kind'] == 'subtitle']
        self.assertTrue(all(Path(j['inputs'][0]).exists() for j in captions))
        self.assertTrue(all(Path(j['destination']).exists() for j in captions))
        hits = list(self.hits)
        self.store.release(task['id'], self.store.owner, 'retry local batch', delay=0)
        local = self.store.claim(self.store.owner, processing=True)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info', side_effect=AssertionError('source request')):
            result = execute('download', local['payload'], store=self.store, task_id=local['id'], worker_slot=-2)
        self.assertEqual(result['file_count'], 5)
        self.assertEqual(self.hits, hits)
        self.store.finish(local['id'], result=result, worker_id=self.store.owner)
        self.cleanup_result(result)

    def test_unchanged_queued_plan_is_refined_once(self):
        from web_worker import execute
        from webapp.disk_budget import DiskBudget
        info = self.fixture()
        task = self.store.claim(self.store.owner)
        with patch('webapp.delivery.executor.ParallelYoutubeDL.extract_info', return_value=info):
            result = execute('download', task['payload'], store=self.store, task_id=task['id'])
        self.store.finish(task['id'], result=result, worker_id=self.store.owner)
        budget = DiskBudget(self.store.directory, self.store.runtime())
        with patch.object(budget, '_refine_queued_plan', wraps=budget._refine_queued_plan) as refine:
            budget.refresh_processing(self.store)
            budget.refresh_processing(self.store)
            self.assertEqual(refine.call_count, 1)
        with self.store.connect() as db:
            work = db.execute(select(delivery_work)).mappings().one()
            plan = json.loads(work['plan']); plan['jobs'][0]['done'] = True
            db.execute(delivery_work.update().values(plan=json.dumps(plan)))
        with patch.object(budget, '_refine_queued_plan', wraps=budget._refine_queued_plan) as refine:
            budget.refresh_processing(self.store)
            self.assertEqual(refine.call_count, 1)
