"""Durable rotation counters and real node switching between queued videos."""
import http.server
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yaml

from app import create_app
from webapp.clash import ClashManager, ROOT


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text('path: videos\n', encoding='utf-8')
        self.app = create_app(self.config, self.root / 'state', start_worker=False)
        self.client = self.app.test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]
        self.headers = {'X-CSRF-Token': token}
        self.manager = self.app.extensions['clash']
        self.store = self.app.extensions['task_store']

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def nodes(self, names=('A', 'B', 'C')):
        return [{'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': 1234} for name in names]

    def seed(self):
        (self.manager.directory / 'nodes.yaml').write_text(
            yaml.safe_dump({'proxies': self.nodes()}), encoding='utf-8')
        self.manager.config['selected'] = 'B'
        self.manager.save()

    def rotation(self, **overrides):
        return {'enabled': True, 'nodes': ['A', 'B', 'C'], 'every': 2,
                'count_failures': True, **overrides}

    def test_validation_order_persistence_idempotence_and_queue_lock(self):
        self.seed()
        good = self.rotation(nodes=['C', 'B', 'A'])
        response = self.post('/api/clash/rotation', good)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['rotation']['nodes'], ['C', 'B', 'A'])
        self.assertEqual(response.json['rotation']['next_node'], 'B')
        generation = response.json['rotation']['id']
        self.assertEqual(ClashManager(self.manager.directory).rotation()['id'], generation)
        self.assertEqual(self.post('/api/clash/rotation', good).json['rotation']['id'], generation)
        before = (self.manager.directory / 'settings.json').read_bytes()
        for invalid in (self.rotation(nodes=['A']), self.rotation(nodes=['A', 'A']),
                        self.rotation(nodes=['A', 'missing']), self.rotation(nodes='A'),
                        self.rotation(nodes=[{}, 'A']), self.rotation(every=0), self.rotation(every=True),
                        self.rotation(every=1.5), self.rotation(every=1001), self.rotation(enabled=1),
                        self.rotation(count_failures='yes'), {}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.post('/api/clash/rotation', invalid).status_code, 400)
                self.assertEqual((self.manager.directory / 'settings.json').read_bytes(), before)
        self.assertEqual(self.client.post('/api/clash/rotation', json=good).status_code, 403)
        self.store.create('download', {})
        self.assertEqual(self.post('/api/clash/rotation', self.rotation(every=3)).status_code, 409)

    def test_ledger_counts_failures_optionally_and_never_skips_or_other_configurations(self):
        for status, skipped, generation in [('succeeded', False, 'rotation-1'),
                                             ('failed', False, 'rotation-1'),
                                             ('succeeded', True, 'rotation-1'),
                                             ('succeeded', False, 'rotation-2')]:
            task = self.store.create('download', {})
            self.store.update_progress(task['id'], {'rotation_id': generation})
            self.store.finish(task['id'], result={'skipped': skipped}, error='failed' if status == 'failed' else None)
        task = self.store.create('download', {})
        self.store.update_progress(task['id'], {'rotation_id': 'rotation-1'})
        self.assertEqual(self.store.rotation_completed('rotation-1'), 2)
        self.assertEqual(self.store.rotation_completed('rotation-1', False), 1)
        self.assertEqual(self.store.rotation_completed('rotation-2'), 1)

    def test_existing_snapshot_uses_saved_rotation_for_each_worker(self):
        import yt_dlp
        from webapp.worker.connection import prepare_connection
        self.seed()
        queued = {'directory': str(self.manager.directory), 'node': 'B', 'proxy': self.manager.proxy_url}
        payload = {'config': {'path': 'videos', 'proxy_mode': 'clash'}, 'base': str(self.root),
                   'urls': ['http://example.test/fixture.mp4'], 'clash': queued}
        for slot, policy in [(0, self.rotation()), (1, self.rotation(every=7)),
                             (1, self.rotation(enabled=False))]:
            with self.subTest(slot=slot, policy=policy):
                self.manager.configure_rotation(policy)
                saved = self.manager.rotation()
                counts = []
                progress = []
                def completed(identity, count_failures):
                    counts.append((identity, count_failures))
                    return 5
                with patch.object(ClashManager, 'download_snapshot', return_value=queued) as resolve, \
                     patch.object(ClashManager, 'select'), \
                     patch.object(yt_dlp.YoutubeDL, 'download', return_value=0), \
                     patch('webapp.environment.require_native_environment'):
                    from webapp.downloads import DownloadProgress
                    prepare_connection(payload['config'], payload, DownloadProgress(progress.append),
                                       rotation_completed=completed, worker_slot=slot)
                actual, count = resolve.call_args.args
                if policy['enabled']:
                    self.assertEqual(actual['rotation'], saved)
                    expected_id = saved['id'] + (f':{slot}' if slot else '')
                    self.assertEqual(counts, [(expected_id, policy['count_failures'])])
                    self.assertEqual(count, 5)
                    # Simulate a pre-existing task snapshot after another edit.
                    queued['rotation'] = saved
                else:
                    self.assertNotIn('rotation', actual)
                    self.assertEqual(counts, [])
                    self.assertEqual(count, 0)





if __name__ == '__main__':
    unittest.main()
