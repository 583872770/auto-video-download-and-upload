"""Real HTTP integration: bounded parallel ranges must preserve every byte."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yt_dlp

from webapp.downloader import build_ydl_options
from webapp.downloads import DownloadLeaseLost
from webapp.parallel_http import ParallelYoutubeDL, ParallelHttpFD


CHUNK = 64 * 1024
DATA = bytes(range(251)) * 3000 + b'last partial chunk'


@contextmanager
def source(mode='ranges', *, size=CHUNK):
    state = dict(active=0, peak=0, calls=[], counts=Counter(), if_ranges=[],
                 fixture_headers=[], stopped=threading.Event(), half_sent=threading.Event(),
                 continue_range=threading.Event())
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            header = self.headers.get('Range', '')
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', header)
            start = int(match[1]) if match else 0
            end = min(int(match[2]) if match and match[2] else len(DATA)-1, len(DATA)-1)
            probe = match and start == end == 0
            with lock:
                state['calls'].append(header)
                state['counts'][start, end] += 1
                call = state['counts'][start, end]
                state['if_ranges'].append(self.headers.get('If-Range'))
                state['fixture_headers'].append(self.headers.get('X-Fixture'))
                if not probe:
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
            try:
                if not probe:
                    # Allow independent requests to overlap, and make the first
                    # block slower so results arrive in a different order.
                    time.sleep(.045 if start == 0 else .015)
                if not probe and (mode in ('throttle', 'unavailable', 'forbidden')
                        or mode == 'recovery-throttle' and start == 0 and call >= 2):
                    self.send_response(403 if mode == 'forbidden' else 503 if mode == 'unavailable' else 429)
                    self.send_header('Retry-After', '30')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                invalid = mode in ('wrong-range', 'changed') and not probe and end-start+1 == size
                serial = mode == 'ignore'
                if mode == 'reject-parallel' and not probe and end-start+1 == size:
                    # Probe succeeded, but the subsequent source ignores Range.
                    serial = True
                response_start, response_end = (0, len(DATA)-1) if serial else (start, end)
                content = DATA[response_start:response_end+1]
                status = 200 if serial or not match else 206
                self.send_response(status)
                if status == 206:
                    reported_start = start + 1 if invalid and mode == 'wrong-range' else start
                    self.send_header('Content-Range', f'bytes {reported_start}-{end}/{len(DATA)}')
                self.send_header('Content-Length', str(len(content)))
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('ETag', '"changed"' if invalid and mode == 'changed' else '"fixture"')
                self.end_headers()
                if mode == 'retry' and not probe and start == size and call == 1:
                    content = content[:len(content)//2]
                if mode == 'always-short' and not probe:
                    content = content[:len(content)//2]
                if mode == 'recovery-throttle' and not probe and start == size and call <= 2:
                    content = content[:len(content)//2]
                if mode == 'read-timeout' and not probe and start == size and call <= 2:
                    state['stopped'].wait(.6)
                if mode == 'streaming' and not probe and start == 0:
                    half = len(content)//2
                    self.wfile.write(content[:half]);self.wfile.flush()
                    state['half_sent'].set()
                    state['continue_range'].wait(5)
                    self.wfile.write(content[half:]);self.wfile.flush()
                elif mode == 'slow' and not probe:
                    for offset in range(0, len(content), 4096):
                        self.wfile.write(content[offset:offset+4096])
                        self.wfile.flush()
                        if state['stopped'].wait(.02):
                            break
                else:
                    self.wfile.write(content)
                    self.wfile.flush()
            except (ConnectionError, OSError):
                pass
            finally:
                if not probe:
                    with lock:
                        state['active'] -= 1

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/video.mp4', state
    finally:
        state['stopped'].set()
        state['continue_range'].set()
        server.shutdown()
        server.server_close()
        thread.join()


class ParallelHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'video.mp4'

    def download(self, url, *, concurrency=3, hook=None, overrides=None, info_overrides=None, subtitle=False):
        options = build_ydl_options({'fragment_concurrency': concurrency,
            'youtube_sleep_preset': False, 'proxy_mode': 'direct'}, Path(self.temp.name))
        options.update(quiet=True, noprogress=True, http_chunk_size=CHUNK, retries=0,
            fragment_retries=1, socket_timeout=2, progress_hooks=[hook] if hook else [])
        options.update(overrides or {})
        info = dict(url=url, protocol='http', filesize=len(DATA), ext='mp4',
            format_id='fixture', vcodec='avc1', acodec='none', http_headers={'X-Fixture': 'retained'})
        info.update(info_overrides or {})
        with ParallelYoutubeDL(options) as downloader:
            return downloader.dl(str(self.path), info, subtitle=subtitle)

    def assert_file(self):
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), hashlib.sha256(DATA).digest())
        self.assertFalse(Path(str(self.path) + '.part').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX advisory file locks')
    def test_competing_range_writer_cannot_truncate_locked_partial(self):
        self.path.write_bytes(DATA[:CHUNK])
        with ParallelYoutubeDL({'quiet':True}) as downloader:
            fd = ParallelHttpFD(downloader, {'file_access_retries':0})
            stream, _ = fd.open_range_file(str(self.path), CHUNK)
            try:
                with self.assertRaises(OSError):
                    fd.open_range_file(str(self.path), 0)
                self.assertEqual(self.path.read_bytes(), DATA[:CHUNK])
            finally:
                stream.close()

    def test_ranges_overlap_within_limit_and_assemble_out_of_order_results(self):
        values = []
        with source() as (url, state):
            self.assertTrue(self.download(url, hook=lambda value: values.append(dict(value)))[0])
        self.assert_file()
        self.assertGreaterEqual(state['peak'], 2)
        self.assertLessEqual(state['peak'], 3)
        self.assertIn('"fixture"', state['if_ranges'])
        self.assertTrue(all(value == 'retained' for value in state['fixture_headers']))
        # No range is requested twice, and the short final range is retained.
        self.assertEqual(max(state['counts'].values()), 1)
        self.assertIn(f'bytes={(len(DATA)//CHUNK)*CHUNK}-{len(DATA)-1}', state['calls'])
        counters = [value['downloaded_bytes'] for value in values]
        self.assertEqual(counters, sorted(counters))
        self.assertEqual(values[-1]['status'], 'finished')
        self.assertEqual(counters[-1], len(DATA))

    def test_range_is_written_before_the_whole_response_is_buffered(self):
        # The server withholds the second half until the first half reaches
        # disk. Retaining a complete range in a Future would fail this check.
        chunk = 512 * 1024
        observed = []
        with source('streaming', size=chunk) as (url, state):
            def hook(value):
                if not state['half_sent'].is_set() or state['continue_range'].is_set():
                    return
                partial = Path(str(self.path) + '.part')
                observed.append(partial.stat().st_size)
                state['continue_range'].set()
            self.assertTrue(self.download(url, hook=hook, overrides={'http_chunk_size':chunk})[0])
        self.assertTrue(observed)
        self.assertGreater(observed[0], 0)
        self.assert_file()

    def test_slow_front_range_refills_connections_inside_bounded_window(self):
        with source('streaming') as (url, state), ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(self.download, url)
            try:
                self.assertTrue(state['half_sent'].wait(2))
                deadline = time.monotonic() + 1.5
                while len(state['calls']) < 7 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertIn(f'bytes={3*CHUNK}-{4*CHUNK-1}', state['calls'])
                self.assertNotIn(f'bytes={6*CHUNK}-{7*CHUNK-1}', state['calls'])
            finally:
                state['continue_range'].set()
            self.assertTrue(task.result(timeout=5)[0])
        self.assert_file()

    def test_concurrency_one_keeps_native_sequential_downloader(self):
        with source() as (url, state):
            self.assertTrue(self.download(url, concurrency=1)[0])
        self.assert_file()
        self.assertNotIn('bytes=0-0', state['calls'])
        self.assertEqual(state['peak'], 1)

    def test_real_yt_dlp_format_selection_and_file_pipeline_use_parallel_downloader(self):
        options = build_ydl_options({'fragment_concurrency': 3,
            'youtube_sleep_preset': False, 'proxy_mode': 'direct'}, Path(self.temp.name))
        options.update(quiet=True, noprogress=True, http_chunk_size=CHUNK,
            outtmpl=str(self.path), format='best', fixup='never', writethumbnail=False,
            writeinfojson=False, writesubtitles=False, writeautomaticsub=False, download_archive=None)
        with source() as (url, state), ParallelYoutubeDL(options) as downloader:
            downloader.process_ie_result(dict(id='fixture', title='Fixture', extractor='fixture',
                formats=[dict(url=url, protocol='http', format_id='fixture', ext='mp4',
                    filesize=len(DATA), vcodec='avc1', acodec='mp4a.40.2')]), download=True)
            self.assertEqual(downloader._download_retcode, 0)
        self.assert_file()
        self.assertGreaterEqual(state['peak'], 2)

    def test_range_ignored_during_probe_falls_back_without_duplicate_content(self):
        with source('ignore') as (url, state):
            self.assertTrue(self.download(url)[0])
        self.assert_file()
        self.assertEqual(state['calls'][0], 'bytes=0-0')

    def test_incompatible_worker_response_discards_partial_and_falls_back(self):
        # Larger native chunks distinguish the fallback requests from the
        # parallel requests; invalid responses must not be appended as chunks.
        from unittest.mock import patch
        from yt_dlp.downloader.http import HttpFD
        native = HttpFD.real_download

        def fallback(fd, filename, info):
            fd.params = {**fd.params, 'http_chunk_size': CHUNK * 2}
            return native(fd, filename, info)

        for mode in ('reject-parallel', 'wrong-range', 'changed'):
            with self.subTest(mode=mode):
                self.path.unlink(missing_ok=True)
                with source(mode) as (url, state), patch.object(HttpFD, 'real_download', fallback):
                    self.assertTrue(self.download(url)[0])
                self.assert_file()

    def test_truncated_range_is_retried_and_output_is_complete(self):
        with source('retry') as (url, state):
            self.assertTrue(self.download(url)[0])
        self.assert_file()
        self.assertEqual(state['counts'][CHUNK, 2*CHUNK-1], 2)

    def test_failed_range_never_publishes_a_complete_file(self):
        values = []
        with source('always-short') as (url, state):
            with self.assertRaises(yt_dlp.utils.DownloadError):
                self.download(url, hook=lambda value: values.append(dict(value)), overrides={'ignoreerrors': False})
        self.assertFalse(self.path.exists())
        self.assertNotIn('finished', [value['status'] for value in values])
        # Original range retries, followed by one single-connection attempt.
        self.assertLessEqual(max(state['counts'].values()), 4)

    def test_read_timeouts_recover_once_with_one_connection_and_valid_bytes(self):
        from unittest.mock import patch
        from webapp.parallel_http import ParallelHttpFD
        original = ParallelHttpFD._parallel_download
        attempts = []

        def download(fd, filename, info):
            attempts.append(fd.params['concurrent_fragment_downloads'])
            return original(fd, filename, info)

        with source('read-timeout') as (url, state), patch.object(ParallelHttpFD, '_parallel_download', download):
            self.assertTrue(self.download(url, overrides={'socket_timeout': .2})[0])
        self.assert_file()
        self.assertEqual(attempts, [3, 1])
        self.assertEqual(state['counts'][CHUNK, 2*CHUNK-1], 3)

    def test_probe_transport_failure_has_one_recovery_and_no_published_error(self):
        from unittest.mock import patch
        from yt_dlp.networking.exceptions import TransportError
        options = build_ydl_options({'youtube_sleep_preset': False}, Path(self.temp.name))
        options.update(quiet=True, noprogress=True, http_chunk_size=CHUNK, proxy='',
                       retries=0, fragment_retries=1)
        with source() as (url, state), ParallelYoutubeDL(options) as ydl:
            original = ydl.urlopen
            failed = False

            def urlopen(request):
                nonlocal failed
                if not failed:
                    failed = True
                    raise TransportError('Read timed out')
                return original(request)

            with patch.object(ydl, 'urlopen', urlopen):
                self.assertTrue(ydl.dl(str(self.path), dict(url=url, protocol='http', filesize=len(DATA)))[0])
            self.assertEqual(ydl._download_retcode, 0)
            self.assertFalse(hasattr(ydl, 'http_range_error'))
        self.assert_file()
        self.assertEqual(state['peak'], 1)

    def test_recovery_http_limit_preserves_status_and_retry_after(self):
        from webapp.request_scheduler import throttle_details
        options = build_ydl_options({'youtube_sleep_preset': False}, Path(self.temp.name))
        options.update(quiet=True, noprogress=True, http_chunk_size=CHUNK, proxy='',
                       retries=0, fragment_retries=1, concurrent_fragment_downloads=3)
        with source('recovery-throttle') as (url, state), ParallelYoutubeDL(options) as ydl:
            self.assertFalse(ydl.dl(str(self.path), dict(url=url, protocol='http', filesize=len(DATA)))[0])
            self.assertEqual(throttle_details(ydl.http_range_error), (429, 30))
        self.assertFalse(self.path.exists())
        self.assertEqual(state['counts'][0, CHUNK-1], 2)

    def test_transport_failure_message_is_specific_and_redacts_credentials(self):
        from unittest.mock import patch
        from yt_dlp.networking.exceptions import TransportError
        from webapp.downloads import DownloadFailure
        from webapp.download_policy import failure_evidence
        failure = DownloadFailure()
        options = build_ydl_options({'youtube_sleep_preset': False}, Path(self.temp.name))
        options.update(quiet=True, http_chunk_size=CHUNK, logger=type('Logger', (), {'debug': lambda *args: None,
            'warning': lambda *args: None, 'error': lambda _, msg: failure.error(msg)})())
        with ParallelYoutubeDL(options) as ydl:
            with patch.object(ydl, 'urlopen', side_effect=TransportError('Read timed out https://user:SECRET@example.test/file?token=SECRET')) as opened:
                self.assertFalse(ydl.dl(str(self.path), dict(url='https://example.test/file', protocol='http', filesize=len(DATA)))[0])
            self.assertEqual(opened.call_count, 2)
            self.assertIn('连接读取超时', failure.message())
            self.assertNotIn('SECRET', failure.message())
            evidence = failure_evidence(failure.as_error(ydl.http_range_error))
            self.assertEqual(evidence.kind, 'transient_failure')
            self.assertTrue(evidence.retryable)

    def test_certificate_failures_are_not_retried_as_connection_recovery(self):
        from unittest.mock import patch
        from yt_dlp.networking.exceptions import CertificateVerifyError
        options = build_ydl_options({'youtube_sleep_preset': False}, Path(self.temp.name))
        options.update(quiet=True, noprogress=True, http_chunk_size=CHUNK)
        with ParallelYoutubeDL(options) as ydl:
            with patch.object(ydl, 'urlopen', side_effect=CertificateVerifyError('certificate verify failed')) as opened:
                self.assertFalse(ydl.dl(str(self.path), dict(url='https://example.test/file', protocol='http', filesize=len(DATA)))[0])
            self.assertEqual(opened.call_count, 1)

    def test_throttle_is_preserved_and_does_not_start_serial_fallback(self):
        for mode, status in (('throttle', 429), ('forbidden', 403)):
            with self.subTest(status=status), source(mode) as (url, state):
                with self.assertRaisesRegex(yt_dlp.utils.DownloadError, f'HTTP Error {status}'):
                    self.download(url, overrides={'ignoreerrors': False})
                self.assertFalse(self.path.exists())
                self.assertEqual(max(state['counts'].values()), 1)
                self.assertLessEqual(len(state['calls']), 4)

    def test_resume_only_requests_bytes_after_contiguous_partial(self):
        resume = CHUNK + 123
        Path(str(self.path) + '.part').write_bytes(DATA[:resume])
        with source() as (url, state):
            self.assertTrue(self.download(url)[0])
        self.assert_file()
        offsets = [int(re.search(r'bytes=(\d+)', header)[1]) for header in state['calls'][1:]]
        self.assertTrue(all(offset >= resume for offset in offsets))

    def test_restart_discards_old_partial(self):
        Path(str(self.path) + '.part').write_bytes(b'old incorrect bytes')
        with source() as (url, state):
            self.assertTrue(self.download(url, overrides={'continuedl': False})[0])
        self.assert_file()

    def test_cancel_from_progress_stops_workers_and_leaves_only_contiguous_partial(self):
        def cancel(value):
            if value['downloaded_bytes']:
                raise DownloadLeaseLost('fixture cancelled')

        with source('slow') as (url, state):
            with self.assertRaises(DownloadLeaseLost):
                self.download(url, hook=cancel)
            counts = len(state['calls'])
            time.sleep(.15)
            self.assertEqual(len(state['calls']), counts)
        self.assertFalse(self.path.exists())
        partial = Path(str(self.path) + '.part').read_bytes()
        self.assertEqual(partial, DATA[:len(partial)])

    def test_live_unknown_sizes_and_third_party_use_native_path(self):
        for info, options in (({'is_live': True}, {}), ({'filesize': None}, {}),
                              ({}, {'_parallel_http': False}), ({}, {'ratelimit': 100*1024**2})):
            with self.subTest(info=info, options=options):
                self.path.unlink(missing_ok=True)
                with source() as (url, state):
                    self.assertTrue(self.download(url, info_overrides=info, overrides=options)[0])
                self.assert_file()
                self.assertNotIn('bytes=0-0', state['calls'])

    def test_subtitles_keep_native_downloader(self):
        with source() as (url, state):
            self.assertTrue(self.download(url, subtitle=True)[0])
        self.assert_file()
        self.assertNotIn('bytes=0-0', state['calls'])

    def test_subtitles_inherit_common_file_start_sleep_and_can_disable_it(self):
        for minimum, maximum, seconds in ((10, 20, 12.5), (0, 0, 0)):
            with self.subTest(interval=(minimum, maximum)):
                self.path.unlink(missing_ok=True)
                with source() as (url, state), \
                     patch('webapp.parallel_http.random.uniform', return_value=seconds) as choose, \
                     patch('yt_dlp.downloader.common.time', wraps=time) as clock:
                    clock.sleep.return_value=None
                    self.assertTrue(self.download(url, subtitle=True, overrides={
                        'sleep_interval': minimum, 'max_sleep_interval': maximum})[0])
                choose.assert_called_once_with(minimum, maximum)
                if seconds:
                    clock.sleep.assert_called_once_with(seconds)
                else:
                    clock.sleep.assert_not_called()
                self.assert_file()

    def test_retry_after_survives_ignored_download_error_for_scheduler_cooldown(self):
        from webapp.request_scheduler import throttle_details
        options = build_ydl_options({'youtube_sleep_preset': False}, Path(self.temp.name))
        options.update(quiet=True, noprogress=True, http_chunk_size=CHUNK, proxy='',
                       retries=0, fragment_retries=1)
        with source('unavailable') as (url, state), ParallelYoutubeDL(options) as downloader:
            result = downloader.dl(str(self.path), dict(url=url, protocol='http', filesize=len(DATA)))
            self.assertFalse(result[0])
            self.assertEqual(throttle_details(downloader.http_range_error), (503, 30))
        self.assertFalse(self.path.exists())
        self.assertEqual(max(state['counts'].values()), 1)


if __name__ == '__main__':
    unittest.main()
