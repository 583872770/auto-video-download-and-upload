"""Caption provenance, shared HTTP session, and cancellable warming waits."""
from contextlib import contextmanager
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yt_dlp
from yt_dlp.utils import DownloadError

from webapp.subtitle_tracks import (SubtitleSession, automatic_tracks, select_tracks,
                                   translated, provider_options, request_slot, original_tracks, caption_plan)
from webapp.delivery.contracts import normalize
from webapp.throughput import PipelineBusy


ORIGINAL = {'url': 'https://www.youtube.com/api/timedtext?lang=en', 'ext': 'vtt'}
TRANSLATED = {'url': ORIGINAL['url'] + '&tlang=zh-Hans', 'ext': 'vtt'}


class SubtitleTrackTests(unittest.TestCase):
    def test_original_discovers_languages_and_excludes_all_translated_variants(self):
        japanese = {**ORIGINAL, 'url': ORIGINAL['url'].replace('lang=en', 'lang=ja')}
        synthetic = {**TRANSLATED, 'url': ORIGINAL['url'] + '&tlang=en'}
        info = {'subtitles': {'ja': [japanese]}, 'automatic_captions': {
            'en': [TRANSLATED, ORIGINAL], 'en-orig': [ORIGINAL],
            'ja-orig': [japanese], 'zh-Hans': [TRANSLATED], 'en-en': [synthetic]}}
        self.assertEqual(original_tracks(info, ['manual', 'automatic']), {
            'ja': ([japanese], None), 'en': (None, [ORIGINAL])})
        self.assertEqual(original_tracks(info, ['manual']), {'ja': ([japanese], None)})
        self.assertEqual(original_tracks({'subtitles': None, 'automatic_captions': None}, ['manual', 'automatic']), {})

    def test_targets_include_manual_translation_aliases_but_keep_originals(self):
        info = {'subtitles': {'en': [ORIGINAL], 'zh-Hans-en': [TRANSLATED]},
                'automatic_captions': {}}
        config = normalize({'subtitles': {'mode': 'original_and_translated',
                                         'languages': ['zh-Hans', 'en']}})['subtitles']
        available, required = caption_plan(info, config)
        self.assertEqual(required, ['en', 'zh-Hans'])
        self.assertEqual(available, {'en': ([ORIGINAL], None), 'zh-Hans': ([TRANSLATED], None)})
        config['mode'] = 'original'
        self.assertEqual(caption_plan(info, config), ({'en': ([ORIGINAL], None)}, ['en']))

    def test_missing_translation_targets_never_succeed_on_originals_alone(self):
        info = {'subtitles': {'en': [ORIGINAL]}}
        config = normalize({'subtitles': {'mode': 'original_and_translated',
                                         'languages': ['zh-Hans', 'ja']}})['subtitles']
        available, required = caption_plan(info, config)
        self.assertEqual(list(available), ['en'])
        self.assertEqual(required, ['en', 'zh-Hans', 'ja'])
        info['automatic_captions'] = {'zh-Hans': [TRANSLATED]}
        self.assertEqual(caption_plan(info, config)[1], ['en', 'zh-Hans'])
        config['language_policy'] = 'all_selected'
        self.assertEqual(caption_plan(info, config)[1], ['en', 'zh-Hans', 'ja'])
        config['sources'] = ['manual']
        self.assertIn('zh-Hans', caption_plan(info, config)[0])

    def test_original_alias_then_native_exact_precede_translation_without_changing_language(self):
        translation = {**ORIGINAL, 'url': 'https://www.youtube.com/api/timedtext?lang=fr&tlang=en'}
        self.assertEqual(automatic_tracks({'en-orig': [ORIGINAL], 'en': [translation]}, 'en'), [ORIGINAL])
        self.assertEqual(automatic_tracks({'en': [translation, ORIGINAL]}, 'en'), [ORIGINAL])
        chosen = select_tracks({'automatic_captions': {'en-orig': [ORIGINAL], 'zh-Hans': [TRANSLATED]}},
                               ['zh-Hans', 'fr'], ['automatic'])
        self.assertEqual(chosen, {'zh-Hans': (None, [TRANSLATED])})
        self.assertEqual(select_tracks({'automatic_captions': {'en-orig': [ORIGINAL]}}, ['en'], ['manual']), {})

    def test_only_actual_youtube_translation_requests_require_warming(self):
        self.assertTrue(translated(TRANSLATED))
        self.assertFalse(translated(ORIGINAL))
        self.assertFalse(translated({**ORIGINAL, 'url': ORIGINAL['url'] + '&tlang=en'}))
        self.assertFalse(translated({'url': 'https://other.test/subs?lang=en&tlang=fr'}))

    def test_warming_is_once_per_visitor_and_preserves_cookiejar_without_reloading_login(self):
        clock = [100.0]
        events = []
        with yt_dlp.YoutubeDL({'quiet': True}) as original, \
                patch('webapp.subtitle_tracks.time.monotonic', side_effect=lambda: clock[0]), \
                patch('webapp.subtitle_tracks.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            original._load_cookies('VISITOR_INFO1_LIVE=first', autoscope='https://www.youtube.com')
            session = SubtitleSession(original)
            session.prepare([ORIGINAL], events.append)
            self.assertEqual(clock[0], 100)
            session.prepare([TRANSLATED], events.append)
            self.assertEqual(clock[0], 175)
            self.assertTrue(all(event['request_wait_reason'] == 'subtitle_session' for event in events))
            self.assertTrue(all('first' not in str(event) for event in events))
            with yt_dlp.YoutubeDL({'quiet': True}) as child:
                session.attach(child)
                self.assertIs(child.cookiejar, original.cookiejar)
                child._load_cookies('VISITOR_INFO1_LIVE=second', autoscope='https://www.youtube.com')
            session.prepare([TRANSLATED], events.append)
            self.assertEqual(clock[0], 250)
            session.prepare([TRANSLATED], events.append)
            self.assertEqual(clock[0], 250)

    def test_warming_stops_when_report_detects_a_lost_lease(self):
        jar = SimpleNamespace(get_cookies_for_url=lambda _: [])
        session = SubtitleSession(SimpleNamespace(cookiejar=jar))
        with patch('webapp.subtitle_tracks.time.sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'lease lost'):
                session.prepare([TRANSLATED], lambda _: (_ for _ in ()).throw(RuntimeError('lease lost')))
        sleep.assert_not_called()

    def test_slot_retries_admission_only_and_propagates_request_failures_once(self):
        attempts = []
        @contextmanager
        def slot(_):
            attempts.append(1)
            if len(attempts) == 1:
                error = PipelineBusy('interval');error.retry_after = 12
                raise error
            yield
        with patch('webapp.subtitle_lane.slot', slot), patch('webapp.subtitle_tracks.wait') as waiting:
            with self.assertRaisesRegex(DownloadError, 'HTTP Error 429'):
                with request_slot(None, lambda _: None):
                    raise DownloadError('HTTP Error 429')
            self.assertEqual(len(attempts), 2)
            self.assertEqual(waiting.call_args.args[0], 12)

    def test_long_cooldown_returns_to_queue_without_sleeping(self):
        @contextmanager
        def slot(_):
            error = PipelineBusy('cooldown');error.retry_after = 900
            raise error
            yield
        with patch('webapp.subtitle_lane.slot', slot), patch('webapp.subtitle_tracks.wait') as waiting:
            with self.assertRaises(PipelineBusy):
                with request_slot(None, lambda _: None):
                    self.fail('admitted while cooling')
            waiting.assert_not_called()

    def test_contended_short_waits_are_bounded_before_returning_to_queue(self):
        clock=[0]
        @contextmanager
        def slot(_):
            error=PipelineBusy('another subtitle');error.retry_after=30
            raise error
            yield
        with patch('webapp.subtitle_lane.slot',slot), \
             patch('webapp.subtitle_tracks.time.monotonic',side_effect=lambda:clock[0]), \
             patch('webapp.subtitle_tracks.wait',side_effect=lambda seconds,*_:clock.__setitem__(0,clock[0]+seconds)):
            with self.assertRaises(PipelineBusy):
                with request_slot(None,lambda _:None):
                    self.fail('unexpected admission')
        self.assertEqual(clock[0],90)

    def test_optional_provider_keeps_other_extractor_arguments(self):
        original = {'extractor_args': {'youtube': {'player_client': ['web']}}}
        with patch.dict(os.environ, {'VIDEO_SUBTITLE_POT_URL': 'http://subtitle-pot:4416'}):
            configured = provider_options(original)
        self.assertEqual(configured['extractor_args']['youtube'], original['extractor_args']['youtube'])
        self.assertEqual(configured['extractor_args']['youtubepot-bgutilhttp']['base_url'], ['http://subtitle-pot:4416'])
        self.assertNotIn('youtubepot-bgutilhttp', original['extractor_args'])
        with patch.dict(os.environ, {'VIDEO_SUBTITLE_POT_URL': 'http://subtitle-pot:4416'}):
            forced = provider_options(original, force_subs=True)
        self.assertEqual(forced['extractor_args']['youtube']['fetch_pot'], ['always'])
