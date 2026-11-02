"""Cookie library API isolation, selection, secret handling and busy guards."""
import io
import unittest
from unittest.mock import patch

import yaml

from tests import test_web
from webapp.console_auth import LOCAL_RPC_METHODS
from webapp.database import cookie_profiles

COOKIE = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tPRIVATE_COOKIE_VALUE\n'


class CookieProfileTests(unittest.TestCase):
    setUp = test_web.WebTests.setUp

    def add(self, name='Account A', content=COOKIE):
        return self.client.post('/api/youtube/cookies', data={
            'name': name, 'cookie': (io.BytesIO(content), 'cookies.txt')}, headers=self.headers)

    def action(self, action, identity):
        return self.client.post('/api/youtube/cookies/' + action,
                                json={'id': identity}, headers=self.headers)

    def replace_content(self, identity, content):
        with self.store.connect() as db:
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id == identity).values(
                content=content.decode()))

    def test_blank_name_defaults_to_filename(self):
        for name in ('', '   '):
            response = self.add(name)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json['item']['name'], 'cookies.txt')
        response = self.client.post('/api/youtube/cookies', data={
            'cookie': (io.BytesIO(COOKIE), 'account-export.txt')}, headers=self.headers)
        self.assertEqual(response.json['item']['name'], 'account-export.txt')

    def test_status_uses_session_capacity_in_custom_mode_without_online_probes(self):
        from webapp.cookie_pool import CookiePool
        from webapp.throughput import MIB, ThroughputController
        ids = [self.add(name).json['item']['id'] for name in ('A', 'B')]
        config = yaml.safe_load(self.config.read_text())
        config.update(throughput_mode='manual', worker_count=16, youtube_cookie_pool=ids,
                      download_methods=['yt_dlp'])
        self.config.write_text(yaml.safe_dump(config), encoding='utf-8')
        summary = CookiePool(self.store, config, self.root, None).status()['capacity_summary']
        controller = ThroughputController(self.store.directory)
        sample = dict(cpu_cores=4, memory_total=8192*MIB, memory_available=4096*MIB,
                      cpu_percent=20, io_pressure=0, rx=0)
        with patch.object(controller, 'sample', return_value=sample), patch('webapp.youtube_auth.probe') as probe:
            controller.update(config, cookie_capacity=summary)
            result = self.client.get('/api/status')
            probe.assert_not_called()
        self.assertEqual(result.status_code, 200)
        capacity = result.json['concurrency']
        self.assertEqual(capacity['requested'], 1)
        self.assertEqual(capacity['mode'], 'cookie_pool')
        self.assertEqual(capacity['cookie_pool']['capacity'], 1)
        self.assertEqual(capacity['effective_limit'], 1)
        self.assertEqual(capacity['cookie_pool']['configured_files'], 2)
        self.assertEqual(capacity['cookie_pool']['effective_sessions'], 1)
        self.assertNotIn('PRIVATE_COOKIE_VALUE', result.text)

    def test_session_limit_setting_saves_and_pool_list_reports_effective_limit(self):
        identity = self.add().json['item']['id']
        response = self.client.put('/api/config', json={'youtube_cookie_pool': [identity],
            'youtube_cookie_session_limit': 2, 'worker_count': 6}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.client.get('/api/config').json['config']['youtube_cookie_session_limit'], 2)
        pool = self.client.get('/api/youtube/cookies').json['pool']
        self.assertEqual(pool['session_limit'], 2)
        self.assertEqual(pool['execution_limit'], 64)
        self.assertEqual(pool['accounts'][0]['configured_limit'], 2)
        self.assertEqual(pool['accounts'][0]['limit'], 1)
        self.assertIn('name="youtube_cookie_session_limit"', self.client.get('/').text)

    def test_database_restores_deleted_cache_and_metadata(self):
        from webapp.cookie_profiles import CookieProfiles
        profiles = CookieProfiles(self.root, self.store)
        identity = self.add().json['item']['id']
        target = profiles.require(identity)
        content = target.read_bytes()
        target.unlink()
        restored = CookieProfiles(self.root, self.store)
        self.assertEqual(restored.items()[0]['name'], 'Account A')
        self.assertEqual(restored.require(identity).read_bytes(), content)
        self.assertEqual(self.action('select', identity).status_code, 200)

    def test_profiles_isolated_by_registered_instance(self):
        from types import SimpleNamespace
        from webapp.cookie_profiles import CookieProfiles
        self.store.fleet = SimpleNamespace(id='instance-a')
        first = CookieProfiles(self.root, self.store)
        identity = first.add('A', COOKIE)['id']
        # No file is needed to retain a newly saved account.
        self.assertFalse(first.path(identity).exists())
        self.store.fleet = SimpleNamespace(id='instance-b')
        second = CookieProfiles(self.root, self.store)
        self.assertEqual(second.items(), [])
        with self.assertRaises(ValueError):
            second.require(identity)
        self.store.fleet = SimpleNamespace(id='instance-a')
        self.assertEqual(CookieProfiles(self.root, self.store).items()[0]['id'], identity)

    def test_existing_profiles_migrate_once_and_deleted_cache_cannot_resurrect(self):
        import json
        from webapp.cookie_profiles import CookieProfiles
        identity = 'a' * 32
        directory = self.root / 'Cookies/profiles'
        directory.mkdir(parents=True)
        target = directory / (identity + '.txt')
        target.write_bytes(COOKIE)
        target.with_suffix('.json').write_text(json.dumps({
            'name': 'Historical', 'created_at': '2026-10-31T00:00:00+00:00'}))
        profiles = CookieProfiles(self.root, self.store)
        self.assertEqual(profiles.items()[0]['name'], 'Historical')
        profiles.delete(identity)
        target.write_bytes(COOKIE)
        self.assertEqual(CookieProfiles(self.root, self.store).items(), [])

    def test_save_many_select_and_delete_without_exposing_secrets(self):
        first, second = self.add(), self.add('Account B')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        first_id, second_id = first.json['item']['id'], second.json['item']['id']
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(first.json['item']['active'])
        self.assertEqual(yaml.safe_load(self.config.read_text())['youtube_cookie'],
                         f'Cookies/profiles/{first_id}.txt')
        result = self.action('select', first_id)
        self.assertEqual(result.status_code, 200, result.text)
        config = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        self.assertEqual(config['youtube_cookie'], f'Cookies/profiles/{first_id}.txt')
        listed = self.client.get('/api/youtube/cookies')
        self.assertEqual(len(listed.json['items']), 2)
        self.assertEqual([x['id'] for x in listed.json['items'] if x['active']], [first_id])
        for response in (first, second, result, listed):
            self.assertNotIn('PRIVATE_COOKIE_VALUE', response.text)
        deleted = self.action('delete', first_id)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json['selection_changed'])
        next_path = f'Cookies/profiles/{second_id}.txt'
        self.assertEqual(deleted.json['cookie_path'], next_path)
        self.assertEqual(yaml.safe_load(self.config.read_text())['youtube_cookie'], next_path)
        self.assertEqual(self.store.runtime()['youtube_cookie'], str(self.root / next_path))
        self.assertFalse((self.root / config['youtube_cookie']).exists())
        self.assertEqual(self.client.get('/api/youtube/cookies').json['items'][0]['id'], second_id)
        self.assertTrue(self.client.get('/api/youtube/cookies').json['items'][0]['active'])
        deleted = self.action('delete', second_id)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json['cookie_path'], '')
        self.assertEqual(yaml.safe_load(self.config.read_text())['youtube_cookie'], '')
        self.assertEqual(self.store.runtime()['youtube_cookie'], '')
        listed = self.client.get('/api/youtube/cookies').json
        self.assertEqual(listed['items'], [])
        self.assertFalse(listed['custom_active'])
        self.assertFalse(self.client.get('/api/status').json['youtube']['saved'])
        # Refreshing the list must neither resurrect deleted credentials nor select a stale path.
        self.assertEqual(yaml.safe_load(self.config.read_text())['youtube_cookie'], '')
        self.assertNotIn('PRIVATE_COOKIE_VALUE', deleted.text)

    def test_delete_inactive_keeps_selection_and_login_status(self):
        first = self.add().json['item']['id']
        second = self.add('Account B').json['item']['id']
        original = self.config.read_bytes()
        auth = self.app.extensions['youtube_auth']
        before = auth.status()
        response = self.action('delete', second)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['selection_changed'])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(auth.status(), before)
        self.assertEqual([(item['id'], item['active']) for item in
                          self.client.get('/api/youtube/cookies').json['items']], [(first, True)])

    def test_delete_current_selects_first_remaining_including_legacy(self):
        legacy = self.root / 'Cookies/youtube.txt'
        legacy.parent.mkdir()
        legacy.write_bytes(COOKIE)
        first = self.add().json['item']['id']
        second = self.add('Account B').json['item']['id']
        self.action('select', first)
        response = self.action('delete', first)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['cookie_path'], 'Cookies/youtube.txt')
        response = self.action('delete', 'legacy')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['cookie_path'], f'Cookies/profiles/{second}.txt')
        self.assertFalse(legacy.exists())
        self.assertEqual([item['id'] for item in self.client.get('/api/youtube/cookies').json['items']], [second])

    def test_delete_only_legacy_cookie_clears_selection_and_does_not_resurrect(self):
        legacy = self.root / 'Cookies/youtube.txt'
        legacy.parent.mkdir()
        legacy.write_bytes(COOKIE)
        listed = self.client.get('/api/youtube/cookies').json
        self.assertEqual(listed['cookie_path'], 'Cookies/youtube.txt')
        self.assertTrue(listed['items'][0]['active'])
        response = self.action('delete', 'legacy')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['cookie_path'], '')
        self.assertFalse(legacy.exists())
        # A stale cache restored externally cannot re-import tombstoned credentials.
        legacy.write_bytes(COOKIE)
        self.assertEqual(self.client.get('/api/youtube/cookies').json['items'], [])
        self.assertEqual(yaml.safe_load(self.config.read_text())['youtube_cookie'], '')
        self.assertEqual(self.store.runtime()['youtube_cookie'], '')
        self.assertFalse(self.client.get('/api/status').json['youtube']['saved'])

    def test_delete_current_clears_stale_login_but_preserves_pause_and_block(self):
        first = self.add().json['item']['id']
        self.add('Account B')
        auth = self.app.extensions['youtube_auth']
        auth._write('youtube_auth_latest', {'state': 'valid', 'expires_at': 4000000000})
        reason = {'state': 'verification_required', 'message': 'manual verification required'}
        auth.block(reason)
        self.assertEqual(self.action('delete', first).status_code, 200)
        self.assertEqual(auth.status()['state'], 'unchecked')
        self.assertEqual(auth.block_reason(), reason)
        self.assertTrue(self.store.download_queue()['paused'])

    def test_delete_current_with_expired_replacement_still_removes_requested_profile(self):
        first = self.add().json['item']['id']
        second = self.add('Expired').json['item']['id']
        self.replace_content(second, COOKIE.replace(b'\t0\tSID', b'\t1\tSID'))
        response = self.action('delete', first)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json['cookie_path'], f'Cookies/profiles/{second}.txt')
        self.assertEqual(self.client.get('/api/youtube/cookies').json['items'][0]['id'], second)

    def test_delete_current_failure_preserves_database_config_and_runtime(self):
        first = self.add().json['item']['id']
        second = self.add('Account B').json['item']['id']
        self.action('select', first)
        original = self.config.read_bytes()
        runtime = self.store.runtime()
        for operation in ('app.save_settings', 'webapp.cookie_profiles.CookieProfiles.delete'):
            with self.subTest(operation=operation), patch(operation, side_effect=OSError('fixture failure')):
                self.assertEqual(self.action('delete', first).status_code, 400)
            self.assertEqual(self.config.read_bytes(), original)
            self.assertEqual(self.store.runtime(), runtime)
            items = self.client.get('/api/youtube/cookies').json['items']
            self.assertEqual([item['id'] for item in items], [first, second])
            self.assertEqual([item['id'] for item in items if item['active']], [first])

    def test_delete_current_is_blocked_while_download_or_browser_login_is_active(self):
        identity = self.add().json['item']['id']
        original = self.config.read_bytes()
        with patch.object(self.store, 'download_connection_busy', return_value=True):
            self.assertEqual(self.action('delete', identity).status_code, 409)
        with patch.object(self.app.extensions['youtube_login'], 'status', return_value={'active': True}):
            self.assertEqual(self.action('delete', identity).status_code, 409)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.client.get('/api/youtube/cookies').json['items'][0]['id'], identity)

    def test_delete_pool_member_updates_pool_and_current_selection(self):
        ids = [self.add(name).json['item']['id'] for name in ('A', 'B', 'C')]
        config = yaml.safe_load(self.config.read_text())
        config['youtube_cookie_pool'] = ids
        self.config.write_text(yaml.safe_dump(config), encoding='utf-8')
        self.assertEqual(self.action('delete', ids[0]).status_code, 200)
        listed = self.client.get('/api/youtube/cookies').json
        self.assertEqual([item['id'] for item in listed['items'] if item['active']], [ids[1]])
        self.assertEqual([item['id'] for item in listed['items'] if item['pooled']], ids[1:])
        self.assertIsNotNone(listed['pool'])
        self.assertEqual(self.action('delete', ids[1]).status_code, 200)
        listed = self.client.get('/api/youtube/cookies').json
        self.assertEqual([item['id'] for item in listed['items'] if item['active']], [ids[2]])
        self.assertFalse(listed['items'][0]['pooled'])
        self.assertIsNone(listed['pool'])
        self.assertEqual(yaml.safe_load(self.config.read_text())['youtube_cookie_pool'], [])
        self.assertEqual(self.store.runtime()['youtube_cookie'],
                         str(self.root / f'Cookies/profiles/{ids[2]}.txt'))

    def test_delete_current_runtime_failure_restores_previous_selection(self):
        first = self.add().json['item']['id']
        self.add('Account B')
        self.action('select', first)
        original = self.config.read_bytes()
        runtime = self.store.runtime()
        with patch.object(self.store, 'set_runtime', side_effect=[OSError('fixture failure'), None]):
            self.assertEqual(self.action('delete', first).status_code, 400)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.store.runtime(), runtime)
        listed = self.client.get('/api/youtube/cookies').json
        self.assertEqual(len(listed['items']), 2)
        self.assertEqual([item['id'] for item in listed['items'] if item['active']], [first])

    def test_delete_current_cache_cleanup_failure_keeps_credentials_and_selection(self):
        from pathlib import Path
        from webapp.cookie_profiles import CookieProfiles
        first = self.add().json['item']['id']
        self.add('Account B')
        self.action('select', first)
        profiles = CookieProfiles(self.root, self.store)
        target = profiles.path(first)
        # Simulate a failure after removing the Cookie file but before finishing
        # metadata cleanup. The DB record must remain and restore the old cache.
        metadata = target.with_suffix('.json')
        metadata.write_text('{}')
        original = self.config.read_bytes()
        runtime = self.store.runtime()
        unlink = Path.unlink

        def fail_metadata(path, *args, **kwargs):
            if path == metadata:
                raise PermissionError('fixture failure')
            return unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', fail_metadata):
            self.assertEqual(self.action('delete', first).status_code, 400)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.store.runtime(), runtime)
        self.assertIn(b'PRIVATE_COOKIE_VALUE', target.read_bytes())
        self.assertEqual(len(self.client.get('/api/youtube/cookies').json['items']), 2)

    def test_switch_blocks_busy_but_saving_does_not(self):
        with patch.object(self.store, 'download_connection_busy', return_value=True):
            response = self.add()
            self.assertEqual(response.status_code, 201)
            self.assertEqual(self.action('select', response.json['item']['id']).status_code, 409)

    def test_switch_preserves_verification_block_and_pause(self):
        identity = self.add().json['item']['id']
        auth = self.app.extensions['youtube_auth']
        reason = {'state': 'verification_required', 'message': 'manual verification required'}
        auth.block(reason)
        self.assertEqual(self.action('select', identity).status_code, 200)
        self.assertEqual(auth.block_reason(), reason)
        self.assertTrue(self.store.download_queue()['paused'])

    def test_legacy_preserved_and_selectable(self):
        legacy = self.root / 'Cookies/youtube.txt'
        legacy.parent.mkdir()
        legacy.write_bytes(COOKIE)
        identity = self.add().json['item']['id']
        self.assertEqual(legacy.read_bytes(), COOKIE)
        self.assertEqual(self.action('select', identity).status_code, 200)
        self.assertEqual(self.action('select', 'legacy').status_code, 200)
        self.assertEqual(yaml.safe_load(self.config.read_text())['youtube_cookie'], 'Cookies/youtube.txt')

    def test_invalid_upload_and_path_escape_do_not_change_config(self):
        original = self.config.read_bytes()
        for name, content in [('bad\nname', COOKIE), ('x' * 81, COOKIE), ('valid', b'invalid')]:
            self.assertEqual(self.add(name, content).status_code, 400)
        for identity in ('../../config.yaml', 'missing', None):
            self.assertEqual(self.action('select', identity).status_code, 400)
            self.assertEqual(self.action('delete', identity).status_code, 400)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.client.get('/api/youtube/cookies').json['items'], [])

    def test_expired_selection_does_not_replace_current(self):
        identity = self.add().json['item']['id']
        original = self.config.read_bytes()
        self.replace_content(identity, COOKIE.replace(b'\t0\tSID', b'\t1\tSID'))
        self.assertEqual(self.action('select', identity).status_code, 400)
        self.assertEqual(self.config.read_bytes(), original)

    def test_selection_failure_restores_config(self):
        identity = self.add().json['item']['id']
        original = self.config.read_bytes()
        with patch('app.save_settings', side_effect=OSError('save failed')):
            self.assertEqual(self.action('select', identity).status_code, 400)
        self.assertEqual(self.config.read_bytes(), original)

    def test_csrf_and_instance_rpc_allowlist(self):
        self.assertEqual(self.client.post('/api/youtube/cookies/select', json={'id': 'legacy'}).status_code, 403)
        self.assertEqual(self.client.post('/api/youtube/cookies/check', json={'id': 'legacy'}).status_code, 403)
        for path in ('/api/youtube/cookies', '/api/youtube/cookies/select', '/api/youtube/cookies/delete', '/api/youtube/cookies/check'):
            self.assertIn('POST', LOCAL_RPC_METHODS[path])

    def test_check_other_profile_preserves_selected_account_and_blocks(self):
        first = self.add().json['item']['id']
        second = self.add('Account B', COOKIE.replace(b'PRIVATE_COOKIE_VALUE', b'SECOND_SECRET')).json['item']['id']
        self.action('select', first)
        original = self.config.read_bytes()
        auth = self.app.extensions['youtube_auth']
        before = auth.status()
        reason = {'state': 'verification_required', 'message': 'verification required'}
        auth.block(reason)
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'Login verified')) as probe:
            response = self.action('check', second)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json['auth']['state'], 'valid')
            self.assertIn(b'SECOND_SECRET', probe.call_args.args[0])
            # Repeated clicks reuse the normal connection-specific preflight cache.
            self.action('check', second)
            self.assertEqual(probe.call_count, 1)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(auth.status(), before)
        self.assertEqual(auth.block_reason(), reason)
        self.assertTrue(self.store.download_queue()['paused'])
        listed = self.client.get('/api/youtube/cookies')
        result = next(item for item in listed.json['items'] if item['id'] == second)
        self.assertEqual(result['auth']['state'], 'valid')
        self.assertIn('checked_at', result['auth'])
        self.assertNotIn('SECOND_SECRET', listed.text + response.text)
        self.replace_content(second, COOKIE)
        listed = self.client.get('/api/youtube/cookies').json['items']
        self.assertIsNone(next(item for item in listed if item['id'] == second)['auth'])

    def test_check_uses_current_egress_and_distinguishes_failures(self):
        identity = self.add().json['item']['id']
        config = yaml.safe_load(self.config.read_text())
        config.update(proxy_mode='direct', youtube_source_address='::')
        self.config.write_text(yaml.safe_dump(config))
        with patch('webapp.youtube_auth.probe', return_value=('network_error', 'Network unavailable')) as probe:
            response = self.action('check', identity)
            self.assertEqual(response.json['auth']['state'], 'network_error')
            self.assertEqual(probe.call_args.kwargs['source_address'], '::')
        for invalid in ('../../config.yaml', 'missing', None):
            self.assertEqual(self.action('check', invalid).status_code, 400)

    def test_check_without_auth_credentials_makes_no_network_request(self):
        identity = self.add(content=COOKIE.replace(b'\tSID\t', b'\tPREF\t')).json['item']['id']
        with patch('webapp.youtube_auth.probe') as probe:
            response = self.action('check', identity)
        self.assertEqual(response.json['auth']['state'], 'not_logged_in')
        probe.assert_not_called()

    def test_pool_recovery_checks_every_account_and_preserves_pause(self):
        self.store.set_downloads_paused(True)
        identity = self.add().json['item']['id']
        config = yaml.safe_load(self.config.read_text())
        config['youtube_cookie_pool'] = [identity]
        self.config.write_text(yaml.safe_dump(config))
        from webapp.cookie_pool import CookiePool
        pool = CookiePool(self.store, config, self.root, None)
        def halt(db, stamp):
            pool.router._state(db, pool.global_scope)
            pool.router._save(db, pool.global_scope, {'halted': True})
        pool.router.transaction_call(halt)
        with patch('webapp.youtube_auth.probe', return_value=('not_logged_in', 'Not logged in')):
            response = self.client.post('/api/youtube/cookies/pool/recover', json={}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(pool.status()['halted'])
        with patch('webapp.youtube_auth.probe', return_value=('valid', 'Login verified')):
            response = self.client.post('/api/youtube/cookies/pool/recover', json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(pool.status()['halted'])
        self.assertTrue(self.store.download_queue()['paused'])
        self.assertEqual(self.client.post('/api/youtube/cookies/pool/recover', json={}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
