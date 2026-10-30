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
                       'fragment_concurrency': 4, 'download_connection_limit': 32}
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

    def test_wait_counts_are_separate_from_real_transfers(self):
        value = self.show(progress=[{'stage': 'downloading'}, {'stage': 'extracting'},
            {'stage': 'waiting_transfer', 'transfer_wait_resource': 'upstream:cooldown'},
            {'stage': 'processing'}])
        self.assertEqual((value['running'], value['transferring'], value['waiting_transfer'], value['preparing'], value['processing']), (4, 1, 1, 1, 1))
        self.assertEqual(value['wait_resources'], {'upstream:cooldown': 1})

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
                scope = 'upstream:youtube-origin'
                key = hashlib.sha256(scope.encode()).hexdigest()
                with store.connect() as db:
                    for index, job in enumerate(jobs):
                        db.execute(tasks.update().where(tasks.c.id == job['id']).values(
                            status='running', worker_id='owner', lease_until=self.stamp + (60 if index != 1 else -1)))
                        db.execute(download_permits.insert().values(token=str(index), resource=key, task_id=job['id'],
                            owner='owner' if index != 2 else 'previous-owner', units=1))
                    db.execute(download_resources.insert().values(key=key, state=json.dumps({'policies': {}})))
                with store.connect() as db:
                    states, usage, _ = read_context(db, [('node', self.config)], self.stamp)
                    self.assertEqual(usage[scope], 1)
                    self.assertEqual(states[scope], {'policies': {}})
            finally:
                store.db.engine.dispose()
