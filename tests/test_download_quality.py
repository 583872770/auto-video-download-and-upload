"""Quality selection uses real yt-dlp selection and provider API contracts."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import yt_dlp

from tests import test_scale
from webapp.downloader import build_ydl_options
from webapp.downmp3 import resolve


class NativeQualityTests(unittest.TestCase):
    def select(self, formats, config=None):
        options = build_ydl_options(config or {}, Path('.'))
        options.update(quiet=True, no_warnings=True)
        info = {'id': 'fixture', 'title': 'Fixture', 'extractor': 'test',
                'formats': [dict(url=f'https://example.test/{i}', **f) for i, f in enumerate(formats)]}
        with yt_dlp.YoutubeDL(options) as downloader:
            return downloader.process_ie_result(deepcopy(info), download=False)

    def formats(self):
        return [
            dict(format_id='h264', ext='mp4', height=1080, width=1920, fps=30,
                 vcodec='avc1.640028', acodec='none', tbr=6000),
            dict(format_id='vp9', ext='webm', height=2160, width=3840, fps=60,
                 vcodec='vp9', acodec='none', tbr=12000),
            dict(format_id='aac', ext='m4a', vcodec='none', acodec='mp4a.40.2', abr=128),
            dict(format_id='audio-opus', ext='webm', vcodec='none', acodec='opus', abr=160),
        ]

    def test_4k_vp9_beats_1080p_h264_and_keeps_best_audio(self):
        selected = self.select(self.formats())
        self.assertEqual(selected['format_id'], 'vp9+audio-opus')
        self.assertEqual(selected['height'], 2160)
        self.assertEqual(selected['ext'], 'mkv')

    def test_no_resolution_cap_and_higher_frame_rate_wins_at_same_resolution(self):
        formats = self.formats() + [dict(format_id='8k30', ext='mp4', height=4320, width=7680,
            fps=30, vcodec='av01.0.16M.08', acodec='none', tbr=20000),
            dict(format_id='8k60', ext='mp4', height=4320, width=7680,
            fps=60, vcodec='av01.0.16M.08', acodec='none', tbr=18000)]
        self.assertEqual(self.select(formats)['format_id'], '8k60+audio-opus')

    def test_progressive_video_and_explicit_custom_selector_remain_usable(self):
        progressive = dict(format_id='combined', ext='mp4', height=720, width=1280,
            fps=30, vcodec='avc1.640028', acodec='mp4a.40.2', tbr=2000)
        selected = self.select([progressive])
        self.assertEqual((selected['format_id'], selected['ext']), ('combined', 'mp4'))
        self.assertEqual(self.select(self.formats(), {'download_format': 'h264+aac'})['format_id'],
                         'h264+aac')


class ApiQualityTests(unittest.TestCase):
    def test_default_and_best_request_highest_supported_numeric_api_target(self):
        for config in ({}, {'downmp3_quality': 'best'}):
            with self.subTest(config=config), patch('webapp.downmp3.request_json', return_value={
                    'success': True, 'download_url': 'https://cdn.example.com/video.mp4'}) as request, \
                    patch('builtins.print'):
                info = resolve('https://www.youtube.com/watch?v=npNnbj46_ik', config, Path('.'))
                query = parse_qs(urlsplit(request.call_args.args[1]).query)
                self.assertEqual(query['format'], ['1080'])
                self.assertEqual(info['format_id'], 'downmp3-1080')


class QualityPolicyTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_defaults_and_best_settings_roundtrip_and_existing_job_stays_independent(self):
        config = self.client.get('/api/config').json['config']
        self.assertEqual((config['yt5s_quality'], config['downmp3_quality']), ('best', 'best'))
        response = self.client.post('/api/jobs', json={'customer': 'Client', 'name': 'Fixed quality',
            'policy': {'yt5s_quality': '360', 'downmp3_quality': '480'}}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        identity = response.json['id']
        response = self.client.put('/api/config', json={
            'yt5s_quality': 'best', 'downmp3_quality': 'best'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        job = self.client.get('/api/jobs/' + identity).json
        self.assertEqual((job['policy']['yt5s_quality'], job['policy']['downmp3_quality']), ('360', '480'))
        response = self.client.put('/api/jobs/' + identity, json={'edit_version': job['edit_version'],
            'policy': {'yt5s_quality': 'best', 'downmp3_quality': 'best'}}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        job = self.client.get('/api/jobs/' + identity).json
        self.assertEqual((job['policy']['yt5s_quality'], job['policy']['downmp3_quality']), ('best', 'best'))
