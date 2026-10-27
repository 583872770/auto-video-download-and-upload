"""Business invariants across compatibility APIs, sessions and multiple nodes."""
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from sqlalchemy import event
from werkzeug.exceptions import Conflict

from tests import test_jobs, test_scale, test_fleet
from tests.test_mysql import MySQLFixture
from webapp.database import schema_migrations
from webapp.jobs import Jobs


class RedesignContract:
    create = test_jobs.JobTests.create
    detail = test_jobs.JobTests.detail
    control = test_jobs.JobTests.control

    def save(self, identity, **data):
        return self.client.put('/api/jobs/' + identity, json=data, headers=self.headers)

    def test_one_estimate_for_all_pages_free_and_pending_prices_remain_distinct(self):
        a, b = self.create(price='100'), self.create('List B', price='200')
        free, pending = self.create('Free', price='0'), self.create('Pending')
        for identity in (a, b):
            self.control(identity, 'start')
            task = self.store.claim(self.store.owner)
            path = self.root / (identity + '.mp4')
            path.write_bytes(b'x' * 1000000)
            self.store.statistics.record_file(task['id'], path)
            self.store.finish(task['id'], result={}, worker_id=self.store.owner)
        self.assertEqual([self.detail(key)['amount'] for key in (a, b, free, pending)], ['0.10', '0.20', '0.00', None])
        summary = self.client.get('/api/jobs/summary').json
        stats = self.client.get('/api/statistics').json
        self.assertEqual((summary['amount'], stats['summary']['amount'], stats['billing']['amount']), ('0.30',)*3)
        self.assertEqual(summary['unset'], 1)
        self.assertTrue(all(row['amount'] is None for row in stats['items']))
        filtered = self.client.get('/api/statistics?q=not-a-session').json
        self.assertEqual(filtered['items'], [])
        self.assertEqual(filtered['billing']['amount'], '0.30')
        self.control(a, 'archive')
        self.control(b, 'delete')
        self.assertEqual(self.client.get('/api/jobs/summary').json['amount'], '0.30')
        self.assertEqual(self.client.get('/api/statistics').json['summary']['amount'], '0.30')
        rows = {row['id']: row for row in self.client.get('/api/statistics/lists').json['items']}
        self.assertEqual((rows[free]['amount'], rows[pending]['amount']), ('0.00', None))

    def test_legacy_writes_cannot_bypass_job_rules_or_price_state(self):
        identity = self.create()
        before = self.detail(identity)
        response = self.client.put('/api/statistics/lists/' + identity,
            json=dict(customer='', name='', price='9'), headers=self.headers)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(self.detail(identity), before)
        response = self.save(identity, price='9', edit_version=before['edit_version'])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['price_set'])
        self.control(identity, 'archive')
        self.assertEqual(self.save(identity, customer='', edit_version=response.json['edit_version']).status_code, 400)
        self.assertEqual(self.client.put('/api/statistics/lists/' + identity,
            json=dict(customer='', price='1'), headers=self.headers).status_code, 410)

    def test_stale_forms_preserve_newer_price_and_partial_updates_preserve_notes(self):
        identity = self.create(price='1', note='keep this note')
        version = self.detail(identity)['edit_version']
        response = self.save(identity, price='19', edit_version=version)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['note'], 'keep this note')
        self.assertEqual(self.save(identity, name='Stale label', price='1', edit_version=version).status_code, 409)
        self.assertEqual(self.save(identity, price='1').status_code, 428)
        self.assertEqual(self.detail(identity)['price'], '19')
        self.assertEqual(self.detail(identity)['name'], 'List A')
        self.assertEqual(self.detail(identity)['revision'], 1)  # Editing cannot invalidate an import checkpoint.
        self.assertEqual(self.detail(identity)['policy_version'], 1)
        fresh = self.detail(identity)['edit_version']
        self.control(identity, 'archive')
        self.control(identity, 'unarchive')
        self.assertEqual(self.save(identity, price='1', edit_version=fresh).status_code, 409)

    def test_simultaneous_edits_have_one_winner(self):
        from Utils.config import load_config
        identity = self.create(price='1')
        version = self.detail(identity)['edit_version']
        config, _ = load_config(self.config)
        def save(price):
            try:
                Jobs(self.store).update(identity, dict(price=price, edit_version=version), config)
                return 200
            except Conflict:
                return 409
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ('19', '23')))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(self.detail(identity)['edit_version'], version+1)

    def test_job_list_queries_are_bounded_and_match_detail(self):
        identity = self.create('Unique')
        for index in range(19):
            self.create('Other ' + str(index))
        def measured(url):
            statements = []
            def capture(connection, cursor, statement, *args):
                statements.append(statement)
            event.listen(self.store.db.engine, 'before_cursor_execute', capture)
            try:
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, response.json)
                return len(statements), response.json['items']
            finally:
                event.remove(self.store.db.engine, 'before_cursor_execute', capture)
        one, _ = measured('/api/jobs?q=Unique')
        many, rows = measured('/api/jobs')
        self.assertEqual(len(rows), 20)
        self.assertEqual(many, one)
        self.assertLessEqual(many, 12)
        detail = self.detail(identity)
        row = next(row for row in rows if row['id'] == identity)
        self.assertEqual({key: detail[key] for key in row}, row)
        self.assertNotIn('running_videos', row)

    def test_resume_aliases_preserve_job_intent_and_obey_stop_and_clear(self):
        started, fresh = self.create(), self.create('Fresh')
        self.control(started, 'start')
        self.store.set_downloads_paused(True)
        self.assertEqual(self.detail(started)['state'], 'waiting')
        for route in ('/api/jobs/system/resume', '/api/downloads/resume'):
            with patch('app.require_downmp3_environment'):
                response = self.client.post(route, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertTrue(self.detail(started)['desired_running'])
            self.assertFalse(self.detail(fresh)['desired_running'])
            self.store.set_downloads_paused(True)
        self.store.create('clear_media', {})
        for route in ('/api/jobs/system/resume', '/api/downloads/resume'):
            with patch('app.require_downmp3_environment'):
                self.assertEqual(self.client.post(route, headers=self.headers).status_code, 409)
        self.assertTrue(self.store.downloads_paused())

    def test_resume_explains_jobs_that_have_not_started(self):
        identity = self.create()
        self.store.set_downloads_paused(True)
        response = self.client.post('/api/jobs/system/resume', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['execution']['paused'])
        self.assertEqual(response.json['execution']['released_queued'], 0)
        self.assertIn('尚未开始', response.json['message'])
        self.assertFalse(self.detail(identity)['desired_running'])
        status = self.client.get('/api/jobs/system/status').json
        self.assertEqual(status['blockers'][0]['code'], 'jobs_not_started')
        self.assertEqual(self.client.get('/api/status').json['execution'], status)

    def test_additive_upgrade_preserves_existing_profiles_and_is_repeatable(self):
        identity = self.create(note='historical note', price='7')
        with self.store.connect() as db:
            db.execute(schema_migrations.delete())
            db.exec_driver_sql('ALTER TABLE job_profiles DROP COLUMN edit_version')
            db.exec_driver_sql('ALTER TABLE job_profiles DROP COLUMN policy_version')
        self.store.db.initialize()
        self.store.db.initialize()
        detail = self.detail(identity)
        self.assertEqual((detail['note'], detail['price'], detail['edit_version'], detail['policy_version']),
            ('historical note', '7', 1, 1))


class SQLiteRedesignTests(RedesignContract, unittest.TestCase):
    setUp = test_jobs.JobTests.setUp


class MySQLRedesignTests(MySQLFixture, RedesignContract, unittest.TestCase):
    def setUp(self):
        MySQLFixture.setUp(self)
        response = self.client.put('/api/config',json={'download_methods':['downmp3']},headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)


class NodeIsolationTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet
    create = test_jobs.JobTests.create
    detail = test_jobs.JobTests.detail

    def test_resume_reports_paused_executors_without_enabling_them(self):
        for store in (self.a, self.b):
            store.fleet.control(store.fleet.id, {'action': 'drain'})
        self.manager.set_downloads_paused(True)
        for route in ('/api/jobs/system/resume', '/api/downloads/resume'):
            response = self.client.post(route, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.json)
            execution = response.json['execution']
            paused = next(item for item in execution['blockers'] if item['code'] == 'instances_paused')
            self.assertIn('2 个在线下载实例', paused['message'])  # Online manager is not an executor.
            self.assertEqual(paused['href'], '/#instances')
            self.assertIn('单独恢复', response.json['message'])
            self.assertTrue(self.a.fleet.row()['paused'])
            self.assertTrue(self.b.fleet.row()['paused'])
        self.a.fleet.control('alpha', {'action': 'resume'})
        status = self.client.get('/api/jobs/system/status').json
        self.assertNotIn('instances_paused', [item['code'] for item in status['blockers']])

    def test_start_uses_healthy_node_and_keeps_failed_node_out(self):
        identity = self.create()
        def check(fleet, target, path, **kwargs):
            if target == 'alpha':
                raise ValueError('fixture cookie unavailable')
            return {'ready': True}
        with patch('webapp.fleet_routes.rpc', side_effect=check):
            response = self.client.post(f'/api/jobs/{identity}/control', json={'action':'start'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['warnings'])
        self.assertTrue(self.detail(identity)['desired_running'])
        self.assertIsNone(self.a.claim(self.a.owner))
        self.assertEqual(self.b.claim(self.b.owner)['batch_id'], identity)

    def test_executor_failure_is_local_and_lease_can_move_to_healthy_node(self):
        from webapp.execution import isolate_download_failure
        identity = self.create()
        with patch('webapp.fleet_routes.rpc', return_value={'ready':True}):
            self.client.post(f'/api/jobs/{identity}/control', json={'action':'start'}, headers=self.headers)
        task = self.a.claim(self.a.owner)
        isolate_download_failure(self.a, 'fixture missing tool')
        self.a.release(task['id'], self.a.owner, 'fixture missing tool', delay=0)
        self.assertTrue(self.detail(identity)['desired_running'])
        self.assertFalse(self.manager.downloads_paused())
        self.assertEqual(self.b.claim(self.b.owner)['id'], task['id'])
        self.assertIsNone(self.a.claim(self.a.owner))

    def test_no_nodes_retains_running_intent_and_explains_wait(self):
        identity = self.create()
        self.a.fleet.control('alpha', {'action':'drain'})
        self.b.fleet.control('beta', {'action':'drain'})
        response = self.client.post(f'/api/jobs/{identity}/control', json={'action':'start'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['job']['state'], 'waiting')
        self.assertTrue(response.json['job']['waiting_reason'])
        self.assertTrue(response.json['job']['desired_running'])

    def test_stale_health_result_cannot_pause_a_new_boot(self):
        self.a.fleet.quarantine('alpha', 'old environment failure', boot_id='older-boot-generation')
        self.assertFalse(self.a.fleet.row()['paused'])

    def test_node_token_cannot_open_business_or_account_apis(self):
        client = self.apps['alpha'].test_client()
        headers = {'Authorization':'Bearer '+self.a.fleet.token}
        self.assertEqual(client.get('/api/jobs', headers=headers).status_code, 403)
        self.assertEqual(client.get('/api/auth/users', headers=headers).status_code, 403)
        self.assertEqual(client.get('/api/config', headers=headers).status_code, 200)
