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
    def test_saved_pool_halt_is_not_evidence_of_explicit_account_limit(self):
        old = '账号池检测到限流或登录验证，已停止整个实例。请检查后人工恢复；不会换账号继续重试。'
        replay = 'Cookie 池沿用此前保存的停止状态，本次尚未选择 Cookie 或发起视频下载请求。'
        for message in (old, replay):
            self.assertEqual(classify(message), ['cookie_pool_halted'])
            values = observed({'download_method': 'yt_dlp'}, {}, None, time.time(), error=message)
            self.assertIn('cookie_pool_halted', [v['code'] for v in values])
            self.assertNotIn('account_rate_limit', [v['code'] for v in values])
        for message in ('Your account has been rate-limited',
                        'YouTube 明确提示账号被限流，实例已停止领取，任务保留。'):
            self.assertIn('account_rate_limit', classify(message))
        self.assertNotIn('account_rate_limit', classify('账号登录验证未通过，请检查限流状态'))

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
        task['progress'].update(output_kind='subtitle',last_failure={'phase': 'transfer', 'kind': 'request_rate_limit', 'http_status': 429})
        self.assertIn('subtitle', task_risks(task)['codes'])
        task.update(status='failed', error='unknown fixture', progress={})
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

    def test_verification_notice_distinguishes_recorded_causes(self):
        cases = [
            ({'source': 'download', 'request_mode': 'anonymous_fallback'},
             '不带 Cookie 重试时遇到机器人验证', 'Video unavailable'),
            ({'http_status': 403}, '登录检查被 YouTube 拒绝（HTTP 403）', 'HTTP 403'),
            ({'verification_reason': 'redirect'}, '登录检查跳转到了其他页面', 'YouTube 以外'),
            ({'source': 'download'}, '下载时登录或验证未通过', '下载器记录'),
            ({}, '上次登录或访问验证未通过', '没有记录具体触发经过'),
        ]
        for fields, title, detail in cases:
            with self.subTest(fields=fields):
                facts = observed({'download_method': 'yt_dlp'}, {},
                                 {'state': 'verification_required', **fields}, time.time())
                fact = next(f for f in facts if f['code'] == 'bot_check')
                self.assertEqual(fact['title'], title)
                self.assertIn(detail, fact['detail'])
                self.assertIn('历史记录', fact['record_note'])
                self.assertIn('重新检测', fact['action'])
                self.assertIn('HTTP 状态：403' if fields.get('http_status') == 403 else 'HTTP 状态：未记录',
                              fact['evidence'])
                self.assertNotIn('Cloudflare', json.dumps(fact, ensure_ascii=False))

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

    def test_saved_anonymous_failure_reaches_workbench_without_losing_attribution(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(root)
            try:
                saved = {'state': 'verification_required', 'checked_at': 1793936259,
                         'source': 'download', 'request_mode': 'anonymous_fallback',
                         'message': 'SECRET', 'node': 'SECRET', 'connection': 'SECRET',
                         'cookie': 'SECRET'}
                with store.connect() as db:
                    db.execute(settings.insert().values(key='youtube_auth_latest', value=json.dumps(saved)))
                    auth = read_auth(db, ['local'], standalone=True)['local']
                facts = observed({'download_method': 'yt_dlp'}, {}, auth, time.time())
                fact = next(f for f in facts if f['code'] == 'bot_check')
                self.assertIn('不带 Cookie 重试', fact['title'])
                self.assertIn('不能证明', fact['impact'])
                self.assertIn('未记录', fact['evidence'])
                self.assertNotIn('SECRET', json.dumps({'auth': auth, 'facts': facts}))
                # A later successful check replaces the historical warning.
                auth.update(state='valid', expires_at=time.time()+300)
                facts = observed({'download_method': 'yt_dlp'}, {}, auth, time.time())
                self.assertNotIn('bot_check', [f['code'] for f in facts])
                self.assertIn('auth_valid', [f['code'] for f in facts])
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

    def test_local_connection_cooldown_also_blocks_transfer_estimate(self):
        stamp = time.time()
        config = {'download_method':'yt_dlp','worker_count':16,'throughput_mode':'auto'}
        value = diagnose(config, 'node', [], None, ({'local-connections:node':
            {'cooldown_until':stamp+40}}, {}, {}), stamp)
        self.assertEqual(value['effective_limit'], 0)
