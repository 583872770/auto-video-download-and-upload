"""A misleading authenticated response gets one isolated public-video retry."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yt_dlp

from web_worker import execute


class CookieFallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cookie = self.root / 'youtube.txt'
        self.original = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture\n'
        self.cookie.write_bytes(self.original)
        self.payload = {'base': str(self.root), 'urls': ['https://www.youtube.com/watch?v=u71MaudlmOc'],
                        'config': {'path': 'videos', 'proxy_mode': 'direct', 'throughput_mode': 'manual',
                                   'youtube_cookie': str(self.cookie)}}

    def test_cookie_unavailable_retries_once_without_mutating_uploaded_cookie_or_double_counting(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                calls = []
                def download(downloader, urls):
                    calls.append(dict(downloader.params))
                    if len(calls) == 1:
                        temporary = Path(downloader.params['cookiefile'])
                        self.assertNotEqual(temporary, self.cookie)
                        temporary.write_text('# Netscape HTTP Cookie File\n', encoding='utf-8')
                        if raises:
                            raise yt_dlp.utils.DownloadError('[youtube] u71MaudlmOc: Video unavailable')
                        downloader.report_error('[youtube] u71MaudlmOc: Video unavailable')
                        return 1
                    return 0
                meter = Mock(resolve_seconds=0)
                with patch.object(yt_dlp.YoutubeDL, 'download', download), \
                        patch('webapp.download_metrics.meter_for', return_value=meter):
                    result = execute('download', self.payload)
                self.assertEqual(len(calls), 2)
                self.assertNotIn('cookiefile', calls[1])
                self.assertEqual(calls[0]['proxy'], calls[1]['proxy'])
                self.assertEqual(calls[0]['format'], calls[1]['format'])
                self.assertFalse(Path(calls[0]['cookiefile']).exists())
                self.assertEqual(self.cookie.read_bytes(), self.original)
                self.assertEqual(result['download_method'], 'yt_dlp')
                self.assertFalse(result['fallback_used'])
                meter.begin.assert_called_once()
                meter.finish.assert_called_once_with(True)

    def test_anonymous_failure_is_returned_without_another_retry(self):
        calls = []
        def download(downloader, urls):
            calls.append(bool(downloader.params.get('cookiefile')))
            downloader.report_error('[youtube] u71MaudlmOc: Video unavailable')
            return 1
        with patch.object(yt_dlp.YoutubeDL, 'download', download):
            with self.assertRaisesRegex(RuntimeError, 'Video unavailable'):
                execute('download', self.payload)
        self.assertEqual(calls, [True, False])

    def test_automatic_pipeline_retries_suppressed_extraction_error_before_transfer(self):
        self.payload['config']['throughput_mode'] = 'auto'
        calls = []
        def extract(downloader, url, **kwargs):
            calls.append(bool(downloader.params.get('cookiefile')))
            if len(calls) == 1:
                downloader.report_error('[youtube] u71MaudlmOc: Video unavailable')
                return None
            return {'id': 'u71MaudlmOc', 'title': 'Fixture', 'ext': 'mp4'}
        with patch.object(yt_dlp.YoutubeDL, 'extract_info', extract), \
                patch.object(yt_dlp.YoutubeDL, 'process_ie_result') as transfer:
            execute('download', self.payload)
        self.assertEqual(calls, [True, False])
        transfer.assert_called_once()

    def test_video_unavailable_without_uploaded_cookies_does_not_retry(self):
        self.payload['config'].pop('youtube_cookie')
        calls = []
        def download(downloader, urls):
            calls.append(True)
            downloader.report_error('[youtube] u71MaudlmOc: Video unavailable')
            return 1
        with patch.object(yt_dlp.YoutubeDL, 'download', download):
            with self.assertRaisesRegex(RuntimeError, 'Video unavailable'):
                execute('download', self.payload)
        self.assertEqual(len(calls), 1)

    def test_other_errors_rejected_cookies_and_partial_transfers_do_not_retry(self):
        for reason in ('HTTP Error 403: Forbidden', 'Sign in to confirm your age', 'rejected', 'partial'):
            with self.subTest(reason=reason):
                calls = []
                def download(downloader, urls):
                    calls.append(True)
                    if reason == 'rejected':
                        downloader.report_warning('The provided YouTube account cookies are no longer valid.')
                    if reason == 'partial':
                        for hook in downloader.params['progress_hooks']:
                            hook({'status': 'downloading', 'filename': 'video.mp4', 'downloaded_bytes': 100,
                                  'info_dict': {'ext': 'mp4'}})
                    downloader.report_error('[youtube] u71MaudlmOc: ' + (
                        'Video unavailable' if reason in ('rejected', 'partial') else reason))
                    return 1
                with patch.object(yt_dlp.YoutubeDL, 'download', download):
                    with self.assertRaises(RuntimeError):
                        execute('download', self.payload)
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.cookie.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
