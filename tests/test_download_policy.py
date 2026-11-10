import unittest
import errno
from http.client import IncompleteRead
import socket

from webapp.download_policy import failure_evidence, recovery_policy, cooldown_deadline
from webapp.downloads import YouTubeAccountLimited
from webapp.throughput import PipelineBusy
from webapp.youtube_auth import YouTubeAuthRequired


class FailureEvidenceTests(unittest.TestCase):
    def test_connectivity_failures_retry_without_account_or_channel_backoff(self):
        from webapp.downloads import DownloadFailure
        from yt_dlp.networking.exceptions import TransportError

        network = ("HTTP 分块下载失败：连接传输中断（TransportError）："
                   "HTTPSConnection(host='rr5.googlevideo.com', port=443): "
                   "Failed to establish a new connection: [Errno 101] Network is unreachable")
        dns = ("[youtube] fixture: Unable to download API page: "
               "HTTPSConnection(host='www.youtube.com', port=443): Failed to resolve "
               "'www.youtube.com' ([Errno -3] Temporary failure in name resolution) "
               '(caused by TransportError("Temporary failure in name resolution"))')
        for original, reason in ((RuntimeError(network), 'network_unreachable'),
                                 (TransportError(network), 'network_unreachable'),
                                 (OSError(errno.ENETUNREACH, 'fixture'), 'network_unreachable'),
                                 (OSError(errno.EHOSTUNREACH, 'fixture'), 'network_unreachable'),
                                 (RuntimeError(dns), 'dns_resolution'),
                                 (socket.gaierror(socket.EAI_AGAIN, 'fixture'), 'dns_resolution')):
            wrapped = RuntimeError('download failed')
            wrapped.__cause__ = original
            for error in (original, wrapped):
                with self.subTest(error=str(error), reason=reason):
                    value = failure_evidence(error, source='youtube', profile_id='one')
                    self.assertEqual((value.kind, value.reason, value.retryable),
                                     ('transient_failure', reason, True))
                    self.assertIsNone(value.http_status)
                    for config in ({}, {'youtube_cookie_pool': ['one']}):
                        policy = recovery_policy(value, config)
                        self.assertEqual((policy.action, policy.delay, policy.targets), ('none', 0, ()))
        for message, reason in ((network, 'network_unreachable'), (dns, 'dns_resolution')):
            logger = DownloadFailure()
            logger.error(message)
            self.assertEqual(failure_evidence(logger.as_error()).reason, reason)

    def test_connectivity_text_does_not_override_http_or_account_evidence(self):
        detail = 'Failed to resolve host; Network is unreachable'
        for status, kind, action in ((403, 'access_denied', 'none'),
                                     (429, 'request_rate_limit', 'cooldown'),
                                     (503, 'transient_failure', 'retry')):
            error = RuntimeError(detail)
            error.http_status = status
            value = failure_evidence(error)
            self.assertEqual(value.kind, kind)
            self.assertIsNone(value.reason)
            self.assertEqual(recovery_policy(value, {}).action, action)
        value = failure_evidence(YouTubeAccountLimited(detail))
        self.assertEqual(value.kind, 'account_rate_limit')
        self.assertEqual(recovery_policy(value, {}).action, 'halt')
        self.assertEqual(failure_evidence(RuntimeError('failure https://example.test/failed-to-resolve')).kind,
                         'unknown')

    def test_only_attributed_pool_limits_are_isolated(self):
        config = {'youtube_cookie_pool': ['one'], 'youtube_throttle_cooldown': 300}
        unknown = failure_evidence(YouTubeAccountLimited('Your account has been rate-limited'))
        self.assertTrue(recovery_policy(unknown, config).manual)
        for error, expected in ((YouTubeAccountLimited('limited'), 3600),
                                (RuntimeError('HTTP Error 429'), 720)):
            error.headers = {'Retry-After': '720'}
            evidence = failure_evidence(error, profile_id='one', profile_name='Account A')
            policy = recovery_policy(evidence, config)
            self.assertEqual((policy.action, policy.delay, policy.manual, policy.targets),
                             ('cooldown', expected, False, ('session',)))
        self.assertTrue(recovery_policy(evidence, {}).targets != ('session',))

    def test_account_limit_does_not_manufacture_http_status(self):
        error = YouTubeAccountLimited('Your account has been rate-limited')
        value = failure_evidence(error, phase='resolve', source='youtube')
        self.assertEqual(value.kind, 'account_rate_limit')
        self.assertIsNone(value.http_status)
        self.assertEqual(recovery_policy(value, {}).action, 'halt')
        error.http_status = 200
        # An independently observed response can report an account limit in HTTP 200.
        second = YouTubeAccountLimited(str(error))
        second.http_status = 200
        self.assertEqual(failure_evidence(second).http_status, 200)

    def test_real_http_evidence_and_retry_after_survive_wrapping(self):
        cause = RuntimeError('request rejected')
        cause.http_status = 429
        cause.headers = {'Retry-After': '720'}
        outer = RuntimeError('download failed')
        outer.__cause__ = cause
        value = failure_evidence(outer, phase='subtitles', source='youtube')
        self.assertEqual((value.kind, value.http_status, value.retry_after), ('request_rate_limit', 429, 720))
        self.assertEqual(value.evidence, 'http_response')
        self.assertFalse(recovery_policy(value, {}).manual)

    def test_local_halt_and_resource_messages_cannot_become_remote_limits(self):
        halt = YouTubeAuthRequired(dict(state='verification_required', source='cookie_pool_halted',
            message='Cookie 池沿用此前保存的停止状态：Your account has been rate-limited HTTP 429'))
        for error, kind in ((halt, 'local_halt'), (PipelineBusy('HTTP 429 quota wait'), 'local_resource')):
            value = failure_evidence(error)
            self.assertEqual(value.kind, kind)
            self.assertEqual(value.source, 'local')
            self.assertEqual(recovery_policy(value, {}).action, 'none')

    def test_http_codes_inside_urls_and_secrets_do_not_classify(self):
        value = failure_evidence(RuntimeError('failure https://u:SECRET@example.test/HTTP/429?token=SECRET'))
        self.assertEqual(value.kind, 'unknown')
        self.assertIsNone(value.http_status)
        self.assertNotIn('SECRET', value.message)

    def test_content_denial_and_unknown_are_not_request_throttles(self):
        for message, kind in (('HTTP Error 403: Forbidden', 'access_denied'),
                              ('Requested format is not available', 'content_restriction'),
                              ('private video', 'content_restriction'), ('unrecognized fixture', 'unknown')):
            value = failure_evidence(RuntimeError(message))
            self.assertEqual(value.kind, kind)
            self.assertEqual(recovery_policy(value, {}).action, 'none')

    def test_cached_incident_is_enriched_and_retained_across_exception_wrapper(self):
        error = RuntimeError('HTTP Error 429')
        first = failure_evidence(error)
        outer = RuntimeError('wrapped')
        outer.__cause__ = error
        second = failure_evidence(outer, phase='transfer', source='youtube')
        self.assertEqual(first.incident_id, second.incident_id)
        self.assertEqual((second.source, second.phase), ('youtube', 'transfer'))
        self.assertEqual(cooldown_deadline(error, 100, 300), 400)
        self.assertEqual(cooldown_deadline(error, 120, 300), 400)
        self.assertEqual(cooldown_deadline(error, 120, 600), 700)

    def test_incomplete_response_survives_downloader_message_wrapping(self):
        from webapp.downloads import DownloadFailure
        from yt_dlp.networking.exceptions import IncompleteRead as YtdlpIncompleteRead

        failure = DownloadFailure()
        failure.error('[download] Got error: 215403 bytes read, 9758503 more expected. Giving up after 2 retries')
        for error in (failure.as_error(), IncompleteRead(b'partial', 9758503),
                      YtdlpIncompleteRead(215403, 9758503), YtdlpIncompleteRead(215403)):
            with self.subTest(error=type(error).__name__):
                value = failure_evidence(error, phase='transfer', source='youtube')
                self.assertEqual(value.kind, 'transient_failure')
                self.assertTrue(value.retryable)
                self.assertIsNone(value.http_status)
                policy = recovery_policy(value, {})
                self.assertEqual((policy.action, policy.delay, policy.manual), ('none', 0, False))

    def test_partial_response_does_not_override_restrictions_or_local_failures(self):
        detail = '215403 bytes read, 9758503 more expected'
        for error, expected in (
                (RuntimeError('HTTP Error 403: Forbidden; ' + detail), 'access_denied'),
                (RuntimeError('HTTP Error 429: Too Many Requests; ' + detail), 'request_rate_limit'),
                (RuntimeError('Requested format is not available; ' + detail), 'content_restriction'),
                (RuntimeError('Sign in to confirm you are not a bot; ' + detail), 'verification_required'),
                (PipelineBusy(detail), 'local_resource')):
            with self.subTest(expected=expected):
                self.assertEqual(failure_evidence(error).kind, expected)
        value = failure_evidence(RuntimeError('failure https://example.test/215403 bytes?token=read'))
        self.assertEqual(value.kind, 'unknown')
        self.assertIsNone(value.http_status)

    def test_incomplete_response_without_expected_length_remains_retryable(self):
        from webapp.downloads import DownloadFailure
        from yt_dlp.networking.exceptions import IncompleteRead as YtdlpIncompleteRead

        diagnostic = f'[download] Got error: {YtdlpIncompleteRead(231816)}. Giving up after 2 retries'
        failure = DownloadFailure()
        failure.error(diagnostic)
        for error in (failure.as_error(), RuntimeError(diagnostic)):
            with self.subTest(error=str(error)):
                evidence = failure_evidence(error, phase='transfer', source='youtube')
                self.assertEqual((evidence.kind, evidence.reason, evidence.retryable),
                                 ('transient_failure', 'incomplete_response', True))
                self.assertIsNone(evidence.http_status)
        # Bounded retries must still respect restrictions and local exceptions.
        for prefix, expected in (('HTTP Error 403: ', 'access_denied'),
                                 ('HTTP Error 429: ', 'request_rate_limit'),
                                 ('Requested format is not available: ', 'content_restriction'),
                                 ('Sign in to confirm you are not a bot: ', 'verification_required')):
            with self.subTest(prefix=prefix):
                self.assertEqual(failure_evidence(RuntimeError(prefix + diagnostic)).kind, expected)
        self.assertEqual(failure_evidence(PipelineBusy(diagnostic)).kind, 'local_resource')

    def test_ordinary_byte_counts_and_urls_do_not_become_incomplete_responses(self):
        for message in ('231816 bytes read', 'Downloaded 231816 bytes read successfully.',
                        '[download] 231816 bytes read. Giving up after 2 retries',
                        'failure https://example.test/[download]GotError?message=231816',
                        'cookie: [download] Got error: 231816 bytes read. Giving up after 2 retries'):
            with self.subTest(message=message):
                self.assertEqual(failure_evidence(RuntimeError(message)).kind, 'unknown')

    def test_typed_transport_failure_is_retryable_without_message_keyword(self):
        from yt_dlp.networking.exceptions import TransportError
        value = failure_evidence(TransportError('connection interrupted by peer'))
        self.assertEqual(value.kind, 'transient_failure')
        self.assertTrue(value.retryable)
        self.assertIsNone(value.http_status)

    def test_suppressed_exception_context_is_not_evidence(self):
        error = RuntimeError('unrelated')
        error.__context__ = YouTubeAccountLimited('Your account has been rate-limited')
        error.__suppress_context__ = True
        self.assertEqual(failure_evidence(error).kind, 'unknown')
