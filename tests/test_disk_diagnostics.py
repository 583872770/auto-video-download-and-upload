"""Disk warnings explain real protection without introducing new admission gates."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from sqlalchemy import select

from tests import test_scale
from webapp.database import notifications, tasks
from webapp.disk_budget import DiskBudget, DiskPressure, GIB, MIB, resource_blocked
from webapp.disk_diagnostics import evaluate_alert, host_usage, explain, summary
from webapp.jobs import Jobs


class DiskSamplesTests(unittest.TestCase):
    def test_two_spaced_samples_and_hysteresis(self):
        state = evaluate_alert({}, 91, 100)
        self.assertEqual(state['severity'], 'normal')
        self.assertEqual(evaluate_alert(state, 91, 120), state)
        state = evaluate_alert(state, 91, 130)
        self.assertEqual(state['severity'], 'critical')
        state = evaluate_alert(state, 86, 160)
        self.assertEqual(state['severity'], 'critical')
        state = evaluate_alert(state, 82, 190)
        state = evaluate_alert(state, 82, 220)
        self.assertEqual(state['severity'], 'warning')
        state = evaluate_alert(state, 76, 250)
        self.assertEqual(state['severity'], 'warning')
        state = evaluate_alert(state, 74, 280)
        state = evaluate_alert(state, 74, 310)
        self.assertEqual(state['severity'], 'normal')

    def test_unavailable_corrupt_stale_and_duplicate_host_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host-disk-usage.json'
            self.assertEqual(host_usage(directory, 1000)['status'], 'unavailable')
            for invalid in ('[]', '{', '{"sampled_at":1000,"sources":{}}'):
                path.write_text(invalid, encoding='utf-8')
                self.assertEqual(host_usage(directory, 1000)['status'], 'unavailable')
            source = dict(key='mysql_binlog', used_bytes=42, device=str(Path(directory).stat().st_dev))
            path.write_text(json.dumps(dict(sampled_at=1000, sources=[None, source, source,
                dict(key='mysql_data', used_bytes=-1), dict(key='private', used_bytes=900)])))
            report = host_usage(directory, 1000)
            self.assertEqual(len(report['sources']), 2)
            self.assertTrue(report['sources'][0]['same_filesystem'])
            self.assertIsNone(report['sources'][1]['used_bytes'])
            self.assertEqual(host_usage(directory, 2000)['status'], 'stale')


class DiskNoticeTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def budget(self, **config):
        return DiskBudget(self.store.directory, dict(disk_min_free_gb=0, disk_resume_free_gb=0, **config))

    def snapshot(self, budget, free=10*GIB, total=100*GIB):
        value = budget.snapshot()
        return {**value, 'free_bytes': free, 'total_bytes': total, 'low_space': False}

    def job_task(self):
        identity = Jobs(self.store).create({'customer': 'test', 'name': 'space', 'urls': 'SjePkzdiyWU'},
            self.store.runtime(), self.root)['id']
        Jobs(self.store).control(identity, 'start', self.store.runtime(), {})
        return self.store.claim(self.store.owner, actions=['download'])

    def test_usage_warning_does_not_block_admission_and_summary_uses_only_cache(self):
        budget = self.budget()
        snapshot = self.snapshot(budget)
        with patch('webapp.disk_diagnostics.time.time', return_value=1000):
            budget.publish(self.store, snapshot)
        with patch('webapp.disk_diagnostics.time.time', return_value=1031):
            budget.publish(self.store, snapshot)
            with self.store.connect() as db:
                self.assertFalse(resource_blocked(self.store, db))
                self.assertEqual(summary(db, 'local')['severity'], 'critical')
        with patch('webapp.disk_diagnostics.time.time', return_value=1122):
            with self.store.connect() as db:
                self.assertIsNone(summary(db, 'local'))

    def test_initial_budget_denial_is_visible_without_consuming_an_attempt(self):
        task = self.job_task()
        budget = self.budget(disk_import_budget_mb=1)
        with self.assertRaises(DiskPressure) as raised:
            with budget.lease('imports', reserve=2*MIB, task=task['id']):
                self.fail('must not reserve more than the category budget')
        detail = raised.exception.detail
        self.assertEqual(detail['kind'], 'budget')
        self.assertLessEqual(detail['allocatable_bytes'], MIB)
        with budget.connect(write=False) as db:
            self.assertEqual(db.execute('SELECT interruptions FROM requirements').fetchone()[0], 0)
        live = {'stage': 'processing', 'cookie_profile_id': 'current-profile', 'title': 'live title'}
        with patch.object(self.store.progress_cache, 'read', return_value={'progress': live, 'updated_at': time.time()}):
            self.store.release(task['id'], self.store.owner, str(raised.exception), resource=True, resource_detail=detail)
        saved = self.store.get(task['id'])
        self.assertEqual(saved['progress']['cookie_profile_id'], 'current-profile')
        self.assertEqual(saved['progress']['stage'], 'disk_wait')
        self.assertEqual(saved['attempts'], 0)
        diagnostic = budget.snapshot()['diagnostic']
        self.assertEqual(diagnostic['severity'], 'blocked')
        self.assertEqual(diagnostic['kind'], 'budget')
        maintenance = self.budget(disk_import_budget_mb=1, download_format='a different task output')
        self.assertEqual(maintenance.snapshot()['diagnostic']['severity'], 'blocked',
            'instance maintenance must not dismiss a task wait just because its output policy differs')
        budget.publish(self.store, budget.snapshot())
        page = self.client.get('/api/notifications').json
        resource = page['resource_alerts'][0]
        affected = self.client.get('/api/notifications/'+resource['id']+'/tasks').json
        self.assertEqual([r['task_id'] for r in affected['items']], [task['id']])
        self.assertEqual(affected['items'][0]['detail']['phase'], '本地处理')
        # A denied large task must not block smaller tasks for the whole instance.
        with self.store.connect() as db:
            self.assertFalse(resource_blocked(self.store, db))
        # Increasing the budget permits a real recovery, even before task pickup.
        larger = self.budget(disk_import_budget_mb=8)
        self.assertEqual(larger.snapshot()['diagnostic']['severity'], 'normal')

    def test_actual_free_and_reservations_are_separate_and_recovery_keeps_pause(self):
        self.store.set_downloads_paused(True)
        budget = self.budget()
        snapshot = self.snapshot(budget, free=GIB)
        snapshot.update(low_space=True, committed_bytes=3*GIB)
        budget.publish(self.store, snapshot)
        self.assertTrue(self.store.downloads_paused())
        with self.store.connect() as db:
            self.assertTrue(resource_blocked(self.store, db))
        healthy = self.snapshot(budget, free=50*GIB)
        with patch('webapp.disk_diagnostics.time.time', return_value=time.time()+31):
            budget.publish(self.store, healthy)
        page = self.client.get('/api/notifications?state=resolved').json
        resource = next(item for item in page['items'] if item['code'] == 'disk_resource')
        self.assertTrue(resource['detail']['recovery']['paused'])
        self.assertIn('仍处于暂停', resource['detail']['activity'])
        self.assertEqual(resource['detail']['recovery']['freed_bytes'], 49*GIB)
        self.assertTrue(self.store.downloads_paused())

    def test_source_filesystem_and_unwritten_reservations_do_not_inflate_disk_use(self):
        budget = self.budget()
        snapshot = self.snapshot(budget, free=50*GIB)
        snapshot.update(used_bytes=GIB, committed_bytes=5*GIB)
        host = dict(status='ok', sampled_at=time.time(), sources=[
            dict(key='mysql_binlog', label='binlog', used_bytes=3*GIB, same_filesystem=True),
            dict(key='mysql_data', label='DB', used_bytes=20*GIB, same_filesystem=False)])
        with patch('webapp.disk_diagnostics.host_usage', return_value=host):
            diagnostic = explain(budget, snapshot)
        self.assertEqual(diagnostic['used_percent'], 50)
        self.assertEqual(diagnostic['unwritten_reserved_bytes'], 4*GIB)
        known = sum(s['used_bytes'] or 0 for s in diagnostic['sources'] if s['same_filesystem'])
        self.assertEqual(known, 50*GIB)
        unknown = next(s for s in diagnostic['sources'] if s['key'] == 'docker_images')
        self.assertIsNone(unknown['used_bytes'])

    def test_retention_counts_completed_residue_separately_from_active_work(self):
        task = self.job_task()
        budget = self.budget()
        with budget.lease('test', reserve=MIB, task=task['id']):
            budget._retention_health(self.store, 0)
            self.assertEqual(budget.snapshot()['diagnostic']['retention']['pending_count'], 1)
            with self.store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='succeeded',
                    finished_at='2020-01-01T00:00:00+00:00'))
            budget._retention_health(self.store, 1)
            diagnostic = budget.snapshot()['diagnostic']
            self.assertEqual(diagnostic['severity'], 'warning')
            self.assertEqual(diagnostic['retention']['completed_count'], 1)
            self.assertEqual(diagnostic['retention']['pending_count'], 0)
            self.assertEqual(diagnostic['retention']['failed_count'], 1)

    def test_successful_readmission_preserves_interruption_history_but_is_not_waiting(self):
        task = self.job_task()
        budget = self.budget(disk_import_budget_mb=4)
        with budget.connect() as db:
            db.execute('INSERT INTO requirements VALUES (?,?,?,?,?)',
                (task['id'], budget._version(), MIB, 2, 0))
        with budget.lease('imports', task=task['id'], reserve=MIB) as lease:
            self.assertEqual(budget.snapshot()['waiting_tasks'], 0)
            with budget.connect(write=False) as db:
                self.assertEqual(db.execute('SELECT interruptions FROM requirements').fetchone()[0], 2)
            with self.assertRaises(DiskPressure):
                budget._reserve(lease, 5*MIB)
            with budget.connect(write=False) as db:
                self.assertEqual(db.execute('SELECT interruptions FROM requirements').fetchone()[0], 3)

    def test_retained_receipt_does_not_hide_waiting_task_or_pressure(self):
        task = self.job_task()
        budget = self.budget(disk_import_budget_mb=4)
        with budget.lease('imports', task=task['id'], reserve=3*MIB) as lease:
            (lease.path / '.completed-files.json').write_text('[]')
            with self.assertRaises(DiskPressure):
                budget._reserve(lease, 5*MIB)
            lease.preserve = True
        snapshot = budget.snapshot()
        self.assertEqual(snapshot['waiting_tasks'], 1)
        self.assertEqual(snapshot['diagnostic']['pressure']['task_id'], task['id'])
        with budget.connect(write=False) as db:
            self.assertEqual(db.execute('SELECT interruptions FROM requirements').fetchone()[0], 1)

    def test_untracked_test_files_require_sustained_evidence_and_are_not_deleted(self):
        budget = self.budget()
        orphan = budget.directory/'test-downloads'/'untracked.bin'
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(b'x'*(2*MIB))
        budget.snapshot(force=True)
        budget._retention_health(self.store, 1, ['文件权限不足'])
        health = budget.snapshot()['diagnostic']['retention']
        self.assertFalse(health['untracked_overdue'])
        self.assertEqual(health['failure_checks'], 1)
        with budget.connect() as db:
            health['untracked_since'] = time.time()-601
            budget._set(db, 'retention_health', health)
        budget._retention_health(self.store, 1, ['文件权限不足'])
        health = budget.snapshot()['diagnostic']['retention']
        self.assertTrue(health['untracked_overdue'])
        self.assertGreaterEqual(health['untracked_bytes'], 2*MIB)
        self.assertLess(health['untracked_bytes'], 2*MIB+65536)
        self.assertEqual(health['failure_checks'], 2)
        self.assertEqual(health['failure_reasons'], ['文件权限不足'])
        self.assertTrue(orphan.is_file())


class CollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('collector', Path(__file__).resolve().parents[1]/'docker/collect_disk_usage.py')
        cls.collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.collector)

    def test_docker_shared_layers_count_once(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps(dict(LayersSize=100, Images=[
            dict(Size=80, SharedSize=60, Containers=0), dict(Size=90, SharedSize=60, Containers=1)])).encode()
        with patch.object(self.collector, 'DockerHTTP', return_value=connection):
            result = self.collector.image_usage()
        self.assertEqual(result['used_bytes'], 100)
        self.assertEqual(result['reclaimable_bytes'], 20)
        connection.close.assert_called_once()

    def test_remote_database_is_never_attributed_to_local_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            app = dict(Mounts=[dict(Destination='/app/.cache/web', Source=directory)],
                Config=dict(Env=['MYSQL_HOST=remote.example']), NetworkSettings=dict(Networks={'app': {}}))
            node = dict(Mounts=[dict(Destination='/var/lib/mysql')], Config=dict(Hostname='mysql'),
                NetworkSettings=dict(Networks={'app': dict(Aliases=['mysql'], IPAddress='172.1.1.2')}))
            def command(args, **kwargs):
                return json.dumps(directory) if args[1] == 'info' else 'local-db'
            with patch.object(self.collector, 'inspect', side_effect=lambda name: app if name == 'app' else node), \
                    patch.object(self.collector, 'command', side_effect=command), \
                    patch.object(self.collector, 'allocated', return_value=1), \
                    patch.object(self.collector, 'image_usage', return_value=dict(used_bytes=4)), \
                    patch.object(self.collector, 'mysql_usage') as mysql:
                _, report = self.collector.collect('app', directory)
            mysql.assert_not_called()
            self.assertIsNone(next(s for s in report['sources'] if s['key']=='mysql_binlog')['used_bytes'])


if __name__ == '__main__':
    unittest.main()
