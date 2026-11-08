"""Regression checks for bounded workers, read-only pool views and audit evidence."""
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from sqlalchemy import event, select
from sqlalchemy.dialects.mysql import dialect

from tests import test_cookie_pool, test_fleet
from webapp.channel_scheduler import ChannelScheduler
from webapp.cookie_pool import CookiePool
from webapp.database import tasks, runtime_hours, cookie_profiles
from webapp.db_pool_calculator import calculate_pool_size, connection_budget
from webapp.disk_budget import DiskCoordinationBusy, MIB
from webapp.download_policy import failure_evidence
from webapp.downloads import DownloadFailure, DownloadLeaseLost
from webapp.maintenance import prune_disk_logs
from webapp.runtime_summary import read, prune, record_terminal
from webapp.store import TaskStore
from webapp.throughput import (PipelineBusy, ThroughputController, _atomic, policy_key,
                               pool_worker_limits, runtime_limits, worker_should_retire)


class PoolSamplingTests(unittest.TestCase):
    setUp = test_cookie_pool.CookiePoolTests.setUp
    pool = test_cookie_pool.CookiePoolTests.pool

    def test_twenty_member_status_uses_five_selects_and_no_router_lock(self):
        identities = self.ids + [self.profiles.add(str(i), test_cookie_pool.COOKIE.replace(b'TEST_SECRET', str(i).encode()))['id']
                                 for i in range(3, 20)]
        config = {**self.config, 'youtube_cookie_pool': identities}
        self.pool(config).status()  # Initialize durable aliases once.
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.store.db.engine, 'before_cursor_execute', capture)
        try:
            with patch.object(ChannelScheduler, 'transaction_call', side_effect=AssertionError('display acquired router lock')):
                result = CookiePool(self.store, config, self.base, None, strict=False).status()
        finally:
            event.remove(self.store.db.engine, 'before_cursor_execute', capture)
        self.assertEqual(result['capacity_summary']['effective_sessions'], 20)
        self.assertEqual(len(statements), 5, statements)
        self.assertTrue(all(statement.lstrip().upper().startswith('SELECT') for statement in statements))
        self.assertTrue(all('FOR UPDATE' not in statement.upper() for statement in statements))

    def test_profile_replacement_and_deletion_are_not_hidden_by_parsing_cache(self):
        first = self.pool()
        scope = first.accounts[0][2]
        with self.store.connect() as db:
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id == self.ids[0]).values(
                content=test_cookie_pool.COOKIE.replace(b'TEST_SECRET', b'REPLACED_SESSION').decode()))
        replaced = CookiePool(self.store, self.config, self.base, None, strict=False)
        self.assertEqual(replaced.accounts[0][2], scope)  # Same named account retains its health budget.
        self.assertIn(b'REPLACED_SESSION', replaced.accounts[0][1].read_bytes())
        self.profiles.delete(self.ids[0])
        result = CookiePool(self.store, self.config, self.base, None, strict=False).status()
        self.assertEqual(result['capacity_summary']['unavailable_files'], 1)
        self.assertEqual(result['effective_sessions'], 2)

    def test_local_waits_do_not_write_cookie_failures_or_health_state(self):
        pool = self.pool()
        scope = pool.accounts[0][2]
        before = pool.status()['accounts']
        with patch('webapp.youtube_cookie_history.record') as record:
            for error in (PipelineBusy('channel backoff'), DiskCoordinationBusy('accounting busy'), DownloadLeaseLost('lease lost')):
                pool.outcome(scope, error)
            record.assert_not_called()
        self.assertEqual(pool.status()['accounts'], before)

    def test_read_snapshot_counts_only_live_owner_fenced_permits(self):
        pool = self.pool()
        with pool.assignment(count_success=False):
            self.assertEqual(sum(row['active'] for row in pool.status()['accounts']), 1)
            with self.store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == pool.router.task_id).values(worker_id='another-owner'))
            self.assertEqual(sum(row['active'] for row in pool.status()['accounts']), 0)


class WorkerBudgetTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = dict(youtube_cookie_pool=['fixture'] * 18, builtin_transfer_limit=18,
                           builtin_connection_limit=16, download_connection_limit=32,
                           download_methods=['yt_dlp'], fragment_concurrency=4)
        self.resources = dict(cpu_cores=4, memory_total=16384*MIB, memory_available=7000*MIB,
                              cpu_percent=50, io_pressure=0, rx=0)

    def test_pool_caps_count_main_auxiliary_and_each_worker_pool(self):
        state = pool_worker_limits(self.config, 36, self.resources)
        self.assertEqual(state['worker_transfer_capacity'], 18)
        self.assertEqual(state['configured_worker_limit'], 20)
        self.assertEqual(state['workers'], 18)  # 48 - 7 main - 4 auxiliary, at 2 each.
        self.assertLessEqual(state['workers'] * state['db_connections_per_worker'] +
                             state['db_main_connections'] + state['db_auxiliary_connections'], 48)
        with patch.dict(os.environ, {'VIDEO_DB_CONNECTION_BUDGET': '64'}):
            self.assertEqual(pool_worker_limits(self.config, 36, self.resources)['workers'], 20)
        with patch.dict(os.environ, {'VIDEO_WORKER_DB_POOL_SIZE': '3', 'VIDEO_WORKER_DB_MAX_OVERFLOW': '1'}):
            self.assertEqual(calculate_pool_size(True), (3, 1))
            self.assertEqual(connection_budget()['workers'], 8)

    def test_explicit_main_pool_is_honored_and_invalid_budget_fails_early(self):
        with patch.dict(os.environ, {'VIDEO_DB_POOL_SIZE': '3', 'VIDEO_DB_MAX_OVERFLOW': '1'}):
            self.assertEqual(calculate_pool_size(), (3, 1))
        with patch.dict(os.environ, {'VIDEO_DB_CONNECTION_BUDGET': '8'}):
            with self.assertRaises(ValueError):
                calculate_pool_size()

    def test_low_memory_and_saved_limits_cannot_restore_session_sized_workers(self):
        low = {**self.resources, 'memory_total': 1024*MIB}
        self.assertLessEqual(pool_worker_limits(self.config, 36, low)['workers'], 2)
        config = {**self.config, 'youtube_cookie_pool_worker_limit': 6}
        controller = ThroughputController(self.root)
        with patch.object(controller, 'sample', return_value=self.resources):
            state = controller.update(config, cookie_capacity={'known': True, 'capacity': 36})
        self.assertEqual(state['workers'], 6)
        self.assertEqual(runtime_limits(config, self.root)['workers'], 6)
        changed = {**config, 'youtube_cookie_pool_worker_limit': 3}
        with patch('webapp.throughput.resource_limits', return_value=self.resources):
            self.assertEqual(runtime_limits(changed, self.root)['workers'], 3)

    def test_hot_shrink_retires_before_another_task_claim(self):
        _atomic(self.root / 'throughput.json', dict(time=time.time(), workers=2))
        self.assertTrue(worker_should_retire(self.root, 2))
        self.assertFalse(worker_should_retire(self.root, 1))
        self.assertFalse(worker_should_retire(self.root, 0))
        _atomic(self.root / 'throughput.json', dict(time=time.time()-30, workers=1))
        self.assertFalse(worker_should_retire(self.root, 2))

    def test_pressure_reduction_does_not_rebound_on_the_next_five_second_sample(self):
        tick = [100]
        sample = dict(self.resources)
        controller = ThroughputController(self.root, clock=lambda: tick[0])
        with patch.object(controller, 'sample', side_effect=lambda: dict(sample)):
            initial = controller.update(self.config, cookie_capacity={'known': True, 'capacity': 36})
            sample['cpu_percent'] = 97
            tick[0] += 61
            reduced = controller.update(self.config, cookie_capacity={'known': True, 'capacity': 36})
            self.assertLess(reduced['workers'], initial['workers'])
            sample['cpu_percent'] = 50
            tick[0] += 5
            held = controller.update(self.config, cookie_capacity={'known': True, 'capacity': 36})
            self.assertEqual(held['workers'], reduced['workers'])
            tick[0] += 61
            recovered = controller.update(self.config, cookie_capacity={'known': True, 'capacity': 36})
            self.assertEqual(recovered['workers'], reduced['workers'] + 1)


