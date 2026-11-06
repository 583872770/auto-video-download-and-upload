"""Multiple workers wait before claiming; local hints never replace DB permits."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from webapp.download_start_gate import DownloadStartGate
from webapp.store import TaskStore


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
