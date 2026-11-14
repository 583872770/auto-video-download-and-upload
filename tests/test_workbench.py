"""Workbench switches, claims, selections and preflight are instance-local."""
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import event, select

from tests import test_fleet, test_jobs
from webapp.database import instances, settings, job_profiles, download_lists, download_sessions


class WorkbenchFleetTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet
    create = test_jobs.JobTests.create

    def test_compact_fleet_details_and_poll_cache_follow_selected_instance(self):
        from webapp.display_cache import display_cache
        display_cache(self.apps['manager']).cache.ttl = 60
        for selected, other in [('alpha', 'beta'), ('beta', 'alpha'), ('alpha', 'beta')]:
            response = self.client.get('/api/instances?poll=1&compact=1', headers={'X-Instance-ID': selected})
            self.assertEqual(response.status_code, 200)
            items = {item['id']: item for item in response.json['items']}
            self.assertIn('concurrency', items[selected])
            self.assertNotIn('concurrency', items[other])
            self.assertNotIn('throughput', items[selected])
            self.assertNotIn('current_tasks', items[selected])
        full = self.client.get('/api/instances?poll=1').json
        self.assertTrue(all('concurrency' in item for item in full['items'] if item['role'] == 'worker'))

    def test_compact_workbench_retains_counts_session_and_fresh_speed(self):
        self.pair()
        task = self.a.claim('alpha-owner')
        other = self.b.claim('beta-owner')
        self.a.update_progress(task['id'], dict(stage='downloading', speed=123456, last_bytes_at=time.time()), 'alpha-owner')
        self.b.update_progress(other['id'], dict(stage='downloading', speed=999999, last_bytes_at=time.time()), 'beta-owner')
        response = self.client.get('/api/jobs/workbench?poll=1&compact=1', headers={'X-Instance-ID': 'alpha'})
        value = response.json
        self.assertEqual(value['running'], 1)
        self.assertEqual(value['job']['running'], 1)
        self.assertIsNotNone(value['session'])
        self.assertNotIn('active_tasks', value)
        self.assertNotIn('running_videos', value['job'])
        self.assertEqual(value['transfer_summary']['speed'], 123456)
        self.assertEqual(value['transfer_summary']['transferring'], 1)
        full = self.client.get('/api/jobs/workbench?poll=1', headers={'X-Instance-ID': 'alpha'}).json
        self.assertEqual([item['id'] for item in full['active_tasks']], [task['id']])

    def test_compact_speed_excludes_stale_samples_and_forwarded_json_is_decodable(self):
        from webapp.workbench import compact_status
        old = dict(status='running', progress=dict(stage='downloading', speed=1000, _reported_at=time.time() - 20))
        value = compact_status(dict(active_tasks=[old]))
        self.assertEqual(value['transfer_summary']['transferring'], 1)
        self.assertEqual(value['transfer_summary']['measured'], 0)
        self.assertEqual(value['transfer_summary']['speed'], 0)
        test_fleet.FleetTests.serve(self, 'alpha')
        import gzip
        for encoding in ('gzip', 'identity'):
            response = self.client.get('/api/status', headers={'X-Instance-ID': 'alpha', 'Accept-Encoding': encoding})
            self.assertEqual(response.status_code, 200)
            payload = gzip.decompress(response.data) if encoding == 'gzip' else response.data
            self.assertIn('tools', json.loads(payload))
            if encoding == 'identity':
                self.assertNotIn('Content-Encoding', response.headers)

    def test_instance_poll_includes_fresh_console_startup_outside_snapshot(self):
        from webapp.display_cache import display_cache
        display_cache(self.apps['manager']).cache.ttl = 60
        manager = self.apps['manager'].extensions['download_startup']
        worker = self.apps['alpha'].extensions['download_startup']
        manager.value = {'state': 'running', 'steps': []}
        worker.value = {'state': 'failed', 'steps': []}
        first = self.client.get('/api/instances?poll=1', headers={'X-Instance-ID': 'alpha'})
        self.assertEqual(first.json['startup']['state'], 'running')
        manager.value['state'] = 'succeeded'
        second = self.client.get('/api/instances?poll=1', headers={'X-Instance-ID': 'alpha'})
        self.assertEqual(second.json['startup']['state'], 'succeeded')

    def test_poll_snapshot_is_scoped_and_control_invalidates_it(self):
        from webapp.workbench import Workbench
        from webapp.display_cache import display_cache
        display_cache(self.apps['manager']).cache.ttl = 60
        original = Workbench.status
        with patch.object(Workbench, 'status', autospec=True, side_effect=original) as load:
            for _ in range(2):
                response = self.client.get('/api/jobs/workbench?poll=1', headers={'X-Instance-ID': 'alpha'})
                self.assertEqual(response.status_code, 200)
            self.assertEqual(load.call_count, 1)
            beta = self.client.get('/api/jobs/workbench?poll=1', headers={'X-Instance-ID': 'beta'})
            self.assertEqual(beta.json['instance_id'], 'beta')
            self.assertEqual(load.call_count, 2)
            self.client.get('/api/jobs/workbench', headers={'X-Instance-ID': 'alpha'})
            self.assertEqual(load.call_count, 3)
            self.control('alpha', 'pause')
            count = load.call_count
            result = self.client.get('/api/jobs/workbench?poll=1', headers={'X-Instance-ID': 'alpha'})
            self.assertTrue(result.json['paused'])
            self.assertEqual(load.call_count, count + 1)

    def test_cookie_labels_use_executor_scope_and_metadata_only(self):
        from tests.account_cookie_fixture import AccountCookieProfiles as CookieProfiles
        from webapp.workbench import Workbench
        content = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture\n'
        profile = CookieProfiles(self.root / 'alpha', self.a).add('Alpha account', content)
        task = {'progress': {'cookie_profile_id': profile['id']}}
        queries = []
        def capture(conn, cursor, sql, parameters, context, many):
            queries.append(sql)
        event.listen(self.manager.db.engine, 'before_cursor_execute', capture)
        try:
            with patch('webapp.cookie_profiles.CookieProfiles.pool_members', side_effect=AssertionError('display must not load credentials')):
                result = Workbench.decorate_cookie_names([task], None, self.manager, scope='alpha')
                self.assertEqual(result[0]['progress']['cookie_profile_name'], 'Alpha account')
                self.assertEqual(len(queries), 1)
                self.assertNotIn('content', queries[0].lower())
                Workbench.decorate_cookie_names(result, None, self.manager, scope='alpha')
                self.assertEqual(len(queries), 1, 'already labelled tasks need no SQL')
                self.assertNotIn('cookie_profile_name', task['progress'])
        finally:
            event.remove(self.manager.db.engine, 'before_cursor_execute', capture)

    def control(self, instance, action, job=None, expected_status=200):
        with patch('webapp.fleet_routes.rpc', return_value={'ready': True}) as rpc:
            response = self.client.post('/api/jobs/workbench', json={'action': action, 'job_id': job},
                headers={**self.headers, 'X-Instance-ID': instance})
        self.assertEqual(response.status_code, expected_status, response.json)
        if action == 'start' and expected_status == 200:
            self.assertEqual(rpc.call_args.args[1], instance)
        return response.json

    def status(self, instance):
        return self.client.get('/api/jobs/workbench', headers={'X-Instance-ID': instance}).json

    def pair(self):
        a = self.create(urls='npNnbj46_ik\n2Gu7q-KUXCk\nWMu6ANTg8mk')
        b = self.create('List B', urls='aaaaaaaaaaa\nbbbbbbbbbbb\nccccccccccc')
        self.control('alpha', 'start', a)
        self.control('beta', 'start', b)
        return a, b

    def test_output_speed_uses_current_instance_session_and_retains_deleted_test_sizes(self):
        self.pair()
        task = self.a.claim(self.a.owner)
        identity = task['payload']['download_session_id']
        now = time.time()
        with self.manager.connect() as db:
            db.execute(download_sessions.update().where(download_sessions.c.id == identity).values(
                started_at=datetime.fromtimestamp(now - 20, timezone.utc).isoformat()))
        for name, size, testing in [('billable.mp4', 1_000_000, False), ('test.mp4', 2_000_000, True)]:
            path = self.root / name
            path.write_bytes(b'x' * size)
            with patch('webapp.statistics.stamp', return_value=datetime.fromtimestamp(now - 1, timezone.utc).isoformat()):
                self.assertTrue(self.a.statistics.record_file(task['id'], path, test_mode=testing))
            if testing:
                path.unlink()
        with patch('webapp.fleet.database_time', return_value=now):
            alpha = self.status('alpha')['session']
            beta = self.status('beta')['session']
        self.assertEqual(alpha['id'], identity)
        self.assertEqual(alpha['elapsed_seconds'], 20)
        self.assertEqual(alpha['file_bytes_exact'], '1000000')
        self.assertEqual(alpha['test_file_bytes_exact'], '2000000')
        self.assertEqual(alpha['downloaded_file_bytes_exact'], '3000000')
        self.assertEqual(alpha['output_mbps'], 1.2)
        self.assertEqual(alpha['output_recent_mbps'], 1.2)
        self.assertEqual(alpha['output_recent_file_bytes_exact'], '3000000')
        self.assertNotEqual(beta['id'], identity)
        self.assertEqual(beta['downloaded_file_bytes'], 0)
        self.assertEqual(beta['output_mbps'], 0)
        self.assertEqual(beta['output_recent_mbps'], 0)
        # Previewing another job must not switch the executor's session totals.
        preview = self.client.get('/api/jobs/workbench?job_id=' + self.status('beta')['job']['id'],
            headers={'X-Instance-ID': 'alpha'}).json
        self.assertEqual(preview['session']['id'], identity)

    def test_output_speed_waits_for_elapsed_time_and_resets_with_new_session(self):
        self.control('alpha', 'pause')
        self.assertIsNone(self.status('alpha')['session'])
        job = self.create()
        self.control('alpha', 'start', job)
        first = self.status('alpha')['session']['id']
        now = datetime.now(timezone.utc).timestamp()
        with self.manager.connect() as db:
            db.execute(download_sessions.update().where(download_sessions.c.id == first).values(
                started_at=datetime.fromtimestamp(now, timezone.utc).isoformat(), file_bytes=1_000_000))
        with patch('webapp.fleet.database_time', return_value=now):
            self.assertIsNone(self.status('alpha')['session']['output_mbps'])
        with patch('webapp.fleet.database_time', return_value=now + 10):
            self.assertEqual(self.status('alpha')['session']['output_mbps'], .8)
        self.control('alpha', 'pause')
        self.assertIsNone(self.status('alpha')['session'])
        self.control('alpha', 'start', job)
        current = self.status('alpha')['session']
        self.assertNotEqual(current['id'], first)
        self.assertEqual(current['downloaded_file_bytes'], 0)

    def test_output_speed_keeps_stopping_session_until_inflight_download_finishes(self):
        job = self.create()
        self.control('alpha', 'start', job)
        task = self.a.claim(self.a.owner)
        self.control('alpha', 'pause')
        self.assertEqual(self.status('alpha')['session']['status'], 'stopping')
        self.a.finish(task['id'], result={}, worker_id=self.a.owner)
        self.assertIsNone(self.status('alpha')['session'])

    def assert_job_state(self, job, state, desired, reason=''):
        detail = self.client.get('/api/jobs/' + job).json
        page = self.client.get('/api/jobs').json['items']
        item = next(item for item in page if item['id'] == job)
        for value in (detail, item):
            self.assertEqual(value['state'], state, value)
            self.assertEqual(value['desired_running'], desired, value)
            self.assertEqual(value['waiting_reason'], reason, value)
        return detail

    def test_job_states_follow_scoped_execution_and_pause_drains(self):
        job = self.create(urls='npNnbj46_ik\n2Gu7q-KUXCk')
        self.manager.set_downloads_paused(True)
        self.control('alpha', 'start', job)
        self.assertFalse(self.assert_job_state(job, 'queued', True)['released'])
        task = self.a.claim('alpha-owner')
        self.assert_job_state(job, 'running', True)
        self.assertEqual(self.status('alpha')['job']['state'], 'running')
        self.control('alpha', 'pause')
        self.assert_job_state(job, 'draining', False)
        self.assertEqual(self.status('alpha')['job']['state'], 'draining')
        self.assertTrue(self.a.finish(task['id'], result={}, worker_id='alpha-owner'))
        self.assert_job_state(job, 'paused', False)
        self.assertEqual(self.status('alpha')['job']['state'], 'paused')

    def test_paused_peer_does_not_hide_other_instances_running_same_job(self):
        job = self.create(urls='npNnbj46_ik\n2Gu7q-KUXCk\nWMu6ANTg8mk')
        self.control('alpha', 'start', job)
        self.control('beta', 'start', job)
        self.a.claim('alpha-owner')
        self.b.claim('beta-owner')
        self.control('alpha', 'pause')
        self.assert_job_state(job, 'running', True)
        self.assertEqual(self.status('alpha')['job']['state'], 'draining')
        self.assertEqual(self.status('beta')['job']['state'], 'running')
        self.control('beta', 'pause')
        self.assert_job_state(job, 'draining', False)

    def test_wait_conditions_only_consider_instances_assigned_to_this_job(self):
        job, other = self.pair()
        with self.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(heartbeat=time.time()-100))
        result = self.client.get('/api/jobs/' + job).json
        self.assertTrue(result['waiting_reason'])
        self.assert_job_state(job, 'waiting', True, result['waiting_reason'])
        self.assert_job_state(other, 'queued', True)
        with self.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(heartbeat=time.time()))
            db.execute(self.manager.db.upsert(settings, {'key': 'disk:alpha',
                'value': json.dumps({'blocked': True, 'time': time.time()})}, ['key']))
        self.assert_job_state(job, 'waiting', True, '等待下载实例释放磁盘空间')
        self.assert_job_state(other, 'queued', True)
        self.assertEqual(self.status('alpha')['job']['state'], 'waiting')
        self.assertEqual(self.status('alpha')['job']['waiting_reason'], '等待下载实例释放磁盘空间')
        self.control('beta', 'start', job)
        self.assert_job_state(job, 'queued', True)

    def test_legacy_executor_can_continue_scoped_peers_paused_job(self):
        job = self.create(urls='npNnbj46_ik\n2Gu7q-KUXCk')
        self.control('alpha', 'start', job)
        self.a.claim('alpha-owner')
        self.control('alpha', 'pause')
        with self.manager.connect() as db:
            self.manager.fleet.activate(db)
            db.execute(download_lists.update().where(download_lists.c.id == job).values(released=1))
        self.assert_job_state(job, 'running', True)
        self.assertEqual(self.b.claim('beta-owner')['batch_id'], job)

    def test_stopped_executor_does_not_look_enabled(self):
        job = self.create()
        self.control('alpha', 'start', job)
        with self.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(paused=0, stop_requested=1))
        self.assert_job_state(job, 'paused', False)

    def test_instance_snapshot_queries_do_not_grow_with_job_count(self):
        job = self.create('Unique')
        self.control('alpha', 'start', job)
        for index in range(19):
            self.create('Other ' + str(index))
        def measured(url):
            statements = []
            def capture(connection, cursor, statement, *args):
                statements.append(statement)
            event.listen(self.manager.db.engine, 'before_cursor_execute', capture)
            try:
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, response.json)
                return len(statements), response.json['items']
            finally:
                event.remove(self.manager.db.engine, 'before_cursor_execute', capture)
        one, _ = measured('/api/jobs?q=Unique')
        many, items = measured('/api/jobs')
        self.assertEqual(len(items), 20)
        self.assertEqual(many, one)
        self.assertLessEqual(many, 14)

    def test_start_does_not_enable_other_instance_or_release_shared_job(self):
        a = self.create()
        self.manager.set_downloads_paused(True)
        before = dict(self.b.fleet.row())
        result = self.control('alpha', 'start', a)
        self.assertEqual(result['selected_job_id'], a)
        self.assertTrue(self.manager.downloads_paused())
        self.assertEqual(dict(self.b.fleet.row()), before)
        self.assertIsNone(self.b.claim('beta-owner'))
        self.assertEqual(self.a.claim('alpha-owner')['batch_id'], a)
        self.assertFalse(self.client.get('/api/jobs/' + a).json['released'])

    def test_start_does_not_grant_global_permission_to_a_legacy_peer(self):
        self.manager.create('download', {'urls':['https://youtu.be/aaaaaaaaaaa']})
        a = self.create()
        self.control('alpha', 'start', a)
        self.assertIsNotNone(self.a.claim('alpha-owner'))
        self.assertIsNone(self.b.claim('beta-owner'))
        self.assertFalse(self.b.fleet.row()['paused'])
        self.assertIsNone(self.manager.fleet.policy(active=True))

    def test_legacy_list_is_adopted_before_scoped_execution(self):
        a = self.create()
        with self.manager.connect() as db:
            db.execute(job_profiles.delete().where(job_profiles.c.id == a))
        self.control('alpha', 'start', a)
        self.assertEqual(self.a.claim('alpha-owner')['batch_id'], a)

    def test_selected_job_can_start_while_its_video_file_is_importing(self):
        self.store = self.manager
        a = self.create(urls='')
        imported = test_jobs.JobTests.import_file(self, a)
        self.control('alpha', 'start', a)
        self.assertIsNone(self.a.claim('alpha-owner'))
        test_jobs.run_import(self.manager, imported['payload'], lambda value: None)
        self.assertEqual(self.a.claim('alpha-owner')['batch_id'], a)
        self.assertIsNone(self.b.claim('beta-owner'))

    def test_selections_and_active_video_lists_are_persisted_per_instance(self):
        a, b = self.pair()
        first = self.a.claim('alpha-owner')
        second = self.b.claim('beta-owner')
        self.assertEqual((first['batch_id'], second['batch_id']), (a, b))
        for instance, job, task in [('alpha', a, first), ('beta', b, second)]:
            value = self.client.get('/api/jobs/workbench', headers={'X-Instance-ID': instance}).json
            self.assertEqual(value['selected_job_id'], job)
            self.assertEqual(value['job']['running'], 1)
            self.assertEqual([item['id'] for item in value['active_tasks']], [task['id']])
        cards = {row['id']: row for row in self.client.get('/api/instances').json['items']}
        self.assertEqual(cards['alpha']['selected_job']['id'], a)
        self.assertEqual(cards['beta']['selected_job']['id'], b)

    def test_download_worker_only_sees_its_selected_batch(self):
        a, b = self.pair()
        self.assertEqual(self.a.runnable_counts().get('download'), 3)
        self.assertEqual(self.b.runnable_counts().get('download'), 3)
        self.assertEqual(self.a.claim('alpha-regular', exclude=('import', 'index'))['batch_id'], a)
        self.assertEqual(self.a.claim('alpha-owner', actions=('download',))['batch_id'], a)
        self.assertEqual(self.b.claim('beta-owner', actions=('download',))['batch_id'], b)

    def test_switch_only_revokes_current_instances_old_job(self):
        a, b = self.pair()
        first, second = self.a.claim('alpha-owner'), self.b.claim('beta-owner')
        self.control('alpha', 'start', b)
        self.assertFalse(self.a.heartbeat(first['id'], 'alpha-owner'))
        self.assertTrue(self.b.heartbeat(second['id'], 'beta-owner'))
        self.assertEqual(self.a.claim('alpha-new')['batch_id'], b)
        self.assertEqual(self.b.claim('beta-new')['batch_id'], b)
        self.assertEqual(self.status('beta')['selected_job_id'], b)

    def test_pause_drains_current_video_and_leaves_peer_running(self):
        self.pair()
        first, second = self.a.claim('alpha-owner'), self.b.claim('beta-owner')
        self.control('alpha', 'pause')
        self.assertIsNone(self.a.claim('alpha-next'))
        self.assertTrue(self.a.heartbeat(first['id'], 'alpha-owner'))
        self.assertFalse(self.a.downloads_stopped())
        self.assertTrue(self.b.heartbeat(second['id'], 'beta-owner'))
        self.assertIsNotNone(self.b.claim('beta-next'))
        self.assertTrue(self.a.finish(first['id'], result={}, worker_id='alpha-owner'))
        self.assertTrue(self.status('alpha')['paused'])
        self.assertFalse(self.status('beta')['paused'])

    def test_stop_revokes_only_current_instance_and_resume_uses_same_selection(self):
        a, b = self.pair()
        first, second = self.a.claim('alpha-owner'), self.b.claim('beta-owner')
        self.control('alpha', 'stop')
        self.assertFalse(self.a.heartbeat(first['id'], 'alpha-owner'))
        self.assertFalse(self.a.finish(first['id'], result={}, worker_id='alpha-owner'))
        self.assertTrue(self.a.downloads_stopped())
        self.assertTrue(self.b.heartbeat(second['id'], 'beta-owner'))
        self.assertIsNotNone(self.b.claim('beta-next'))
        self.assertEqual(self.status('alpha')['selected_job_id'], a)
        with patch('webapp.fleet_routes.rpc'):
            response = self.client.put('/api/instances/alpha', json={'action': 'resume'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.a.claim('alpha-new')['batch_id'], a)

    def test_preflight_failure_or_policy_change_does_not_touch_peers(self):
        a, b = self.pair()
        first = self.a.claim('alpha-owner')
        with patch('webapp.fleet_routes.rpc', side_effect=ValueError('environment unavailable')):
            response = self.client.post('/api/jobs/workbench', json={'action':'start', 'job_id':a},
                headers={**self.headers, 'X-Instance-ID':'beta'})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.a.heartbeat(first['id'], 'alpha-owner'))
        self.assertEqual(self.status('beta')['selected_job_id'], b)
        with patch('webapp.fleet_routes.rpc', side_effect=lambda *args, **kwargs:
                   self.b.fleet.control('beta', {'action':'stop'})):
            response = self.client.post('/api/jobs/workbench', json={'action':'start','job_id':a},
                headers={**self.headers, 'X-Instance-ID':'beta'})
        self.assertEqual(response.status_code, 409, response.json)
        self.assertTrue(self.status('beta')['paused'])
        self.assertTrue(self.a.heartbeat(first['id'], 'alpha-owner'))

    def test_shared_cap_and_duplicate_claim_protection_still_apply(self):
        a = self.create(urls='npNnbj46_ik\n2Gu7q-KUXCk\nWMu6ANTg8mk')
        for node in ('alpha', 'beta'):
            self.control(node, 'start', a)
        with self.manager.connect() as db:
            db.execute(settings.update().where(settings.c.key == 'fleet_budget').values(value='2'))
        first, second = self.a.claim('alpha-owner'), self.b.claim('beta-owner')
        self.assertNotEqual(first['id'], second['id'])
        self.assertIsNone(self.a.claim('extra-alpha'))
        self.assertIsNone(self.b.claim('extra-beta'))

    def test_task_management_pause_archive_and_clear_block_scoped_claims(self):
        a, b = self.pair()
        for action in ('pause', 'archive'):
            response = self.client.post(f'/api/jobs/{a}/control', json={'action':action}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertIsNone(self.a.claim('alpha-owner'))
            self.assertFalse(self.status('beta')['paused'])
            if action == 'pause':
                self.control('alpha', 'start', a)
        self.control('alpha', 'start', a, expected_status=400)
        self.assertEqual(self.b.claim('beta-owner')['batch_id'], b)

    def test_delete_cancels_pending_videos_and_unbinds_only_affected_instances(self):
        a, b = self.pair()
        first, second = self.a.claim('alpha-owner'), self.b.claim('beta-owner')
        response = self.client.post(f'/api/jobs/{a}/control', json={
            'action': 'delete', 'confirmation': 'List A'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        alpha, beta = self.status('alpha'), self.status('beta')
        self.assertIsNone(alpha['selected_job_id'])
        self.assertIsNone(alpha['job'])
        self.assertEqual(alpha['active_tasks'], [])
        self.assertTrue(alpha['paused'])
        self.assertEqual(beta['selected_job_id'], b)
        self.assertFalse(beta['paused'])
        self.assertFalse(self.a.heartbeat(first['id'], 'alpha-owner'))
        self.assertFalse(self.a.finish(first['id'], result={}, worker_id='alpha-owner'))
        self.assertTrue(self.b.heartbeat(second['id'], 'beta-owner'))
        self.assertEqual(self.manager.counts(batch_id=a).get('cancelled'), 3)
        self.assertEqual(self.manager.download_queue()['queued'], 2)
        with self.manager.connect() as db:
            self.assertIsNone(db.execute(select(settings.c.value).where(
                settings.c.key == 'workbench_job:alpha')).scalar_one_or_none())
            self.assertEqual(db.execute(select(settings.c.value).where(
                settings.c.key == 'workbench_job:beta')).scalar_one(), b)

    def test_old_deleted_selection_is_hidden_from_workbench_and_instance_cards(self):
        a, b = self.pair()
        # Simulate a job deleted by the earlier version, with its selection left behind.
        with self.manager.connect() as db:
            db.execute(job_profiles.update().where(job_profiles.c.id == a).values(deleted=1))
            db.execute(download_lists.update().where(download_lists.c.id == a).values(released=0))
            db.execute(instances.update().where(instances.c.id == 'alpha').values(paused=1))
        for suffix in ('', '?job_id=' + a, '?compact=1&job_id=' + a):
            response = self.client.get('/api/jobs/workbench' + suffix, headers={'X-Instance-ID': 'alpha'})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertIsNone(response.json['selected_job_id'])
            self.assertIsNone(response.json['job'])
            self.assertEqual(response.json['queued'], 0)
        cards = {row['id']: row for row in self.client.get('/api/instances').json['items']}
        self.assertIsNone(cards['alpha']['selected_job'])
        self.assertEqual(cards['beta']['selected_job']['id'], b)
        self.assertEqual(self.manager.download_queue()['queued'], 3)
        self.assertEqual(self.manager.download_queue()['waiting_lists'], 1)

    def test_offline_or_unknown_instance_cannot_start(self):
        a = self.create()
        with self.manager.connect() as db:
            db.execute(instances.update().where(instances.c.id == 'alpha').values(heartbeat=time.time()-100))
        self.control('alpha', 'start', a, expected_status=400)
        self.control('missing', 'start', a, expected_status=400)

    def test_media_clear_prevents_start_and_all_stop_includes_scoped_instances(self):
        a, b = self.pair()
        self.manager.set_downloads_paused(True)
        self.assertTrue(self.status('alpha')['paused'])
        self.assertTrue(self.status('beta')['paused'])
        self.manager.create('clear_media', {})
        self.control('alpha', 'start', a, expected_status=409)
        self.assertIsNone(self.a.claim('alpha-owner'))


class StandaloneWorkbenchTests(unittest.TestCase):
    setUp = test_jobs.JobTests.setUp
    create = test_jobs.JobTests.create

    def test_delete_last_job_clears_workbench_and_preserves_delivered_files_and_ledger(self):
        job = self.create(urls='npNnbj46_ik\n2Gu7q-KUXCk\nWMu6ANTg8mk', price='2')
        with patch('app.require_downmp3_environment'):
            response = self.client.post('/api/jobs/workbench', json={
                'action': 'start', 'job_id': job}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        completed = self.store.claim(self.store.owner)
        delivered = self.root / 'delivered.mp4'
        delivered.write_bytes(b'x' * 100)
        self.assertTrue(self.store.statistics.record_file(completed['id'], delivered))
        self.assertTrue(self.store.finish(completed['id'], result={}, worker_id=self.store.owner))
        running = self.store.claim('running-owner')
        response = self.client.post(f'/api/jobs/{job}/control', json={
            'action': 'delete', 'confirmation': 'List A'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.client.get('/api/jobs').json['items'], [])
        for suffix in ('', '?job_id=' + job, '?compact=1&job_id=' + job):
            value = self.client.get('/api/jobs/workbench' + suffix).json
            self.assertIsNone(value['selected_job_id'])
            self.assertIsNone(value['job'])
            self.assertTrue(value['paused'])
            self.assertEqual((value['queued'], value['running']), (0, 0))
        self.assertEqual(self.store.download_queue()['queued'], 0)
        self.assertEqual(self.store.download_queue()['waiting_lists'], 0)
        self.assertEqual(self.store.counts(batch_id=job).get('cancelled'), 2)
        self.assertFalse(self.store.heartbeat(running['id'], 'running-owner'))
        self.assertFalse(self.store.finish(running['id'], result={}, worker_id='running-owner'))
        self.assertEqual(self.store.get(completed['id'])['status'], 'succeeded')
        from webapp.jobs import Jobs
        self.assertEqual(Jobs(self.store).detail(job)['file_bytes_exact'], '100')
        self.assertEqual(delivered.read_bytes(), b'x' * 100)
        # Existing installations may still have a deleted selection in settings.
        with self.store.connect() as db:
            db.execute(self.store.db.upsert(settings,
                {'key': 'selected_download_job', 'value': job}, ['key']))
        value = self.client.get('/api/jobs/workbench?job_id=' + job).json
        self.assertIsNone(value['selected_job_id'])
        self.assertIsNone(value['job'])

    def test_start_pause_stop_and_persisted_selection(self):
        job = self.create(urls='npNnbj46_ik\n2Gu7q-KUXCk')
        with patch('app.require_downmp3_environment'):
            response = self.client.post('/api/jobs/workbench', json={'action':'start','job_id':job}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        first = self.store.claim('owner')
        self.assertEqual(first['batch_id'], job)
        session = self.client.get('/api/jobs/workbench').json['session']
        self.assertEqual(session['id'], first['payload']['download_session_id'])
        self.assertEqual(session['downloaded_file_bytes'], 0)
        response = self.client.post('/api/jobs/workbench', json={'action':'pause'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNone(self.store.claim('next'))
        self.assertTrue(self.store.heartbeat(first['id'], 'owner'))
        response = self.client.post('/api/jobs/workbench', json={'action':'stop'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(self.store.heartbeat(first['id'], 'owner'))
        self.assertEqual(self.client.get('/api/jobs/workbench').json['selected_job_id'], job)


if __name__ == '__main__':
    unittest.main()
