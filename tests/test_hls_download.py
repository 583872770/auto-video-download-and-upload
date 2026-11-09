"""Real native-HLS transfers, media probes and yt-dlp completion semantics."""
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import PropertyMock, patch

from yt_dlp.networking.exceptions import HTTPError

from webapp.hls_download import empty_tail_candidate
from webapp.parallel_http import ParallelYoutubeDL


PLAIN = '#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\none.ts\n#EXTINF:1,\ntwo.ts\n#EXTINF:0.007,\ntail.ts\n#EXT-X-ENDLIST\n'


class CandidateTests(unittest.TestCase):
    def test_only_complete_plain_playlists_with_subframe_tails(self):
        candidate = empty_tail_candidate(PLAIN, {'duration': 2})
        self.assertEqual(candidate[0], 'tail.ts')
        self.assertNotIn('tail.ts', candidate[1])
        self.assertEqual(candidate[2], 2)
        for manifest, info in (
                (PLAIN.replace('#EXT-X-ENDLIST', ''), {}),
                (PLAIN.replace('0.007', '.051'), {}),
                (PLAIN.replace('0.007', 'nan'), {}),
                (PLAIN.replace('0.007', '-.007'), {}),
                (PLAIN.replace('#EXTINF:1,', '#EXT-X-BYTERANGE:188\n#EXTINF:1,'), {}),
                (PLAIN.replace('#EXTINF:1,', '#EXT-X-DISCONTINUITY\n#EXTINF:1,'), {}),
                (PLAIN.replace('#EXTINF:1,', '#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:1,'), {}),
                (PLAIN.replace('#EXTINF:1,', '#EXT-X-MAP:URI="init"\n#EXTINF:1,'), {}),
                (PLAIN, {'is_live': True}), (PLAIN, {'format_index': 0}),
                (PLAIN, {'section_start': 0}), (PLAIN, {'duration': 20}),
                (PLAIN, {'extra_param_to_segment_url': 'token=fixture'}),
                (PLAIN.replace('#EXTM3U\n', ''), {}),
                (PLAIN + 'another.ts\n', {})):
            with self.subTest(manifest=manifest, info=info):
                self.assertIsNone(empty_tail_candidate(manifest, info))


