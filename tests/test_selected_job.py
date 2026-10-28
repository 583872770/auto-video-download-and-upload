"""Starting a selected job restores permission without reviving other jobs."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import test_fleet, test_jobs, test_scale


class SelectedJobTests(unittest.TestCase):
    setUp = test_jobs.JobTests.setUp
    create = test_jobs.JobTests.create
    control = test_jobs.JobTests.control
    detail = test_jobs.JobTests.detail

    def start(self, identity, **extra):
        return self.client.post(f'/api/jobs/{identity}/control', json={
            'action': 'start', 'resume_system': True, **extra}, headers=self.headers)

    def test_global_stop_then_selection_only_releases_selected_job(self):
        a, b = self.create(), self.create('List B')
        self.control(a, 'start')
        self.control(b, 'start')
        self.store.set_downloads_paused(True)
        with patch('app.require_downmp3_environment'):
            response = self.start(b)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(self.store.downloads_paused())
        self.assertFalse(self.detail(a)['released'])
        self.assertTrue(self.detail(b)['released'])
        self.assertEqual(self.store.claim(self.store.owner)['batch_id'], b)
        self.assertIsNone(self.store.claim('second-owner'))

    def test_start_while_running_switches_exclusively_to_selected_job(self):
        a, b = self.create(), self.create('List B')
        self.control(a, 'start')
        task = self.store.claim(self.store.owner)
        with patch('app.require_downmp3_environment'):
            response = self.start(b)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(self.detail(a)['released'])
        self.assertFalse(self.store.heartbeat(task['id'], self.store.owner))
        self.assertEqual(self.store.claim('second-owner')['batch_id'], b)
        self.assertIsNone(self.store.claim('third-owner'))

    def test_selection_survives_reload_and_stop(self):
        identity = self.create()
        with patch('app.require_downmp3_environment'):
            self.assertEqual(self.start(identity).status_code, 200)
        self.control(identity, 'stop')
        restored = test_scale.create_app(self.config, self.root / 'state', start_worker=False)
        response = restored.test_client().get('/api/jobs/system/status')
        self.assertEqual(response.json['selected_job']['id'], identity)
        self.assertFalse(response.json['selected_job']['released'])
        self.assertIsNone(restored.extensions['task_store'].claim('new-process'))

    def test_other_jobs_and_unassigned_videos_cannot_bypass_selection(self):
        a, b = self.create(), self.create('List B', urls='npNnbj46_ik\n2Gu7q-KUXCk')
        legacy = self.store.create('download', {'urls':['https://youtu.be/WMu6ANTg8mk']})
        running = self.store.claim(self.store.owner)
        self.assertEqual(running['id'], legacy['id'])
        with patch('app.require_downmp3_environment'):
            self.assertEqual(self.start(b).status_code, 200)
        self.assertFalse(self.store.heartbeat(running['id'], self.store.owner))
        self.control(a, 'start')  # Even older clients may not escape the shared scope.
        self.store.activate_download_lists()
        with self.store.connect() as db:
            self.assertEqual(self.store._released_queued_count(db), 2)
        self.assertEqual(self.store.claim('one')['batch_id'], b)
        self.assertEqual(self.store.claim('two')['batch_id'], b)
        self.assertIsNone(self.store.claim('three'))

    def test_failed_switch_preserves_current_job_and_lease(self):
        a, b = self.create(), self.create('List B')
        with patch('app.require_downmp3_environment'):
            self.assertEqual(self.start(a).status_code, 200)
        task = self.store.claim(self.store.owner)
        with patch('app.require_downmp3_environment', side_effect=ValueError('fixture unavailable')):
            self.assertEqual(self.start(b).status_code, 400)
        self.assertEqual(self.store.selected_download_job(), a)
        self.assertTrue(self.store.heartbeat(task['id'], self.store.owner))
        self.assertFalse(self.detail(b)['released'])

    def test_can_select_already_running_job_without_pending_videos(self):
        identity = self.create()
        self.control(identity, 'start')
        task = self.store.claim(self.store.owner)
        with patch('app.require_downmp3_environment'):
            response = self.start(identity)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.store.selected_download_job(), identity)
        self.assertTrue(self.store.heartbeat(task['id'], self.store.owner))

    def test_concurrent_starts_leave_one_shared_job_scope(self):
        from webapp.jobs import Jobs
        identities = [self.create(), self.create('List B')]
        def start(identity):
            Jobs(self.store).control(identity, 'start', {}, {
                'resume_system':True, '_checked_policy_version':1})
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(start, identities))
        selected = self.store.selected_download_job()
        for identity in identities:
            self.assertEqual(self.detail(identity)['released'], identity == selected)
        self.assertEqual(self.store.claim('winner')['batch_id'], selected)
        self.assertIsNone(self.store.claim('loser'))

    def test_waits_for_stopped_processes_before_changing_intent(self):
        a, b = self.create(), self.create('List B')
        self.control(a, 'start')
        self.store.claim(self.store.owner)
        self.store.set_downloads_paused(True)
        with patch('app.require_downmp3_environment') as preflight:
            response = self.start(b)
        self.assertEqual(response.status_code, 409, response.json)
        preflight.assert_not_called()
        self.assertTrue(self.store.downloads_paused())
        self.assertTrue(self.detail(a)['released'])
        self.assertFalse(self.detail(b)['released'])

    def test_selected_policy_is_checked_before_release(self):
        identity = self.create()
        self.store.set_downloads_paused(True)
        with patch('app.require_downmp3_environment', side_effect=ValueError('fixture environment unavailable')) as check:
            response = self.start(identity)
        check.assert_called_once()
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn('fixture environment unavailable', response.json['error'])
        self.assertTrue(self.store.downloads_paused())
        self.assertFalse(self.detail(identity)['released'])

    def test_policy_changed_during_preflight_requires_recheck(self):
        identity = self.create()
        self.store.set_downloads_paused(True)
        def change_policy():
            response = self.client.put('/api/config', json={'downmp3_quality': '720'}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
        with patch('app.require_downmp3_environment', side_effect=change_policy):
            response = self.start(identity)
        self.assertEqual(response.status_code, 409, response.json)
        self.assertTrue(self.store.downloads_paused())
        self.assertFalse(self.detail(identity)['released'])

    def test_rejects_archived_deleted_and_empty_tasks(self):
        archived = self.create()
        deleted = self.create('Deleted')
        empty = self.create('Empty', urls='')
        self.control(archived, 'archive')
        self.control(deleted, 'delete')
        self.store.set_downloads_paused(True)
        with patch('app.require_downmp3_environment') as check:
            for identity, status in [(archived, 400), (deleted, 404), (empty, 400)]:
                response = self.start(identity)
                self.assertEqual(response.status_code, status, response.json)
        check.assert_not_called()
        self.assertTrue(self.store.downloads_paused())

    def test_validates_explicit_resume_option(self):
        identity = self.create()
        for data in [{'resume_system': 'true'}, {'action': 'pause'}]:
            response = self.start(identity, **data)
            self.assertEqual(response.status_code, 400, response.json)
        self.assertFalse(self.detail(identity)['released'])

    def test_stop_requested_during_preflight_is_rechecked_in_transaction(self):
        a, b = self.create(), self.create('List B')
        self.control(a, 'start')
        self.store.claim(self.store.owner)
        with patch('app.require_downmp3_environment', side_effect=lambda: self.store.set_downloads_paused(True)):
            response = self.start(b)
        self.assertEqual(response.status_code, 409, response.json)
        self.assertTrue(self.store.downloads_paused())
        self.assertFalse(self.detail(b)['released'])

    def test_media_clear_started_during_preflight_blocks_release(self):
        identity = self.create()
        self.store.set_downloads_paused(True)
        with patch('app.require_downmp3_environment', side_effect=lambda: self.store.create('clear_media', {})):
            response = self.start(identity)
        self.assertEqual(response.status_code, 400, response.json)
        self.assertTrue(self.store.downloads_paused())
        self.assertFalse(self.detail(identity)['released'])

    def test_can_start_while_selected_job_is_still_importing(self):
        identity = self.create(urls='')
        task = test_jobs.JobTests.import_file(self, identity)
        self.store.set_downloads_paused(True)
        with patch('app.require_downmp3_environment'):
            response = self.start(identity)
        self.assertEqual(response.status_code, 200, response.json)
        test_jobs.run_import(self.store, task['payload'], lambda value: None)
        self.assertTrue(self.detail(identity)['released'])
        self.assertEqual(self.store.claim(self.store.owner)['action'], 'import')
        self.assertEqual(self.store.claim('download-owner')['batch_id'], identity)


class SelectedJobFleetTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet
    create = test_jobs.JobTests.create

    def start(self, identity):
        with patch('webapp.fleet_routes.rpc'):
            response = self.client.post(f'/api/jobs/{identity}/control',
                json={'action':'start', 'resume_system':True}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)

    def test_shared_selection_and_instance_video_progress_match_across_workers(self):
        a, b = self.create(), self.create('List B', urls='npNnbj46_ik\n2Gu7q-KUXCk')
        self.start(a)
        first = self.a.claim('alpha-owner')
        self.assertEqual(first['batch_id'], a)
        self.start(b)
        self.assertFalse(self.a.heartbeat(first['id'], 'alpha-owner'))
        for worker, owner in [(self.a,'alpha-new'), (self.b,'beta-owner')]:
            task = worker.claim(owner)
            self.assertEqual(task['batch_id'], b)
            worker.update_progress(task['id'], {'stage':'downloading','percent':25,'speed':1000}, owner)
        payload = self.client.get('/api/instances').json
        current = self.client.get('/api/jobs/system/status').json['selected_job']
        self.assertEqual(payload['execution']['selected_job']['id'], current['id'])
        self.assertEqual(current['id'], b)
        self.assertEqual(current['running'], 2)
        workers = [item for item in payload['items'] if item['id'] in ('alpha', 'beta')]
        self.assertTrue(all(item['role'] == 'hybrid' for item in payload['items']))
        self.assertEqual(sum(item['running'] for item in workers), current['running'])
        for item in workers:
            self.assertEqual(item['current_tasks'][0]['batch_id'], b)
            self.assertEqual(item['current_tasks'][0]['progress']['percent'], 25)
            self.assertEqual(item['speed'], 1000)
        self.assertIsNone(self.a.claim('other-alpha'))
        self.assertIsNone(self.b.claim('other-beta'))

    def test_resume_instance_checks_its_own_policy(self):
        identity = self.create()
        self.start(identity)
        self.manager.set_downloads_paused(True)
        with patch('webapp.fleet_routes.rpc') as rpc:
            response = self.client.put('/api/instances/alpha', json={'action':'resume'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(rpc.call_args.args[1], 'alpha')
        self.assertEqual(rpc.call_args.kwargs['data'], {})


if __name__ == '__main__':
    unittest.main()
