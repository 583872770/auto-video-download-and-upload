import json
from pathlib import Path
import tempfile
import time
import unittest

from webapp.risk_diagnostics import GUIDE, classify, local_health, observed, read_auth, task_risks
from webapp.concurrency_diagnostics import diagnose
from webapp.store import TaskStore
from webapp.database import settings


class RiskDiagnosticsTests(unittest.TestCase):
    def test_ambiguous_errors_keep_qualified_explanations_and_multiple_causes(self):
        self.assertEqual(classify('Video unavailable'), ['content_unavailable'])
        codes = classify('HTTP Error 403: Sign in to confirm you are not a bot')
        self.assertIn('http_403', codes)
        self.assertIn('bot_check', codes)
        self.assertNotIn('cookie_rotation', codes)
        for message, code in [('HTTP 429', 'http_429'), ('HTTP 503', 'http_503'),
                              ('HTTP 404', 'http_404'), ('connection reset', 'network'),
                              ('租约已失效', 'lease'), ('磁盘预算不足', 'disk_budget'),
                              ('database is locked', 'database'), ('private video', 'content_permission')]:
            self.assertIn(code, classify(message))
        self.assertEqual(classify('unrecognized fixture'), [])
        self.assertEqual(classify('failure https://example.test/403?token=429'), [])
        self.assertNotIn('quota', classify('Disk quota exceeded'))

    def test_retry_status_comes_from_task_state_instead_of_error_guess(self):
        task = dict(action='download', status='failed', error='HTTP 429', attempts=3,
                    max_attempts=3, available_at=0, progress={})
        self.assertFalse(task_risks(task)['retry_scheduled'])
        task.update(status='queued', available_at=time.time()+120, progress={'stage': 'retrying'})
        self.assertTrue(task_risks(task)['retry_scheduled'])
        task.update(action='subtitles')
        self.assertIn('subtitle', task_risks(task)['codes'])
        task.update(action='download', status='failed', error='unknown fixture')
        self.assertEqual(task_risks(task)['codes'], ['unknown'])

    def test_auth_evidence_unknown_and_expired_are_not_invalid_cookie(self):
        stamp = time.time()
        config = {'download_method': 'yt_dlp', 'proxy_mode': 'direct'}
        for auth, expected in [(None, 'auth_unknown'), ({'state': 'network_error'}, 'auth_unknown'),
                               ({'state': 'valid', 'expires_at': stamp-1}, 'auth_unknown'),
                               ({'state': 'valid', 'expires_at': stamp+60}, 'auth_valid'),
                               ({'state': 'verification_required'}, 'bot_check'),
                               ({'state': 'invalid'}, 'auth_missing')]:
            values = observed(config, {}, auth, stamp)
            self.assertIn(expected, [value['code'] for value in values])
        self.assertNotIn('auth_unknown', [v['code'] for v in observed({'download_method':'downmp3'}, {}, None, stamp)])
        self.assertIn('auth_unknown', [v['code'] for v in observed({'download_method':'yt5s_fallback'}, {}, None, stamp)])

    def test_local_route_evidence_is_sanitized_and_stale_is_not_live(self):
        with tempfile.TemporaryDirectory() as root:
            stamp = time.time()
            Path(root, 'route-health.json').write_text(json.dumps({'secret-node-name': {
                'until': stamp+30, 'probe_until':stamp+60, 'providers': {'secret-url': {'until':stamp+90}}}}))
            Path(root, 'log-write-warning.json').write_text(json.dumps({'time':stamp,'message':'secret token'}))
            health = local_health(root, {'proxy_mode':'clash'}, root)
            self.assertEqual((health['cooling'], health['probing']), (1,1))
            self.assertNotIn('secret', json.dumps(health))
            values = observed({'download_method':'downmp3','proxy_mode':'clash'}, {'local_health':health}, None, stamp)
            self.assertIn('telemetry', [v['code'] for v in values])
            health['time'] -= 60
            values = observed({'download_method':'downmp3','proxy_mode':'clash'}, {'local_health':health}, None, stamp)
            self.assertNotIn('telemetry', [v['code'] for v in values])
            self.assertTrue(any('无法确认' in v['detail'] for v in values))

    def test_all_codes_have_impact_recovery_action_and_no_secret_echo(self):
        codes = [row['code'] for row in GUIDE]
        self.assertEqual(len(codes), len(set(codes)))
        for row in GUIDE:
            self.assertTrue(all(row.get(k) for k in ('category','title','impact','recovery','action')))
            self.assertTrue(row['href'].startswith('/#'))
        values = observed({'download_method':'yt_dlp'}, {}, None, time.time(),
                          error='HTTP 403 https://user:password@example.test/?token=secret')
        self.assertNotIn('password', json.dumps(values))
        self.assertNotIn('secret', json.dumps(values))
        self.assertTrue(set(v['code'] for v in values).issubset(codes))

    def test_batch_auth_lookup_scopes_instances_and_omits_cookie_metadata(self):
        import hashlib
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(root)
            try:
                key = 'i:' + hashlib.sha256(b'node').hexdigest()[:16] + ':youtube_auth_latest'
                with store.connect() as db:
                    db.execute(settings.insert().values(key=key, value=json.dumps({'state':'valid',
                        'checked_at':123, 'expires_at':456, 'message':'secret'})))
                    result = read_auth(db, ['node', 'other'])
                self.assertEqual(result, {'node':{'state':'valid','checked_at':123,'expires_at':456}})
            finally:
                store.db.engine.dispose()

    def test_resolver_cooldown_does_not_claim_existing_transfers_are_blocked(self):
        stamp = time.time()
        config = {'download_method':'yt_dlp', 'worker_count':16, 'throughput_mode':'auto'}
        context = ({'resolver:yt_dlp': {'cooldown_until': stamp+50}}, {}, {})
        value = diagnose(config, 'node', [], None, context, stamp)
        self.assertGreater(value['channels'][0]['effective_limit'], 0)
        self.assertEqual(value['channels'][0]['resolver_cooldown_seconds'], 50)
        self.assertTrue(any(r['phase'] == 'resolve' and r['cooldown_seconds'] == 50
                            for r in value['channels'][0]['resources']))

    def test_auto_connection_cooldown_also_blocks_transfer_estimate(self):
        stamp = time.time()
        config = {'download_method':'yt_dlp','worker_count':16,'throughput_mode':'auto'}
        value = diagnose(config, 'node', [], None, ({'connections:youtube-origin':
            {'cooldown_until':stamp+40}}, {}, {}), stamp)
        self.assertEqual(value['effective_limit'], 0)