class FailureDetailTests(unittest.TestCase):
    def test_partial_drm_warning_is_not_proof_all_media_is_drm(self):
        failure = DownloadFailure()
        failure.warning('Some tv_samsung https formats have been skipped as they are DRM protected')
        failure.error('Requested format is not available')
        error = failure.as_error()
        self.assertEqual(error.failure_reason, 'format_unavailable')
        self.assertIn('不能认定全部格式', str(error))
        failure.warning('This video is drm protected and only images are available for download')
        error = failure.as_error()
        self.assertEqual(error.failure_reason, 'drm_only')
        self.assertEqual(failure_evidence(error).kind, 'content_restriction')
        self.assertFalse(failure_evidence(error).retryable)

    def test_no_data_is_transient_but_cannot_override_403_429_or_auth(self):
        failure = DownloadFailure()
        failure.error('Did not get any data blocks')
        value = failure_evidence(failure.as_error(), phase='transfer')
        self.assertEqual((value.kind, value.reason, value.retryable), ('transient_failure', 'no_data', True))
        for message, expected in (
                ('HTTP Error 403: Forbidden; Did not get any data blocks', 'access_denied'),
                ('HTTP Error 429; Did not get any data blocks', 'request_rate_limit'),
                ('Sign in to confirm you are not a bot; Did not get any data blocks', 'verification_required')):
            self.assertEqual(failure_evidence(RuntimeError(message)).kind, expected)

    def test_old_drm_warning_cannot_overwrite_http_access_failure(self):
        failure = DownloadFailure()
        failure.warning('This video is drm protected and only images are available')
        failure.error('HTTP Error 403: Forbidden')
        value = failure_evidence(failure.as_error())
        self.assertEqual((value.kind, value.http_status, value.reason), ('access_denied', 403, None))


class RuntimeSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = TaskStore(self.root / 'state')

    def task(self, progress=None):
        task = self.store.create('download', {'base': str(self.root),
            'config': {'download_attempts': 3}, 'urls': ['https://youtu.be/00000000001']})
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='running', attempts=1,
                worker_id=self.store.owner, lease_until=time.time()+60, progress=json.dumps(progress or {})))
        return task

    def test_final_results_survive_log_cleanup_and_retries_are_not_failures(self):
        success = self.task({'elapsed': 10})
        self.store.finish(success['id'], result={'file_count': 1, 'file_bytes': 12345, 'test_mode': True}, worker_id=self.store.owner)
        self.assertFalse(self.store.finish(success['id'], result={'file_bytes': 9999}, worker_id=self.store.owner))
        retry = self.task({'last_failure': {'kind': 'transient_failure', 'reason': 'no_data'}})
        self.store.finish(retry['id'], error='no data', worker_id=self.store.owner, retryable=True)
        self.assertEqual(read(self.store)['totals']['completed'], 1)
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == retry['id']).values(
                status='running', attempts=3, worker_id=self.store.owner, lease_until=time.time()+60))
        self.store.finish(retry['id'], error='no data', worker_id=self.store.owner, retryable=True)
        prune_disk_logs(self.store, {'disk_log_budget_mb': 1}, low_space=True)
        result = read(self.store)
        self.assertEqual(result['totals']['completed'], 2)
        self.assertEqual(result['totals']['file_bytes'], 12345)
        self.assertEqual(result['totals']['success_rate'], 50)
        self.assertEqual(result['failure_reasons'], {'no_data': 1})
        self.assertEqual(TaskStore(self.store.directory).get(success['id'])['status'], 'succeeded')
        self.assertEqual(read(TaskStore(self.store.directory))['totals']['completed'], 2)

    def test_racing_duplicate_settlements_count_once_and_rollback_is_atomic(self):
        task = self.task()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.store.finish(task['id'], result={'file_count': 1}, worker_id=self.store.owner), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(read(self.store)['totals']['completed'], 1)
        second = self.task()
        with patch.object(self.store.statistics, 'transition', side_effect=RuntimeError('rollback fixture')):
            with self.assertRaises(RuntimeError):
                self.store.finish(second['id'], error='failed', worker_id=self.store.owner)
        self.assertEqual(self.store.get(second['id'])['status'], 'running')
        self.assertEqual(read(self.store)['totals']['completed'], 1)

    def test_hour_retention_is_scoped_and_sql_compiles_for_mysql(self):
        stamp = time.time()
        with self.store.connect() as db:
            record_terminal(self.store, db, {'action': 'download', 'instance_id': None}, 'failed', {}, {}, stamp-32*86400)
            record_terminal(self.store, db, {'action': 'download', 'instance_id': 'other'}, 'failed', {}, {}, stamp-32*86400)
        prune(self.store, stamp)
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(runtime_hours.c.scope)).scalars().all(), ['other'])
        from sqlalchemy.dialects.mysql import insert
        statement = insert(runtime_hours).values(scope='local', hour=1, outcome='failed', reason='unknown',
            task_count=1, file_count=0, file_bytes=0, elapsed_seconds=0).on_duplicate_key_update(
                task_count=runtime_hours.c.task_count+1)
        self.assertIn('ON DUPLICATE KEY UPDATE', str(statement.compile(dialect=dialect())))

    def test_no_data_retry_pins_instance_and_cookie_then_restores_original_target(self):
        profile = 'a' * 32
        progress = {'cookie_profile_id': profile, 'last_failure': {
            'kind': 'transient_failure', 'reason': 'no_data', 'profile_id': profile}}
        task = self.task(progress)
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(instance_id='alpha'))
        self.store.finish(task['id'], error='no data', worker_id=self.store.owner, retryable=True)
        queued = self.store.get(task['id'], private=True)
        self.assertEqual(queued['target_instance_id'], 'alpha')
        self.assertEqual(queued['payload']['_retry_cookie_profile_id'], profile)
        self.assertEqual(queued['payload']['_retry_cookie_instance_id'], 'alpha')
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='running', attempts=2,
                worker_id=self.store.owner, lease_until=time.time()+60,
                progress=json.dumps({'retry_cookie_profile_id': profile})))
        self.store.finish(task['id'], result={'file_count': 1}, worker_id=self.store.owner)
        finished = self.store.get(task['id'], private=True)
        self.assertIsNone(finished['target_instance_id'])
        self.assertNotIn('_retry_cookie_profile_id', finished['payload'])
        self.assertNotIn('_retry_cookie_instance_id', finished['payload'])

    def test_failed_logs_are_kept_ahead_of_newer_success_logs(self):
        failed, success = self.task(), self.task()
        with self.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == failed['id']).values(status='failed'))
            db.execute(tasks.update().where(tasks.c.id == success['id']).values(status='succeeded'))
        paths = [self.store.log_path(item['id']) for item in (failed, success)]
        paths[0].write_bytes(b'f' * (200*1024))
        paths[1].write_bytes(b's' * (400*1024))
        for path, age in zip(paths, (86400, 300)):
            os.utime(path, (time.time()-age, time.time()-age))
        prune_disk_logs(self.store, {'disk_log_budget_mb': 1})
        self.assertTrue(paths[0].exists())
        self.assertFalse(paths[1].exists())
        policy = json.loads((self.store.directory/'log-retention.json').read_text())
        self.assertEqual(policy['unexpired_failure_files_removed'], 0)


class RuntimeSummaryWebTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet
    serve = test_fleet.FleetTests.serve

    def test_selected_instance_summary_is_forwarded_and_scoped(self):
        for identity, amount in [('alpha', 100), ('beta', 200)]:
            store = self.apps[identity].extensions['task_store']
            with store.connect() as db:
                record_terminal(store, db, {'action': 'download', 'instance_id': identity}, 'succeeded',
                    {}, {'file_count': 1, 'file_bytes': amount}, time.time())
        self.serve('alpha')
        client = self.apps['manager'].test_client()
        response = client.get('/api/disk/runtime-summary?days=7&instance_id=alpha')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['scope'], 'alpha')
        self.assertEqual(response.json['totals']['file_bytes'], 100)
        self.assertEqual(response.json['totals']['completed'], 1)
        for days in ('0', '31', 'nan'):
            self.assertEqual(self.apps['alpha'].test_client().get(
                '/api/disk/runtime-summary?days=' + days).status_code, 400)


if __name__ == '__main__':
    unittest.main()