@contextmanager
def source(manifest, segments, *, tail=b'', status=200, missing=None, tail_length=True):
    calls = Counter()
    headers = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls[self.path] += 1
            headers.append(self.headers.get('X-Fixture'))
            code = status if self.path == '/tail.ts' else 200
            data = (manifest.encode() if self.path == '/video.m3u8' else tail
                    if self.path == '/tail.ts' else segments.get(self.path.lstrip('/'), b''))
            if self.path == '/' + str(missing):
                data = b''
            self.send_response(code)
            if self.path != '/tail.ts' or tail_length:
                self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/video.m3u8', calls, headers
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class Logger:
    def __init__(self):
        self.errors, self.warnings = [], []

    def debug(self, message):
        pass

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
class HlsDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        directory = Path(cls.fixture.name)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=96x64:r=24', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
            '-t', '2', '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '24', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0',
            '-hls_segment_filename', str(directory/'segment%d.ts'), str(directory/'video.m3u8')],
            capture_output=True, check=True, timeout=30)
        cls.base_manifest = (directory/'video.m3u8').read_text()
        cls.manifest = cls.base_manifest.replace('#EXT-X-ENDLIST', '#EXTINF:0.007,\ntail.ts\n#EXT-X-ENDLIST')
        cls.segments = {p.name: p.read_bytes() for p in sorted(directory.glob('segment*.ts'))}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'video.mp4'
        self.logger = Logger()
        self.options = dict(quiet=True, noprogress=True, logger=self.logger, ignoreerrors='only_download',
            retries=0, fragment_retries=0, concurrent_fragment_downloads=4,
            cachedir=False, proxy='', socket_timeout=2, hls_prefer_native=True,
            outtmpl=str(self.path), download_archive=str(Path(self.temp.name)/'archive.txt'))

    def info(self, url):
        return dict(id='fixture', title='Fixture', extractor='youtube', extractor_key='Youtube',
            url=url, protocol='m3u8_native', ext='mp4', format_id='96',
            vcodec='avc1', acodec='mp4a.40.2', duration=2, http_headers={'X-Fixture': 'retained'})

    def test_empty_tail_recovers_through_file_fixup_and_archive(self):
        with source(self.manifest, self.segments, tail_length=False) as (url, calls, headers), ParallelYoutubeDL(self.options) as ydl:
            ydl.process_ie_result(self.info(url), download=True)
            self.assertEqual(ydl._download_retcode, 0)
        self.assertTrue(self.path.is_file())
        self.assertEqual(calls['/video.m3u8'], 1)
        self.assertEqual(calls['/tail.ts'], 1)
        self.assertTrue(all(calls['/'+name] == 1 for name in self.segments))
        self.assertTrue(all(value == 'retained' for value in headers))
        self.assertEqual(self.logger.errors, [])
        self.assertTrue(any('时长校验通过' in value for value in self.logger.warnings))
        self.assertIn('youtube fixture', Path(self.options['download_archive']).read_text())
        decoded = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '1',
            '-i', str(self.path), '-f', 'null', '-'], capture_output=True, timeout=30)
        self.assertEqual(decoded.returncode, 0, decoded.stderr)

    def test_native_serial_and_parallel_transfers_keep_all_nonempty_media(self):
        for concurrency in (1, 4):
            for tail in (b'', next(iter(self.segments.values()))):
                with self.subTest(concurrency=concurrency, nonempty=bool(tail)):
                    self.path.unlink(missing_ok=True)
                    options = {**self.options, 'concurrent_fragment_downloads': concurrency}
                    with source(self.manifest, self.segments, tail=tail) as (url, _, _), ParallelYoutubeDL(options) as ydl:
                        self.assertTrue(ydl.dl(str(self.path), self.info(url))[0])
                        self.assertEqual(ydl._download_retcode, 0)
                    self.assertEqual(self.path.read_bytes(), b''.join(self.segments.values()) + tail)

    def test_missing_middle_fragment_is_fatal_and_cannot_enter_archive(self):
        with source(self.manifest, self.segments, missing='segment1.ts') as (url, _, _), ParallelYoutubeDL(self.options) as ydl:
            ydl.process_ie_result(self.info(url), download=True)
            self.assertNotEqual(ydl._download_retcode, 0)
        self.assertFalse(Path(self.options['download_archive']).exists())
        self.assertFalse(self.path.exists())

    def test_old_partial_download_is_discarded_before_verified_assembly(self):
        Path(str(self.path)+'.part').write_bytes(b'OLD INVALID MEDIA' * 300)
        Path(str(self.path)+'.ytdl').write_text(json.dumps({'downloader': {'current_fragment': {'index': 1}}}))
        with source(self.manifest, self.segments) as (url, calls, _), ParallelYoutubeDL(self.options) as ydl:
            self.assertTrue(ydl.dl(str(self.path), self.info(url))[0])
            self.assertEqual(ydl._download_retcode, 0)
            self.assertTrue(all(calls['/'+name] == 1 for name in self.segments))
        self.assertEqual(self.path.read_bytes(), b''.join(self.segments.values()))

    def test_recovery_preserves_unrelated_download_errors(self):
        with source(self.manifest, self.segments) as (url, _, _), ParallelYoutubeDL(self.options) as ydl:
            ydl.report_error('unrelated prior failure')
            self.assertTrue(ydl.dl(str(self.path), self.info(url))[0])
            self.assertEqual(ydl._download_retcode, 1)

    def test_no_probe_means_no_unverified_success(self):
        with source(self.manifest, self.segments) as (url, _, _), ParallelYoutubeDL(self.options) as ydl:
            with patch('webapp.hls_download.FFmpegPostProcessor.probe_executable', new_callable=PropertyMock, return_value=None):
                self.assertFalse(ydl.dl(str(self.path), self.info(url))[0])
            self.assertNotEqual(ydl._download_retcode, 0)
        self.assertFalse(self.path.exists())

    def test_real_http_access_limits_are_not_accepted_as_empty_tails(self):
        for status in (403, 429):
            with self.subTest(status=status):
                with source(self.manifest, self.segments, status=status) as (url, calls, _), ParallelYoutubeDL(self.options) as ydl:
                    with self.assertRaises(HTTPError) as caught:
                        ydl.dl(str(self.path), self.info(url))
                    self.assertEqual(caught.exception.status, status)
                    self.assertEqual(calls['/tail.ts'], 1)
                self.assertFalse(self.path.exists())

    def test_long_empty_tail_remains_a_download_failure(self):
        manifest = self.manifest.replace('0.007', '1.0')
        with source(manifest, self.segments) as (url, _, _), ParallelYoutubeDL(self.options) as ydl:
            ydl.dl(str(self.path), self.info(url))
            self.assertNotEqual(ydl._download_retcode, 0)
        self.assertFalse(any('时长校验通过' in value for value in self.logger.warnings))

    def test_missing_track_and_duration_mismatch_delete_file_and_fail(self):
        for invalid in ('missing_audio', 'wrong_duration', 'corrupt'):
            with self.subTest(invalid=invalid):
                streams = [{'codec_type': 'video', 'codec_name': 'h264', 'duration': '2'}]
                if invalid != 'missing_audio':
                    streams.append({'codec_type': 'audio', 'codec_name': 'aac', 'duration': '1' if invalid == 'wrong_duration' else '2'})
                result = subprocess.CompletedProcess([], 1 if invalid == 'corrupt' else 0,
                    stdout=json.dumps({'streams': streams}), stderr='bad media' if invalid == 'corrupt' else '')
                with source(self.manifest, self.segments) as (url, _, _), ParallelYoutubeDL(self.options) as ydl:
                    with patch('webapp.hls_download.subprocess.run', return_value=result):
                        self.assertFalse(ydl.dl(str(self.path), self.info(url))[0])
                    self.assertNotEqual(ydl._download_retcode, 0)
                self.assertFalse(self.path.exists())

    def test_ordinary_hls_has_no_extra_requests_or_media_probe(self):
        with source(self.base_manifest, self.segments) as (url, calls, _), ParallelYoutubeDL(self.options) as ydl:
            with patch('webapp.hls_download.verify_tail_media') as verify:
                self.assertTrue(ydl.dl(str(self.path), self.info(url))[0])
            verify.assert_not_called()
            self.assertEqual(ydl._download_retcode, 0)
        self.assertEqual(calls['/video.m3u8'], 1)
        self.assertNotIn('/tail.ts', calls)


if __name__ == '__main__':
    unittest.main()
