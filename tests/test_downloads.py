"""Batch normalization, durable per-video progress and independent failures."""
import functools
import http.server
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import create_app
from webapp.downloads import DownloadFailure, DownloadProgress
from webapp.config.settings import urls_from, YOUTUBE_PREFIX


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config = self.root / 'config.yaml'
        config.write_text('path: videos\n', encoding='utf-8')
        self.app = create_app(config, self.root / 'state', start_worker=False)
        self.client = self.app.test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]
        self.headers = {'X-CSRF-Token': token}
        self.store = self.app.extensions['task_store']


    def test_structured_progress_tracks_audio_video_and_clears_stale_speed(self):
        task = self.store.create('download', {'urls': ['https://example.test/video.mp4']})
        tracker = DownloadProgress(lambda value: self.store.update_progress(task['id'], value))
        info = {'title': 'Video Title', 'format_id': '137', 'vcodec': 'avc1', 'ext': 'mp4',
                'requested_formats': [{'filesize': 1000}, {'filesize': 500}]}
        tracker.begin_video(info)
        info = {key: value for key, value in info.items() if key != 'requested_formats'}
        tracker.hook({'status': 'downloading', 'filename': 'video.mp4', 'downloaded_bytes': 500,
                      'total_bytes': 1000, 'speed': 200, 'eta': 3, 'info_dict': info})
        tracker.save(force=True)
        data = self.client.get(f'/api/tasks/{task["id"]}').json['progress']
        self.assertEqual(data['percent'], 25)
        self.assertEqual(data['downloaded_bytes'], 500)
        self.assertEqual(data['total_bytes'], 1500)
        self.assertEqual(data['speed'], 200)
        tracker.hook({'status': 'finished', 'filename': 'video.mp4', 'downloaded_bytes': 1000,
                      'total_bytes': 1000, 'info_dict': info})
        tracker.hook({'status': 'finished', 'filename': 'audio.m4a', 'downloaded_bytes': 500,
                      'total_bytes': 500, 'info_dict': {**info, 'vcodec': 'none'}})
        tracker.postprocess({'status': 'started', 'info_dict': info})
        data = self.store.get(task['id'])['progress']
        self.assertEqual(data['percent'], 100)
        self.assertEqual(data['stage'], 'processing')
        self.assertEqual(data['downloaded_bytes'], 1500)
        self.assertIsNone(data['speed'])
        self.store.finish(task['id'], error='download failed')
        failed = self.store.get(task['id'])['progress']
        self.assertEqual(failed['stage'], 'failed')
        self.assertIsNone(failed['speed'])
        self.assertIsNone(failed['eta'])

    def test_subtitles_share_progress_and_bytes_with_other_outputs(self):
        values=[]
        tracker=DownloadProgress(values.append)
        tracker.state['output_kind']='subtitle'
        tracker.hook({'status':'downloading','filename':'captions.vtt','downloaded_bytes':40,
                      'total_bytes':100,'speed':20,'info_dict':{'ext':'vtt','requested_formats':[{'filesize':9000}]}})
        tracker.save(force=True)
        self.assertEqual((values[-1]['stage'],values[-1]['percent']),('downloading',40))
        self.assertEqual((values[-1]['downloaded_bytes'],values[-1]['total_bytes'],values[-1]['speed']),(40,100,20))
        self.assertTrue(values[-1]['format'].startswith('字幕'))
        tracker.hook({'status':'finished','filename':'captions.vtt','downloaded_bytes':100,
                      'total_bytes':100,'info_dict':{'ext':'vtt'}})
        self.assertEqual(values[-1]['percent'],100)
        self.assertIsNone(values[-1]['speed'])

    def test_unknown_total_does_not_invent_percent_or_speed(self):
        values = []
        tracker = DownloadProgress(values.append)
        tracker.hook({'status': 'downloading', 'downloaded_bytes': 42, 'speed': float('nan'), 'eta': float('inf')})
        tracker.save(force=True)
        self.assertIsNone(values[-1]['percent'])
        self.assertIsNone(values[-1]['speed'])
        self.assertIsNone(values[-1]['eta'])
        json.dumps(values[-1], allow_nan=False)

    def test_cookie_rejection_and_bot_checks_have_distinct_actionable_errors(self):
        failure = DownloadFailure()
        failure.error('[youtube] id: Sign in to confirm you’re not a bot.')
        self.assertIn('代理节点', failure.message())
        self.assertNotIn('已失效', failure.message())
        failure.warning('[youtube] The provided YouTube account cookies are no longer valid.')
        self.assertIn('已失效或被轮换', failure.message())
        self.assertIn('上传新的 Cookie', failure.message())


    def test_other_extractor_errors_retain_reason_without_credentials_or_terminal_codes(self):
        failure = DownloadFailure()
        failure.error('\x1b[31mHTTP Error 403: Forbidden https://user:SECRET@host.test/video?token=PRIVATE\x1b[0m')
        message = failure.message()
        self.assertIn('HTTP Error 403: Forbidden', message)
        for secret in ('SECRET', 'PRIVATE', '\x1b'):
            self.assertNotIn(secret, message)



