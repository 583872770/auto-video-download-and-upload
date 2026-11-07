"""Cross-node credential ownership, races, legacy data and effective controls."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import select
from tests import test_fleet
from webapp.cookie_binding import CookieBindingConflict
from webapp.cookie_profiles import CookieProfiles
from webapp.database import cookie_bindings, cookie_profiles

COOKIE = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tPRIVATE_SESSION\n'


class CookieBindingTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet
    serve = test_fleet.FleetTests.serve

    def library(self, node):
        return CookieProfiles(self.root / node, self.apps[node].extensions['task_store'])

    def test_reexport_and_renaming_do_not_allow_another_node_to_use_the_cookie(self):
        first, second = self.library('alpha'), self.library('beta')
        identity = first.add('Account A', COOKIE)['id']
        # Changed comments and auxiliary cookies do not create a new identity.
        changed = COOKIE + b'.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tDIFFERENT\n'
        with self.assertRaises(CookieBindingConflict) as result:
            second.add('Renamed Account', changed)
        self.assertIn('alpha', str(result.exception))
        self.assertNotIn('PRIVATE_SESSION', str(result.exception))
        self.assertEqual(second.items(), [])
        self.assertTrue(first.require(identity).is_file())
        self.assertEqual(first.items()[0]['instance_id'], 'alpha')
        # Same-node aliases remain compatible with pool session deduplication.
        first.add('Another export', changed)
        self.assertEqual(len(first.items()), 2)

    def test_any_shared_core_token_blocks_partial_reexports_atomically(self):
        extra = b'.youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\tSHARED_TOKEN\n'
        self.library('alpha').add('A', COOKIE + extra)
        changed = COOKIE.replace(b'PRIVATE_SESSION', b'NEW_SESSION') + extra
        with self.assertRaises(CookieBindingConflict):
            self.library('beta').save_legacy(changed)
        with self.b.connect() as db:
            self.assertFalse(db.execute(select(cookie_bindings).where(
                cookie_bindings.c.scope == 'beta')).first())
        self.library('beta').add('Independent', COOKIE.replace(b'PRIVATE_SESSION', b'INDEPENDENT'))

    def test_concurrent_imports_have_exactly_one_owner(self):
        libraries = [self.library('alpha'), self.library('beta')]
        barrier = threading.Barrier(2)
        def upload(library):
            barrier.wait(timeout=10)
            try:
                return library.add('A', COOKIE)
            except CookieBindingConflict:
                return None
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(upload, libraries))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(sum(len(library.items()) for library in libraries), 1)

    def test_custom_path_and_legacy_upload_cannot_bypass_binding(self):
        self.library('alpha').add('A', COOKIE)
        second = self.library('beta')
        custom = self.root / 'beta/custom.txt'
        custom.write_bytes(COOKIE)
        with self.assertRaises(CookieBindingConflict):
            second.restore_selected('custom.txt')
        with self.assertRaises(CookieBindingConflict):
            second.save_legacy(COOKIE)
        with patch('webapp.youtube_auth.probe') as probe:
            result = self.apps['beta'].extensions['youtube_auth'].check(
                {'youtube_cookie': 'custom.txt', 'proxy_mode': 'direct'}, self.root / 'beta')
        probe.assert_not_called()
        self.assertEqual(result['state'], 'invalid')
        self.assertIn('alpha', result['message'])

    def test_deleting_and_offline_instances_keep_ownership_for_old_snapshots(self):
        first, second = self.library('alpha'), self.library('beta')
        identity = first.add('A', COOKIE)['id']
        first.delete(identity)
        self.assertEqual(first.items(), [])
        with self.assertRaises(CookieBindingConflict):
            second.add('A', COOKIE)
        first.add('Restored', COOKIE)

    def test_worker_runtime_conflict_uses_auth_isolation_before_network_or_download(self):
        from web_worker import _execute
        from webapp.youtube_auth import YouTubeAuthRequired
        self.library('alpha').add('A', COOKIE)
        (self.root / 'beta/custom.txt').write_bytes(COOKIE)
        config = {**self.b.fleet.local_settings()['config'], 'download_methods': ['yt_dlp'],
                  'youtube_cookie': 'custom.txt'}
        with patch('webapp.youtube_auth.probe') as probe, patch('webapp.downloader.require_download_tools') as tools:
            with self.assertRaises(YouTubeAuthRequired) as result:
                _execute('download', {'config': config, 'base': str(self.root / 'beta'),
                    '_execution_settings': True, 'urls': ['https://youtu.be/npNnbj46_ik']}, store=self.b)
        probe.assert_not_called()
        tools.assert_not_called()
        self.assertFalse(result.exception.retryable)
        self.assertIn('alpha', str(result.exception))

    def test_upgrade_preserves_old_duplicates_but_only_earliest_owner_can_execute(self):
        from webapp.migrations import _cookie_ownership
        from webapp.cookie_pool import CookiePool
        from webapp.youtube_auth import YouTubeAuthRequired
        with self.a.connect() as db:
            for node, day in [('alpha', '01'), ('beta', '02')]:
                db.execute(cookie_profiles.insert().values(scope=node, id='a' * 32, name=node,
                    content=COOKIE.decode(), created_at=f'2026-11-{day}', deleted=0))
            # Cross the migration page boundary without changing ownership of
            # the original credential; same-instance exports share the binding.
            for number in range(55):
                db.execute(cookie_profiles.insert().values(scope='alpha', id=f'{number:032x}', name='Historical',
                    content=COOKIE.decode(), created_at='2026-10-31', deleted=0))
        _cookie_ownership(self.a.db.engine)
        _cookie_ownership(self.a.db.engine)  # Resume-safe migration.
        self.assertTrue(self.library('alpha').require('a' * 32).is_file())
        second = self.library('beta')
        self.assertEqual(len(second.items()), 1)
        response = self.apps['beta'].test_client().get('/api/youtube/cookies')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(response.json['items']), 1)
        self.assertIn(b'PRIVATE_SESSION', second.export('a' * 32)[1])
        with self.assertRaises(YouTubeAuthRequired) as result:
            CookiePool(self.b, {'youtube_cookie_pool': ['a' * 32]}, self.root / 'beta', None)
        self.assertIn('alpha', str(result.exception))
        second.delete('a' * 32)

    def test_saving_foreign_pool_ids_and_custom_credentials_is_rejected(self):
        identity = self.library('alpha').add('A', COOKIE)['id']
        self.serve('beta')
        headers = {**self.headers, 'X-Instance-ID': 'beta'}
        response = self.client.put('/api/config', json={'download_methods': ['yt_dlp'],
            'youtube_cookie_pool': [identity]}, headers=headers)
        self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.b.fleet.local_settings()['config']['youtube_cookie_pool'], [])
        (self.root / 'beta/custom.txt').write_bytes(COOKIE)
        response = self.client.put('/api/config', json={'youtube_cookie': 'custom.txt'}, headers=headers)
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn('alpha', response.json['error'])

    def test_pool_controls_save_to_selected_instance_and_match_settings_and_scheduler(self):
        from webapp.channels import registry
        from webapp.youtube_pacing import cookie_pool_start_interval
        self.serve('alpha')
        limits = {'builtin_transfer_limit': 12, 'youtube_cookie_session_limit': 3,
                  'youtube_cookie_pool_start_interval': 7}
        response = self.client.put('/api/instances/alpha', json=limits, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        config = self.apps['alpha'].test_client().get('/api/config').json['config']
        for key, value in limits.items():
            self.assertEqual(config[key], value)
        effective = self.a.fleet.local_settings()['config']
        effective.update(download_methods=['yt_dlp'], download_channels=[])
        self.assertEqual(registry(effective)[0].transfer_limit, 12)
        self.assertEqual(cookie_pool_start_interval(effective), 7)
        self.assertNotEqual(self.b.fleet.local_settings()['config']['builtin_transfer_limit'], 12)
        # Saving full settings must also replace the resource overlay.
        response = self.client.put('/api/config', json={'builtin_transfer_limit': 9},
            headers={**self.headers, 'X-Instance-ID': 'alpha'})
        self.assertEqual(response.status_code, 200, response.json)
        item = next(item for item in self.a.fleet.list()['items'] if item['id'] == 'alpha')
        self.assertEqual(item['resources']['builtin_transfer_limit'], 9)
        for key, value in [('builtin_transfer_limit', 65), ('youtube_cookie_session_limit', 0),
                           ('youtube_cookie_pool_start_interval', -1)]:
            response = self.client.put('/api/instances/alpha', json={key: value}, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.json)


if __name__ == '__main__':
    unittest.main()
