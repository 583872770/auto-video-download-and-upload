"""Peak planning, shared inputs, handoff promises and hot-path accounting."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp.delivery.space import (acquisition_peak, output_bytes, source_bytes, unused_inputs,
                                  processing_peak, refine_processing_estimates)
from webapp.disk_budget import DiskBudget, DiskPressure, GIB, MIB


class SpacePlanTests(unittest.TestCase):
    def test_youtube_codec_labels_do_not_turn_stream_copy_into_transcode_forecast(self):
        source = dict(path=Path('shared'), bytes=100*MIB, format=dict(filesize=100*MIB,
            vcodec='avc1.640028', acodec='mp4a.40.2', ext='mp4', protocol='https'))
        jobs = [dict(kind='av', inputs=['shared'], mp4=True, mp3=False)]
        plan = acquisition_peak([source], jobs, 60, test_mode=True)
        self.assertEqual(plan['processing_bytes'], 0)
        source['format']['vcodec'] = 'vp09.00.50.08'
        plan = acquisition_peak([source], jobs, 60, test_mode=True)
        self.assertGreater(plan['processing_bytes'], 300*MIB)

    def test_fifty_small_captions_reserve_real_conversion_cost_instead_of_24_mib_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'caption.vtt'; path.write_bytes(b'x'*4096)
            jobs = [dict(kind='subtitle', inputs=[str(path)]) for _ in range(50)]
            self.assertEqual(processing_peak(jobs, test_mode=True), 1200*MIB)
            self.assertTrue(refine_processing_estimates(jobs))
            self.assertEqual(processing_peak(jobs, test_mode=True), 50*MIB)
            self.assertFalse(refine_processing_estimates(jobs))

    def test_real_inputs_shrink_forecasts_without_dropping_transcode_or_prepared_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source';source.write_bytes(b'x'*MIB)
            jobs = [dict(kind=k, inputs=[str(source)], output_estimate=100*MIB, mp4=mp4, mp3=False)
                    for k,mp4 in [('video_only',False),('av',True)]]
            refine_processing_estimates(jobs)
            self.assertLess(jobs[0]['output_estimate'], 2*MIB)
            self.assertEqual(jobs[1]['output_estimate'], 3*MIB)
            jobs[1]['prepared'] = 'ready';jobs[1]['output_estimate'] = 100*MIB
            refine_processing_estimates(jobs)
            self.assertEqual(jobs[1]['output_estimate'], 100*MIB)

    def test_direct_publication_keeps_its_zero_extra_write_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source'; source.write_bytes(b'x'*MIB)
            job = dict(kind='video_only', inputs=[str(source)], output_estimate=0)
            self.assertFalse(refine_processing_estimates([job]))
            self.assertEqual(processing_peak([job], test_mode=True), 0)

    def test_exact_approximate_and_rate_estimates_are_distinguished(self):
        self.assertEqual(source_bytes({'filesize': 1000}), 1000)
        self.assertEqual(source_bytes({'filesize_approx': 1000}), 1200)
        self.assertEqual(source_bytes({'tbr': 800}, 10), 1200000)
        self.assertEqual(source_bytes({}, 10), GIB)
        self.assertEqual(source_bytes({'filesize': float('nan')}, 10), GIB)

    def test_shared_progressive_source_is_counted_once(self):
        source = dict(path=Path('shared.mp4'), bytes=100 * MIB,
                      format={'filesize': 100 * MIB, 'acodec': 'aac', 'vcodec': 'h264', 'abr': 128})
        jobs = [dict(kind=k, inputs=['shared.mp4'], mp4=False, mp3=False) for k in ('video_only', 'audio_only')]
        plan = acquisition_peak([source], jobs, 60, test_mode=True)
        self.assertEqual(plan['source_bytes'], 100 * MIB)
        self.assertLess(jobs[1]['output_estimate'], 3 * MIB)
        self.assertGreater(plan['peak_bytes'], 200 * MIB)
        self.assertFalse(plan['estimated'])

    def test_independent_tracks_do_not_reserve_conversion_copies(self):
        sources = [dict(path=Path(k), bytes=100 * MIB, format=dict(filesize=100 * MIB, **fmt))
                   for k, fmt in [('v', dict(acodec='none', vcodec='h264')), ('a', dict(vcodec='none', acodec='aac'))]]
        jobs = [dict(kind=k, inputs=[p], mp4=False, mp3=False) for k, p in [('video_only', 'v'), ('audio_only', 'a')]]
        plan = acquisition_peak(sources, jobs, 60, test_mode=True)
        self.assertEqual(plan['processing_bytes'], 0)
        self.assertEqual(plan['peak_bytes'], 216 * MIB)

    def test_matching_progressive_av_does_not_reserve_a_copy_but_hls_does(self):
        source = dict(path=Path('av'), bytes=100 * MIB, format=dict(filesize=100 * MIB,
            ext='mp4', vcodec='h264', acodec='aac', protocol='https'))
        jobs = [dict(kind='av', inputs=['av', 'av'], mp4=False, mp3=False, auto=True)]
        self.assertEqual(acquisition_peak([source], jobs, 60, test_mode=True)['processing_bytes'], 0)
        source['format']['protocol'] = 'm3u8_native'
        self.assertGreater(acquisition_peak([source], jobs, 60, test_mode=True)['processing_bytes'], 0)

    def test_audio_estimate_uses_track_rate_instead_of_video_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'large.ts'
            with source.open('wb') as f: f.truncate(4 * GIB)
            probe = ({'audio', 'video'}, dict(streams=[dict(codec_type='audio', bit_rate='128000')],
                format=dict(duration='3600')))
            amount = output_bytes([source], 'audio_only', probes=[probe])
            self.assertLess(amount, 80 * MIB)
            self.assertGreater(amount, 54 * MIB)
            probe[1]['streams'][0].pop('bit_rate')
            self.assertLess(output_bytes([source], 'audio_only', probes=[probe]), 300 * MIB)

    def test_last_consumer_controls_source_retirement(self):
        jobs = [dict(inputs=['shared'], done=True), dict(inputs=['shared'])]
        self.assertEqual(unused_inputs(jobs), set())
        jobs[1]['done'] = True
        self.assertEqual(unused_inputs(jobs), {'shared'})

    def test_paired_outputs_reserve_both_writes_and_prepared_files_are_not_reserved_again(self):
        jobs = [dict(kind=k, inputs=['shared'], output_estimate=n) for k,n in
                [('video_only', 100 * MIB), ('audio_only', 5 * MIB)]]
        self.assertEqual(processing_peak(jobs, test_mode=False), 105 * MIB)
        jobs[1]['prepared'] = 'audio.m4a'
        self.assertEqual(processing_peak(jobs, test_mode=True), 100 * MIB)
        jobs[0]['done'] = True
        self.assertEqual(processing_peak(jobs, test_mode=True), 0)

    def test_processing_reservation_does_not_bypass_physical_low_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = DiskBudget(tmp, dict(disk_min_free_gb=1, disk_resume_free_gb=2))
            with budget.lease('test', task='b' * 32) as source:
                source.plan(40 * MIB)
                source.preserve = True
            local = budget.adopt(source.id, source.task, source.path)
            with self.assertRaises(DiskPressure):
                with local, patch.object(budget, '_free', return_value=512 * MIB):
                    local.check(force=True)

    def test_handoff_preserves_completion_reservation_and_terminal_cleanup_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = DiskBudget(tmp, dict(disk_min_free_gb=0, disk_resume_free_gb=0))
            lease = budget.lease('test', task='a' * 32)
            with lease:
                lease.plan(40 * MIB)
                self.assertEqual(lease.reserved, 40 * MIB)
                (lease.path / 'source').write_bytes(b'x' * 4096)
                lease.preserve = True
            with budget.connect(write=False) as db:
                self.assertEqual(db.execute('SELECT reserved FROM leases').fetchone()[0], 40 * MIB)
            local = budget.adopt(lease.id, lease.task, lease.path)
            with local:
                local.checkpoint_space()
            with budget.connect(write=False) as db:
                self.assertIsNone(db.execute('SELECT id FROM leases').fetchone())
                self.assertIsNone(db.execute("SELECT key FROM state WHERE key LIKE 'completion:%'").fetchone())

    def test_planned_download_prevents_a_second_task_overcommitting(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = DiskBudget(tmp, dict(disk_budget_gb=1, disk_test_budget_gb=1,
                                         disk_min_free_gb=0, disk_resume_free_gb=0))
            with budget.lease('test', reserve=0) as first:
                first.plan(700 * MIB)
                with self.assertRaises(DiskPressure):
                    with budget.lease('test', reserve=400 * MIB): pass
                first.check(force=True)  # Existing promise still finishes.

    def test_impossible_single_task_fails_instead_of_waiting_forever(self):
        from webapp.delivery.verify import LocalDeliveryError
        with tempfile.TemporaryDirectory() as tmp:
            budget = DiskBudget(tmp, dict(disk_budget_gb=1, disk_test_budget_gb=1,
                                         disk_min_free_gb=0, disk_resume_free_gb=0))
            with budget.lease('test', reserve=0) as lease:
                with self.assertRaisesRegex(LocalDeliveryError, '预计完成峰值'):
                    lease.plan(2 * GIB)

    def test_processing_scratch_blocks_new_growth_but_not_existing_promises(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = DiskBudget(tmp, dict(disk_budget_gb=1, disk_test_budget_gb=1,
                                         disk_min_free_gb=0, disk_resume_free_gb=0))
            with budget.lease('test', reserve=400 * MIB) as running, budget.lease('test', reserve=0) as waiting:
                with budget.connect() as db:
                    budget._set(db, 'scratch:' + waiting.id, 400 * MIB)
                running.check(force=True)
                with self.assertRaisesRegex(DiskPressure, '后处理周转额度'):
                    with budget.lease('test', reserve=400 * MIB): pass

    def test_preclaim_space_hint_preserves_processing_scratch_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = DiskBudget(tmp, dict(disk_budget_gb=1, disk_test_budget_gb=1,
                disk_min_free_gb=0, disk_resume_free_gb=0, storage_mode='test'))
            with budget.lease('test', task='running', reserve=400 * MIB):
                with budget.lease('test', reserve=0) as waiting:
                    with budget.connect() as db:
                        budget._set(db, 'scratch:' + waiting.id, 400 * MIB)
                    with self.assertRaises(DiskPressure):
                        with budget.lease('test', task='waiting', reserve=400 * MIB): pass
                    self.assertIn('waiting', budget.blocked_tasks())
                self.assertNotIn('waiting', budget.blocked_tasks())

    def test_processing_backlog_shares_space_but_concurrent_workers_can_finish(self):
        from webapp.database import delivery_work
        from webapp.store import TaskStore

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as resources:
            store = TaskStore(tmp)
            resources.callback(store.db.engine.dispose)
            budget = DiskBudget(tmp, dict(disk_budget_gb=1, disk_test_budget_gb=1,
                disk_min_free_gb=0, disk_resume_free_gb=0, postprocess_limit=2, storage_mode='test'))
            leases = []
            for index in range(4):
                task = store.create('download', {'urls': [f'https://www.youtube.com/watch?v={index:011}']})
                with budget.lease('test', task=task['id'], reserve=225 * MIB) as lease:
                    source = lease.path / 'source.mp4'
                    with source.open('wb') as stream:
                        stream.truncate(100 * MIB)
                    lease.plan(225 * MIB)
                    lease.preserve = True
                    plan = dict(config={'storage_mode': 'test'}, jobs=[dict(
                        kind='video_only', inputs=[str(source)], output_estimate=105 * MIB)])
                    with store.connect() as db:
                        db.execute(delivery_work.insert().values(task_id=task['id'], instance_id='local',
                            workspace=str(lease.path), disk_lease=lease.id, plan=json.dumps(plan),
                            created_at=index, source_attempts=1))
                leases.append(lease)

            with self.assertRaises(DiskPressure):
                with budget.lease('test', task='waiting', reserve=300 * MIB):
                    pass
            budget.refresh_processing(store)
            self.assertNotIn('waiting', budget.blocked_tasks())
            with budget.connect(write=False) as db:
                self.assertEqual(budget._processing_scratch(db), 242 * MIB)
                usage, committed = budget._usage(db)
                self.assertEqual(committed['test'], usage['test'])
            with budget.lease('test', task='waiting', reserve=300 * MIB):
                with self.assertRaises(DiskPressure):
                    with budget.lease('test', reserve=125 * MIB):
                        pass
                with budget.adopt(leases[0].id, leases[0].task, leases[0].path) as first:
                    first.reserve_processing_space()
                    with budget.adopt(leases[1].id, leases[1].task, leases[1].path) as second:
                        second.reserve_processing_space()
                        self.assertEqual(first.reserved - first.measured_bytes, 121 * MIB)
                        self.assertEqual(second.reserved - second.measured_bytes, 121 * MIB)
                        snapshot = budget.snapshot()
                        unwritten = snapshot['committed_bytes'] - snapshot['used_bytes']
                        self.assertGreater(unwritten, 541 * MIB)
                        self.assertLessEqual(unwritten, 542 * MIB)

    def test_repeated_progress_checks_do_not_rescan_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = DiskBudget(tmp, dict(disk_min_free_gb=0, disk_resume_free_gb=0))
            with budget.lease('test') as lease:
                lease.plan(2 * GIB)
                with patch('webapp.disk_budget.size_of', side_effect=AssertionError('hot path scanned')):
                    for _ in range(20): lease.check()


if __name__ == '__main__': unittest.main()