class DownloadRateTests(unittest.TestCase):
    def test_missing_speed_uses_new_bytes_and_time_with_a_resumed_file(self):
        clock = [10.0]
        values = []
        with patch('webapp.downloads.time.monotonic', side_effect=lambda: clock[0]):
            tracker = DownloadProgress(values.append)
            def update(size, **fields):
                tracker.hook({'status': 'downloading', 'filename': 'resumed.mp4',
                              'downloaded_bytes': size, 'total_bytes': 20000, **fields})
                tracker.save(force=True)
                return values[-1]
            self.assertIsNone(update(10000)['speed'])
            clock[0] += 2
            progress = update(12000, speed=float('nan'))
            self.assertEqual(progress['speed'], 1000)
            self.assertEqual(progress['eta'], 8)
            self.assertEqual(progress['downloaded_bytes'], 12000)
            clock[0] += 2
            self.assertEqual(update(14000, speed=777, eta=9)['speed'], 777)
            self.assertEqual(values[-1]['eta'], 9)
            clock[0] += 2
            self.assertEqual(update(16000)['speed'], 1000)
            json.dumps(values[-1], allow_nan=False)

    def test_counter_reset_new_format_and_new_video_do_not_inherit_speed(self):
        clock = [10.0]
        with patch('webapp.downloads.time.monotonic', side_effect=lambda: clock[0]):
            tracker = DownloadProgress(lambda value: None)
            def update(size, filename='video', status='downloading'):
                tracker.hook({'status': status, 'filename': filename, 'downloaded_bytes': size})
                return tracker.state['speed']
            update(5000)
            clock[0] += 2
            self.assertEqual(update(7000), 1000)
            clock[0] += 1
            self.assertIsNone(update(0))
            self.assertEqual(tracker.state['downloaded_bytes'], 0)
            clock[0] += 1
            self.assertEqual(update(100), 100)
            self.assertIsNone(update(100, status='finished'))
            self.assertIsNone(update(9000, filename='audio'))
            clock[0] += 2
            self.assertEqual(update(9200, filename='audio'), 100)
            self.assertIsNone(tracker.state['eta'])  # Unknown file size.
            tracker.postprocess({})
            self.assertIsNone(tracker.state['speed'])
            tracker.begin_video({'title': 'Next video'})
            self.assertIsNone(update(100000, filename='audio'))

    def test_rate_window_stays_bounded_and_reports_zero_when_bytes_stop(self):
        clock = [10.0]
        with patch('webapp.downloads.time.monotonic', side_effect=lambda: clock[0]):
            tracker = DownloadProgress(lambda value: None)
            for count in range(2000):
                clock[0] += 0.01
                tracker.hook({'status': 'downloading', 'downloaded_bytes': count * 10})
            self.assertAlmostEqual(tracker.state['speed'], 1000)
            self.assertLessEqual(len(tracker.streams['video']['rate_samples']), 22)
            for _ in range(8):
                clock[0] += 1
                tracker.hook({'status': 'downloading', 'downloaded_bytes': 19990})
            self.assertEqual(tracker.state['speed'], 0)
