import hashlib
import json
import tempfile
import time
import unittest

from webapp.concurrency_diagnostics import diagnose, read_context
from webapp.throughput import policy_key


class ConcurrencyDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.config = {'download_method': 'yt_dlp', 'worker_count': 16,
                       'fragment_concurrency': 4, 'download_connection_limit': 32, 'builtin_transfer_limit': 8}
        self.stamp = time.time()

    def show(self, states=None, progress=(), rate=None, **kwargs):
        return diagnose(self.config, 'node', progress, rate, (states or {}, {}, {}), self.stamp, **kwargs)

    def test_manual_workers_do_not_override_channel_and_connection_limits(self):
        value = self.show()
        self.assertEqual((value['requested'], value['transfer_limit'], value['effective_limit']), (16, 16, 8))
        channel = value['channels'][0]
        self.assertEqual(channel['connection_units'], 2)
        self.assertFalse(channel['resolver_separate'])
        self.config['download_connection_limit'] = 16
        self.assertEqual(self.show()['channels'][0]['connection_units'], 1)
        self.assertEqual(self.show()['effective_limit'], 8)

    def test_cooldown_and_recovery_and_expiration(self):
        state = {'upstream:youtube-origin': {'cooldown_until': self.stamp + 30,
                 'recover_until': self.stamp + 90, 'cooldown_status': 429,
                 'cooldown_kind': 'subtitles'}}
        value = self.show(state)
        self.assertEqual(value['effective_limit'], 0)
        self.assertEqual(value['channels'][0]['cooldown_seconds'], 30)
        self.assertEqual(value['channels'][0]['throttle_kind'], 'subtitles')
        self.stamp += 31
        self.assertEqual(self.show(state)['effective_limit'], 1)
        self.stamp += 60
        self.assertEqual(self.show(state)['effective_limit'], 8)

    def test_recent_shared_policy_takes_minimum_stale_policy_is_ignored(self):
        state = {'upstream:youtube-origin': {'policies': {
            'recent': {'time': self.stamp - 10, 'limit': 3},
            'old': {'time': self.stamp - 100, 'limit': 1}}}}
        self.assertEqual(self.show(state)['effective_limit'], 3)
        self.assertEqual(self.show(global_limit=2)['effective_limit'], 2)
        self.assertEqual(self.show(global_limit=16, global_running=15)['effective_limit'], 1)

    def test_auto_snapshot_requires_matching_policy_and_fresh_time(self):
        self.config['throughput_mode'] = 'auto'
        rate = {'policy': policy_key(self.config), 'time': self.stamp, 'transfers': 10,
                'groups': {'youtube-origin': {'limit': 3}}}
        self.assertEqual(self.show(rate=rate)['effective_limit'], 3)
        rate['time'] -= 21
        self.assertFalse(self.show(rate=rate)['sample_fresh'])
        self.assertEqual(self.show(rate=rate)['transfer_limit'], 2)
        rate.update(time=self.stamp, policy=[])
        self.assertEqual(self.show(rate=rate)['transfer_limit'], 2)

    def test_pool_spacing_reports_saved_value_even_without_fresh_runtime(self):
        self.config['youtube_cookie_pool'] = ['a', 'b']
        original = policy_key(self.config)
        for interval in (3, 0, 600):
            self.config['youtube_cookie_pool_start_interval'] = interval
            self.assertNotEqual(policy_key(self.config), original)
            value = self.show()
            self.assertFalse(value['cookie_pool']['known'])
            self.assertEqual(value['youtube_cookie_pool_start_interval'], interval)
            self.assertEqual(value['cookie_allocation']['pool_start_interval'], interval)

    def test_pool_adaptive_option_does_not_display_an_unused_instance_budget(self):
        self.config.update(youtube_cookie_pool=['a', 'b'], youtube_pacing_mode='adaptive')
        result = self.show({'youtube-pacing:instance:node': {'starts': [self.stamp] * 30}})
        self.assertIsNone(result['youtube_pacing'])
        self.assertIsNotNone(result['cookie_allocation'])

    def test_auto_native_parallel_connections_reduce_effective_video_limit(self):
        self.config.update(throughput_mode='auto', worker_count=4,
                           fragment_concurrency=4, download_connection_limit=16,
                           builtin_connection_limit=4)
        rate = {'policy': policy_key(self.config), 'time': self.stamp, 'transfers': 4}
        result = self.show({'local-connections:node': {'policies': {
            'live': {'time': self.stamp, 'limit': 4}}}}, rate=rate)
        self.assertEqual(result['channels'][0]['connection_units'], 4)
        self.assertEqual(result['effective_limit'], 1)

    def test_retired_shared_connections_do_not_reduce_eighteen_pool_slots(self):
        self.config.update(youtube_cookie_pool=['a'] * 18, builtin_transfer_limit=18,
                           builtin_connection_limit=1, download_connection_limit=32)
        rate = {'policy': policy_key(self.config), 'time': self.stamp, 'workers': 18,
                'cookie_pool': {'known': True, 'capacity': 36, 'reason': 'ready'}}
        value = self.show({'connections:youtube-origin': {'cooldown_until': self.stamp + 3600,
            'policies': {'legacy': {'time': self.stamp, 'limit': 1}}}}, rate=rate)
        self.assertEqual(value['effective_limit'], 18)
        self.assertEqual(value['transfer_allocation']['restricted'], 0)
        self.assertIsNone(value['channels'][0]['connection_limit'])
        self.assertNotIn('connections', [r['scope'] for r in value['channels'][0]['resources']])
        self.assertEqual(value['transfer_allocation']['unoccupied'], 18)
        policy = policy_key(self.config)
        self.config['builtin_connection_limit'] = 128
        self.assertEqual(policy_key(self.config), policy)

    def test_wait_counts_are_separate_from_real_transfers(self):
        value = self.show(progress=[{'stage': 'downloading'}, {'stage': 'extracting'},
            {'stage': 'waiting_transfer', 'transfer_wait_resource': 'upstream:cooldown'},
            {'stage': 'processing'}])
        self.assertEqual((value['running'], value['transferring'], value['waiting_transfer'], value['preparing'], value['processing']), (4, 1, 1, 1, 1))
        self.assertEqual(value['wait_resources'], {'upstream:cooldown': 1})

    def test_pacing_settings_and_diagnostics_share_effective_interval_and_cause(self):
        self.config.update(youtube_pacing_mode='adaptive', youtube_start_interval=45)
        value = self.show({'youtube-pacing:instance:node': {
            'interval': 120, 'last_limit_at': self.stamp - 3600,
            'wait_reason': '等待下一次视频启动'}})
        self.assertEqual(value['youtube_start_interval'], 45)
        pacing = value['youtube_pacing']
        self.assertEqual((pacing['minimum_interval'], pacing['interval']), (45, 120))
        self.assertEqual(pacing['interval_reason_code'], 'account_limit')
        self.assertTrue(pacing['initialized'])

    def test_disk_block_expires_without_claiming_it_has_recovered(self):
        context = ({}, {}, {'node': {'time': self.stamp, 'blocked': True, 'message': 'budget full'}})
        value = diagnose(self.config, 'node', [], None, context, self.stamp)
        self.assertTrue(value['disk_blocked'])
        self.assertEqual(value['disk_message'], 'budget full')
        value = diagnose(self.config, 'node', [], None, context, self.stamp + 91)
        self.assertFalse(value['disk_blocked'])
        self.assertEqual(value['disk_message'], '')

    def test_context_counts_only_permits_owned_by_live_leases(self):
        from webapp.store import TaskStore
        from webapp.database import tasks, download_permits, download_resources
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(root)
            try:
                jobs = [store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']}) for _ in range(3)]
                scope = 'local-transfers:node'
                key = hashlib.sha256(scope.encode()).hexdigest()
                with store.connect() as db:
                    for index, job in enumerate(jobs):
                        db.execute(tasks.update().where(tasks.c.id == job['id']).values(
                            status='running', worker_id='owner', lease_until=self.stamp + (60 if index != 1 else -1)))
                        db.execute(download_permits.insert().values(token=str(index), resource=key, task_id=job['id'],
                            owner='owner' if index != 2 else 'previous-owner', units=1))
                    db.execute(download_resources.insert().values(key=key, state=json.dumps({'policies': {}})))
                with store.connect() as db:
                    states, usage, _, holders = read_context(db, [('node', self.config)], self.stamp)
                    self.assertEqual(usage[scope], 1)
                    self.assertEqual(holders[scope], {jobs[0]['id']: 1})
                    self.assertEqual(states[scope], {'policies': {}})
            finally:
                store.db.engine.dispose()

    def allocation(self, stages, *, limit=12, effective=None, pool=False):
        self.config.update(builtin_transfer_limit=limit, builtin_connection_limit=64,
                           download_connection_limit=64)
        if pool:
            self.config['youtube_cookie_pool'] = ['account-a', 'account-b']
        tasks = [{'id': str(index), 'video_id': 'video-' + str(index),
                  'progress': {'stage': stage, 'cookie_profile_name': 'Account A'}}
                 for index, stage in enumerate(stages)]
        holders = {'local-transfers:node': {str(index): 1 for index in range(min(3, len(tasks)))}}
        rate = None
        if pool:
            rate = {'policy': policy_key(self.config), 'time': self.stamp,
                    'cookie_pool': {'known': True, 'capacity': 37, 'reason': 'ready'}}
        state = {'upstream:youtube-origin': {'policies': {
            'test': {'time': self.stamp, 'limit': effective}}}} if effective is not None else {}
        result = diagnose(self.config, 'node', [t['progress'] for t in tasks], rate,
                          (state, {'local-transfers:node': len(holders['local-transfers:node'])}, {}, holders),
                          self.stamp, current_tasks=tasks)
        return result['transfer_allocation']

    def test_twelve_slots_use_permits_instead_of_execution_count(self):
        value = self.allocation(['downloading', 'waiting_request', 'extracting', 'waiting_transfer', 'processing'])
        self.assertEqual(value['configured'], 12)
        self.assertEqual([t['stage'] for t in value['occupied']], ['downloading', 'waiting_request', 'extracting'])
        self.assertEqual((value['unoccupied'], value['restricted']), (9, 0))
        self.assertEqual(value['outside'], {'waiting_transfer': 1, 'processing': 1})
        self.assertEqual(value['occupied'][1]['cookie_name'], 'Account A')

    def test_shared_restriction_and_hot_reduction_preserve_existing_holders(self):
        value = self.allocation(['downloading'] * 3, effective=5)
        self.assertEqual((len(value['occupied']), value['unoccupied'], value['restricted']), (3, 2, 7))
        value = self.allocation(['downloading'] * 3, limit=2)
        self.assertEqual((value['configured'], len(value['occupied']), value['over_limit']), (2, 3, 1))
        self.assertEqual((value['unoccupied'], value['restricted']), (0, 0))

    def test_cookie_capacity_is_not_the_twelve_channel_slots(self):
        value = self.allocation(['downloading'] * 3, pool=True)
        self.assertEqual((value['configured'], value['effective']), (12, 12))

    def cookie_cards(self, profiles, *, states=None, current_tasks=(), paused=False, stamp=None,
                     start_gate=None, online=True):
        self.config.update(youtube_cookie_pool=['a', 'b', 'copy', 'bad'], builtin_transfer_limit=12,
                           builtin_connection_limit=64, download_connection_limit=64)
        pool = {'known': True, 'capacity': 6, 'effective_sessions': 2, 'reason': 'ready',
                'sampled_at': self.stamp, 'profiles': profiles}
        rate = {'policy': policy_key(self.config), 'time': self.stamp, 'cookie_pool': pool,
                'download_start_gate': start_gate or {}}
        value = diagnose(self.config, 'node', [], rate, (states or {}, {}, {}, {}),
                         self.stamp if stamp is None else stamp, current_tasks=current_tasks,
                         cookie_profiles=[{'id': p, 'name': 'Cookie ' + p} for p in self.config['youtube_cookie_pool']],
                         paused=paused, online=online)
        return {a['id']: a for a in value['cookie_allocation']['accounts']}

    def test_idle_accounts_explain_observed_account_and_shared_gates(self):
        profiles = [{'id': 'a', 'state': 'ready', 'active': 0, 'limit': 2, 'next_start_at': self.stamp + 60},
                    {'id': 'b', 'state': 'cooldown', 'active': 0, 'limit': 0, 'cooldown_until': self.stamp + 300},
                    {'id': 'copy', 'state': 'ready', 'shared_with': 'a'},
                    {'id': 'bad', 'state': 'unavailable'}]
        cards = self.cookie_cards(profiles, states={'youtube-cookie-pool:node': {'next_at': self.stamp + 10}})
        reasons = lambda identity: {r['code']: r for r in cards[identity]['reasons']}
        self.assertEqual(set(reasons('a')), {'account_interval', 'pool_interval'})
        self.assertEqual(reasons('a')['account_interval']['until'], self.stamp + 60)
        self.assertIn('account_cooldown', reasons('b'))
        self.assertNotIn('account_busy', reasons('b'))
        self.assertEqual(set(reasons('copy')), {'shared'})
        self.assertEqual(set(reasons('bad')), {'unavailable'})
        self.assertEqual(cards['a']['name'], 'Cookie a')

    def test_account_tasks_group_by_id_and_include_preparation_without_permits(self):
        tasks = [{'id': 'one', 'lease_until': self.stamp + 60,
                  'progress': {'cookie_profile_id': 'a', 'stage': 'extracting'}},
                 {'id': 'two', 'lease_until': self.stamp + 60,
                  'progress': {'cookie_profile_id': 'a', 'stage': 'waiting_request'}},
                 {'id': 'old', 'lease_until': self.stamp - 1,
                  'progress': {'cookie_profile_id': 'a', 'stage': 'downloading'}},
                 {'id': 'removed', 'progress': {'cookie_profile_id': 'removed', 'stage': 'processing'}}]
        cards = self.cookie_cards([{'id': 'a', 'state': 'ready', 'active': 2, 'limit': 2}], current_tasks=tasks)
        self.assertEqual([t['task_id'] for t in cards['a']['tasks']], ['one', 'two'])
        self.assertTrue(all(not t['transfer_reserved'] for t in cards['a']['tasks']))
        self.assertEqual(cards['a']['reasons'][0]['code'], 'account_busy')
        self.assertIn('removed', {r['code'] for r in cards['removed']['reasons']})

    def test_account_rest_is_distinct_from_start_spacing_and_expires(self):
        profiles = [{'id': 'a', 'state': 'ready', 'active': 0, 'limit': 1,
                     'rest_until': self.stamp + 30, 'next_start_at': self.stamp + 60}]
        reasons = {r['code']: r for r in self.cookie_cards(profiles)['a']['reasons']}
        self.assertEqual(reasons['account_rest']['until'], self.stamp + 30)
        self.assertEqual(reasons['account_interval']['until'], self.stamp + 60)
        profiles[0]['rest_until'] = self.stamp - 1
        reasons = {r['code'] for r in self.cookie_cards(profiles)['a']['reasons']}
        self.assertNotIn('account_rest', reasons)
        self.assertIn('account_interval', reasons)

    def test_nonpool_rest_uses_instance_completion_timestamp(self):
        self.config['youtube_task_rest_interval'] = 30
        value = self.show({'youtube-start:node': {'last_finished_at': self.stamp - 10}})
        self.assertEqual(value['youtube_task_rest_interval'], 30)
        self.assertEqual(value['youtube_task_rest_until'], self.stamp + 20)

    def test_missing_or_stale_account_samples_do_not_invent_idle_reasons(self):
        cards = self.cookie_cards([])
        self.assertTrue(all(not a['known'] for a in cards.values()))
        self.assertEqual(cards['a']['reasons'][0]['code'], 'unknown')
        cards = self.cookie_cards([{'id': 'a', 'state': 'ready', 'active': 0, 'limit': 2}], stamp=self.stamp + 21)
        self.assertFalse(cards['a']['known'])
        self.assertEqual(cards['a']['reasons'][0]['code'], 'unknown')

    def test_no_observed_gate_does_not_claim_queue_is_empty_or_video_can_start(self):
        profiles = [{'id': 'a', 'state': 'ready', 'active': 0, 'limit': 2}]
        card = self.cookie_cards(profiles)['a']
        self.assertEqual(card['reasons'][0]['code'], 'unassigned')
        self.assertEqual(card['reasons'][0]['label'], '当前未分配任务；仍需等待队列调度')
        card = self.cookie_cards(profiles, paused=True)['a']
        self.assertEqual(card['reasons'][0]['code'], 'paused')

    def test_live_local_claim_wait_is_visible_and_expired_or_offline_wait_is_not(self):
        profiles = [{'id': 'a', 'state': 'ready', 'active': 0, 'limit': 2}]
        for field, code in [('next_at', 'dispatch_interval'), ('preparing_until', 'dispatch_preparing')]:
            with self.subTest(field=field):
                gate = {field: self.stamp + 15}
                reason = self.cookie_cards(profiles, start_gate=gate)['a']['reasons'][0]
                self.assertEqual(reason['code'], code)
                self.assertEqual(reason['until'], self.stamp + 15 if field == 'next_at' else None)
                for kwargs in ({'stamp': self.stamp + 16}, {'stamp': self.stamp + 21}, {'online': False}):
                    reasons = self.cookie_cards(profiles, start_gate=gate, **kwargs)['a']['reasons']
                    self.assertNotIn(code, {r['code'] for r in reasons})

    def test_bottleneck_reports_local_claim_wait_only_for_fresh_matching_policy(self):
        self.config['youtube_cookie_pool'] = ['a']
        for field, label in [('next_at', '等待下一次任务领取'), ('preparing_until', '分配账号')]:
            with self.subTest(field=field):
                rate = {'policy': policy_key(self.config), 'time': self.stamp,
                        'cookie_pool': {'known': True, 'capacity': 1, 'reason': 'ready'},
                        'download_start_gate': {field: self.stamp + 60}}
                self.assertIn(label, self.show(rate=rate)['bottleneck'])
                rate['policy'] = []
                self.assertNotIn(label, self.show(rate=rate)['bottleneck'])
                rate.update(policy=policy_key(self.config), time=self.stamp - 21)
                self.assertNotIn(label, self.show(rate=rate)['bottleneck'])

    def test_shared_start_gates_show_later_deadline_without_duplicate_reasons(self):
        cards = self.cookie_cards([{'id': 'a', 'state': 'ready', 'active': 0, 'limit': 2}], states={
            'youtube-cookie-pool:node': {'next_at': self.stamp + 5},
            'youtube-start:node': {'next_at': self.stamp + 9}})
        self.assertEqual(len(cards['a']['reasons']), 1)
        self.assertEqual(cards['a']['reasons'][0]['until'], self.stamp + 9)

    def test_expired_cooldown_snapshot_does_not_invent_full_account_concurrency(self):
        cards = self.cookie_cards([{'id': 'a', 'state': 'cooldown', 'active': 0, 'limit': 0,
                                  'cooldown_until': self.stamp - 1}])
        self.assertEqual(cards['a']['reasons'][0]['code'], 'account_refresh')
