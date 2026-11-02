"""Real HTTP integration: bounded parallel ranges must preserve every byte."""
from collections import Counter
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest

import yt_dlp

from webapp.downloader import build_ydl_options
from webapp.downloads import DownloadLeaseLost
from webapp.parallel_http import ParallelYoutubeDL, fragment_connection_budget, transfer_connection_units


CHUNK = 64 * 1024
DATA = bytes(range(251)) * 3000 + b'last partial chunk'


@contextmanager
def source(mode='ranges', *, size=CHUNK):
    state = dict(active=0, peak=0, calls=[], counts=Counter(), if_ranges=[],
                 fixture_headers=[], stopped=threading.Event())
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
                if not probe and mode in ('throttle', 'unavailable'):
                    self.send_response(429 if mode == 'throttle' else 503)
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
                if mode == 'slow' and not probe:
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
        self.assertLessEqual(max(state['counts'].values()), 2)

    def test_throttle_is_preserved_and_does_not_start_serial_fallback(self):
        with source('throttle') as (url, state):
            with self.assertRaisesRegex(yt_dlp.utils.DownloadError, 'HTTP Error 429'):
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

    def test_retry_after_survives_ignored_download_error_for_scheduler_cooldown(self):
        from webapp.channel_scheduler import throttle_details
        options = build_ydl_options({'youtube_sleep_preset': False}, Path(self.temp.name))
        options.update(quiet=True, noprogress=True, http_chunk_size=CHUNK, proxy='',
                       retries=0, fragment_retries=1)
        with source('unavailable') as (url, state), ParallelYoutubeDL(options) as downloader:
            result = downloader.dl(str(self.path), dict(url=url, protocol='http', filesize=len(DATA)))
            self.assertFalse(result[0])
            self.assertEqual(throttle_details(downloader.http_range_error), (503, 30))
        self.assertFalse(self.path.exists())
        self.assertEqual(max(state['counts'].values()), 1)

    def test_connection_budget_counts_native_http_and_combined_tracks(self):
        config = {'fragment_concurrency': 8, 'download_connection_limit': 4}
        budget = fragment_connection_budget(config, 16, 2)
        self.assertEqual(budget, 2)
        info = {'requested_formats': [{'protocol': 'https'}, {'protocol': 'https'}]}
        # Tracks are downloaded in sequence, so reserve the per-track maximum.
        self.assertEqual(transfer_connection_units(info, budget, native=True), 2)
        self.assertEqual(transfer_connection_units(info, budget), 1)
        self.assertEqual(transfer_connection_units({'protocol': 'm3u8_native'}, budget), 2)

    def test_worker_reserves_same_connection_count_as_actual_downloader(self):
        from unittest.mock import patch
        from web_worker import execute
        from webapp.channel_scheduler import ChannelScheduler

        for mode in ('manual', 'auto'):
            with self.subTest(mode=mode):
                reservations = []
                configured = []

                @contextmanager
                def permit(scheduler, channel, phase, **kwargs):
                    if phase == 'transfer':
                        reservations.append(kwargs['streams'])
                    yield channel

                def process(downloader, info, download=True):
                    configured.append(downloader.params['concurrent_fragment_downloads'])
                    self.assertIsInstance(downloader, ParallelYoutubeDL)
                    self.assertTrue(downloader.params['_parallel_http'])

                payload = dict(base=self.temp.name, urls=['https://youtu.be/00000000001'], config={
                    'download_method': 'yt_dlp', 'path': 'videos', 'throughput_mode': mode,
                    'youtube_pacing_mode': 'adaptive', 'youtube_sleep_preset': False,
                    'worker_count': 2, 'fragment_concurrency': 8, 'download_connection_limit': 4})
                info = dict(id='00000000001', title='fixture', ext='mp4',
                    requested_formats=[{'protocol': 'https'}, {'protocol': 'https'}])
                with patch.object(ChannelScheduler, 'permit', permit), \
                        patch.object(yt_dlp.YoutubeDL, 'extract_info', return_value=info), \
                        patch.object(yt_dlp.YoutubeDL, 'process_ie_result', process), \
                        patch('webapp.downloader.require_download_tools'), patch('builtins.print'):
                    execute('download', payload)
                self.assertEqual(reservations, [2])
                self.assertEqual(configured, reservations)


if __name__ == '__main__':
    unittest.main()
