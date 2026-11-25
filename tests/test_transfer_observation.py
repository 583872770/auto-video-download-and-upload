"""Real native sleeps and HTTP transfers must remain independent observations."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from webapp.delivery.executor import DeliveryDownloader
from webapp.downloads import DownloadFailure, DownloadProgress
from webapp.wait_diagnostics import task_wait


class TransferObservationTests(unittest.TestCase):
    def test_real_sleep_boundary_zero_progress_resume_and_transfer_times(self):
        from webapp.transfer_observation import FileTransferObservation
        updates = []
        progress = DownloadProgress(updates.append)
        session = SimpleNamespace(progress=progress, transfer_seconds=0)
        fd = SimpleNamespace(filesize_or_none=lambda _: 100, temp_name=lambda _: 'part',
                             real_download=lambda *args: None, add_progress_hook=lambda hook: None)
        clock = [0.0]
        with patch('webapp.transfer_observation.time.monotonic', side_effect=lambda: clock[0]):
            observer = FileTransferObservation(session, fd, 'file')
            observer.sleeping(10)
            clock[0] = 10
            observer.request('file', {})
            self.assertEqual(progress.state['stage'], 'waiting_connection')
            self.assertIsNone(progress.state['request_wait_until'])
            event = dict(status='downloading', filename='file', downloaded_bytes=100, speed=999)
            observer.hook(event);progress.hook(event)
            self.assertEqual(progress.state['stage'], 'waiting_connection')
            self.assertIsNone(progress.state['speed'])
            self.assertEqual(task_wait(progress.state)['code'], 'first_byte')
            clock[0] = 13
            event['downloaded_bytes'] = 120
            observer.hook(event);progress.hook(event)
            self.assertEqual(progress.state['stage'], 'downloading')
            clock[0] = 17
            event.update(status='finished', downloaded_bytes=200)
            observer.hook(event);progress.hook(event);observer.close()
        self.assertEqual(progress.state['network_seconds'], dict(interval=10, connecting=3, transferring=4))
        self.assertEqual(session.transfer_seconds, 4)
        self.assertEqual(progress.state['stage'], 'processing')

    def test_native_media_and_subtitle_waits_still_run_once_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'source.vtt').write_text('WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n')
            server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=tmp))
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            try:
                updates = []
                progress = DownloadProgress(updates.append)
                session = SimpleNamespace(progress=progress, failure=DownloadFailure(), transfer_seconds=0)
                with DeliveryDownloader(session, {'quiet': True, 'noprogress': True,
                        'sleep_interval': 10, 'max_sleep_interval': 10,
                        'progress_hooks': [progress.hook]}) as downloader:
                    url = f'http://127.0.0.1:{server.server_port}/source.vtt'
                    with patch('yt_dlp.downloader.common.time.sleep') as sleep:
                        for subtitle in (False, True):
                            self.assertTrue(downloader.dl(str(root / ('subtitle.vtt' if subtitle else 'media.vtt')),
                                {'id': 'x', 'url': url, 'protocol': 'http', 'ext': 'vtt'}, subtitle)[0])
                    self.assertEqual([call.args[0] for call in sleep.call_args_list], [10, 10])
                    self.assertIsNone(downloader.observation)
                stages = [item['stage'] for item in updates]
                self.assertEqual(stages.count('waiting_request'), 2)
                self.assertIn('waiting_connection', stages)
                self.assertIn('downloading', stages)
                self.assertIsNone(updates[-1]['request_wait_until'])
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_file_boundary_detaches_hooks_and_restores_the_native_downloader(self):
        from yt_dlp.downloader.http import HttpFD
        from webapp.transfer_observation import FileTransferObservation
        with DeliveryDownloader(SimpleNamespace(progress=DownloadProgress(lambda _: None),
                failure=DownloadFailure(), transfer_seconds=0), {'quiet': True}) as downloader:
            fd = HttpFD(downloader, downloader.params)
            original_hooks = list(fd._progress_hooks)
            observer = FileTransferObservation(downloader.session, fd, 'unused-file')
            observer.detach()
            self.assertNotIn('real_download', vars(fd))
            self.assertEqual(fd._progress_hooks, original_hooks)
            self.assertIsNone(observer.session)

