"""Multiple workers wait before claiming; local hints never replace DB permits."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from webapp.download_start_gate import DownloadStartGate
from webapp.store import TaskStore
from webapp.throughput import policy_key


class DownloadStartGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gate = DownloadStartGate(self.root)
        self.snapshot()

    def snapshot(self, **summary):
        (self.root / 'throughput.json').write_text(json.dumps(dict(
            mode='cookie_pool', time=time.time(), policy=['cookie_pool', 'test'],
            cookie_pool=dict(next_start_at=0, **summary))), encoding='utf-8')

    def test_twenty_four_workers_make_only_one_claim_and_leave_others_queued(self):
        store = TaskStore(self.root)
        for i in range(24):
            store.create('download', dict(base=str(self.root), config={}, urls=[f'https://youtu.be/{i:011d}']))
        with ThreadPoolExecutor(24) as pool:
            results = list(pool.map(lambda _: self.gate.claim(store, actions=('download',)), range(24)))
        claimed = [r for r in results if r[0]]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(sum(r[2] for r in results), 23)
        from sqlalchemy import select
        from webapp.database import tasks
        with store.connect() as db:
            rows = db.execute(select(tasks.c.status, tasks.c.attempts)).all()
        self.assertEqual(rows.count(('queued', 0)), 23)
        claimed[0][1].finish(10)
        with patch.object(store, 'claim', side_effect=AssertionError('Claimed during local pacing')):
            for _ in range(24):
                self.assertTrue(self.gate.claim(store, actions=('download',))[2])

    def test_other_lanes_remain_runnable_while_downloads_wait(self):
        ticket, _ = self.gate.acquire()
        store = Mock(owner='fixture')
        store.claim.return_value = {'action': 'check'}
        task, other_ticket, waiting = self.gate.claim(store, exclude=('import', 'index', 'subtitles'))
        self.assertEqual(task['action'], 'check')
        self.assertIsNone(other_ticket)
        self.assertTrue(waiting)
        self.assertIn('download', store.claim.call_args.kwargs['exclude'])
        self.gate.claim(store, actions=('import', 'index'))
        self.assertEqual(store.claim.call_args.kwargs['actions'], ('import', 'index'))
        ticket.finish()

    def test_crashed_owner_is_reclaimed_and_stale_ticket_cannot_overwrite_new_one(self):
        first, _ = self.gate.acquire()
        with patch('webapp.download_start_gate.alive', return_value=False):
            second, waiting = self.gate.acquire()
        self.assertFalse(waiting)
        first.finish(60)
        self.assertEqual(self.gate.read()['token'], second.token)
        second.finish(2)
        self.assertIsNone(self.gate.read()['token'])

    def test_live_owner_lease_expiry_and_configuration_change_release_wait(self):
        first, _ = self.gate.acquire()
        self.assertTrue(self.gate.acquire()[1])
        state = self.gate.read()
        state['expires_at'] = time.time() - 1
        self.gate.path.write_text(json.dumps(state), encoding='utf-8')
        second, waiting = self.gate.acquire()
        self.assertFalse(waiting)
        second.finish(60)
        data = json.loads((self.root / 'throughput.json').read_text())
        data['policy'] = ['cookie_pool', 'changed']
        (self.root / 'throughput.json').write_text(json.dumps(data), encoding='utf-8')
        self.assertFalse(self.gate.acquire()[1])

    def test_stale_snapshot_falls_back_and_future_snapshot_defers_without_claim(self):
        data = json.loads((self.root / 'throughput.json').read_text())
        data['cookie_pool']['next_start_at'] = time.time() + 3600
        (self.root / 'throughput.json').write_text(json.dumps(data), encoding='utf-8')
        store = Mock(owner='fixture')
        self.assertTrue(self.gate.claim(store, actions=('download',))[2])
        store.claim.assert_not_called()
        data['time'] = time.time() - 21
        (self.root / 'throughput.json').write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(self.gate.acquire(), (None, False))

    def test_empty_or_failed_claim_releases_local_ticket(self):
        store = Mock(owner='fixture')
        store.claim.side_effect = RuntimeError('database unavailable')
        with self.assertRaises(RuntimeError):
            self.gate.claim(store, actions=('download',))
        self.assertIsNone(self.gate.read()['token'])

    def test_status_only_reports_live_waits_for_the_current_pool_policy(self):
        config = {'youtube_cookie_pool': ['a']}
        snapshot = json.loads((self.root / 'throughput.json').read_text())
        snapshot['policy'] = policy_key(config)
        (self.root / 'throughput.json').write_text(json.dumps(snapshot), encoding='utf-8')
        ticket, _ = self.gate.acquire()
        stamp = time.time()
        status = self.gate.status(config, stamp)
        self.assertEqual(set(status), {'preparing_until'})
        self.assertGreater(status['preparing_until'], stamp)
        with patch('webapp.download_start_gate.alive', return_value=False):
            self.assertEqual(self.gate.status(config, stamp), {})
        self.assertEqual(self.gate.status(config, stamp + 61), {})
        self.assertEqual(self.gate.status({}), {})
        self.assertEqual(self.gate.status({'youtube_cookie_pool': ['other']}), {})
        ticket.finish(60)
        status = self.gate.status(config)
        self.assertEqual(set(status), {'next_at'})
        self.assertGreater(status['next_at'], time.time())
        self.assertEqual(self.gate.status(config, status['next_at']), {})

    def run_scoped_wait(self, *, manual_retry=False):
        from web_worker import drain
        from webapp.cookie_pool import CookiePool
        from webapp.cookie_profiles import CookieProfiles
        from webapp.diagnostics import process_diagnostics

        store = TaskStore(self.root)
        self.addCleanup(store.db.engine.dispose)
        base = self.root / 'app'
        profiles = CookieProfiles(base, store)
        cookie = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tTEST_SECRET\n'
        ids = [profiles.add(str(i), cookie.replace(b'TEST_SECRET', str(i).encode()))['id']
               for i in range(3)]
        config = {'download_method': 'yt_dlp', 'youtube_cookie_pool': ids,
                  'youtube_start_interval': 120, 'youtube_cookie_session_limit': 1,
                  'youtube_cookie_pool_start_interval': 5, 'storage_mode': 'test',
                  'path': str(self.root / 'videos'), 'min_free_gb': 0,
                  'disk_min_free_gb': 0, 'disk_resume_free_gb': 0}
        store.set_runtime(config)
        store.set_download_settings(config, base, base / '.cache/clash')
        pool = CookiePool(store, config, base, None)

        def block(db, stamp):
            scope = pool.global_scope if manual_retry else pool.accounts[0][2]
            state = pool.router._state(db, scope)
            state['retry_next_at' if manual_retry else 'next_at'] = stamp + 3600
            pool.router._save(db, scope, state)
        pool.router.transaction_call(block)
        payload = {'base': str(base), 'config': config, 'urls': ['https://youtu.be/00000000001']}
        flags = {'_manual_retry': True} if manual_retry else {
            '_retry_cookie_profile_id': ids[0], '_retry_cookie_instance_id': 'local'}
        blocked = store.create('download', {**payload, **flags})
        normal = store.create('download', {**payload, 'urls': ['https://youtu.be/00000000002']})
        (self.root / 'throughput.json').write_text(json.dumps({
            'mode': 'cookie_pool', 'time': time.time(), 'policy': policy_key(config),
            'cookie_pool': pool.status()['capacity_summary']}), encoding='utf-8')
        completed = []

        def download(action, selected, *args, **kwargs):
            completed.append(selected['config']['_cookie_pool_selected'])
            return {'skipped': True}

        original_sleep = time.sleep

        def sleep(seconds):
            self.assertLess(seconds, 1, 'Account-specific backoff stalled the worker or local claim gate')
            original_sleep(seconds)

        with patch.dict(os.environ), process_diagnostics(self.root, 0), \
                patch('web_worker._execute', side_effect=download), \
                patch('webapp.throughput.worker_should_retire', side_effect=lambda *_: bool(completed)), \
                patch('web_worker.time.sleep', side_effect=sleep), \
                patch('web_worker.os.dup2'), patch('builtins.print'):
            drain(self.root, role='download')
        queued = store.get(blocked['id'], private=True)
        self.assertEqual((queued['status'], queued['attempts']), ('queued', 0))
        self.assertGreater(queued['available_at'], time.time() + 50)
        for key, value in flags.items():
            self.assertEqual(queued['payload'][key], value)
        self.assertEqual(store.get(normal['id'])['status'], 'succeeded')
        self.assertEqual(len(completed), 1)
        if not manual_retry:
            self.assertNotEqual(completed[0], ids[0])

    def test_pinned_account_wait_preserves_binding_and_worker_continues_normal_download(self):
        self.run_scoped_wait()

    def test_manual_retry_quota_wait_does_not_stall_normal_download(self):
        self.run_scoped_wait(manual_retry=True)
