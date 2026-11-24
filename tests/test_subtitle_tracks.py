"""Caption provenance and the shared originating HTTP session."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yt_dlp

from webapp.subtitle_tracks import (SubtitleSession, automatic_tracks, select_tracks,
                                   translated, provider_options, original_tracks, caption_plan)
from webapp.delivery.contracts import normalize


ORIGINAL = {'url': 'https://www.youtube.com/api/timedtext?lang=en', 'ext': 'vtt'}
TRANSLATED = {'url': ORIGINAL['url'] + '&tlang=zh-Hans', 'ext': 'vtt'}


class SubtitleTrackTests(unittest.TestCase):
    def test_live_chat_is_excluded_from_original_and_translated_caption_plans(self):
        chat = {'url': 'https://www.youtube.com/watch?v=fixture', 'ext': 'json'}
        for protocol in ('youtube_live_chat', 'youtube_live_chat_replay'):
            with self.subTest(protocol=protocol):
                info = {'subtitles': {'live_chat': [chat], 'en': [ORIGINAL, {**chat, 'protocol': protocol}],
                    'fr-en': [{**TRANSLATED, 'protocol': protocol}]},
                    'automatic_captions': {'live_chat': [chat], 'ja': [{**chat, 'protocol': protocol}]}}
                for mode in ('original', 'original_and_translated'):
                    config = normalize({'subtitles': {'mode': mode, 'languages': ['fr']}})['subtitles']
                    available, required = caption_plan(info, config)
                    self.assertEqual(available, {'en': ([ORIGINAL], None)})
                    self.assertEqual(required, ['en'] if mode == 'original' else ['en', 'fr'])

    def test_json3_captions_remain_available_when_chat_is_the_only_other_resource(self):
        caption = {**ORIGINAL, 'ext': 'json3'}
        info = {'subtitles': {'live_chat': [{'url': ORIGINAL['url'], 'ext': 'json'}]},
                'automatic_captions': {'en-orig': [caption]}}
        config = normalize({})['subtitles']
        self.assertEqual(caption_plan(info, config), ({'en': (None, [caption])}, ['en']))
        info['automatic_captions'] = {}
        self.assertEqual(caption_plan(info, config), ({}, []))

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

    def test_only_actual_cross_language_youtube_tracks_are_translations(self):
        self.assertTrue(translated(TRANSLATED))
        self.assertFalse(translated(ORIGINAL))
        self.assertFalse(translated({**ORIGINAL, 'url': ORIGINAL['url'] + '&tlang=en'}))
        self.assertFalse(translated({'url': 'https://other.test/subs?lang=en&tlang=fr'}))

    def test_caption_downloads_reuse_downloader_and_restore_parameters(self):
        with yt_dlp.YoutubeDL({'quiet': True}) as original:
            original._load_cookies('VISITOR_INFO1_LIVE=first', autoscope='https://www.youtube.com')
            session = SubtitleSession(original)
            params = original.params
            director = original._request_director
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(original, 'process_ie_result', side_effect=AssertionError('video reprocessed')), \
                 patch.object(original, 'dl') as download:
                for lang in ('en', 'ja'):
                    paths = session.download({'id': 'fixture', 'title': 'Fixture'}, lang,
                        ([ORIGINAL], None), Path(tmp))
                    self.assertEqual(Path(paths[0]).name, f'caption.{lang}.vtt')
                    self.assertTrue(download.call_args.kwargs['subtitle'])
                    self.assertIs(original.params, params)
                    self.assertIs(original._request_director, director)
                self.assertEqual(download.call_count, 2)
                with patch.object(original, '_write_subtitles', side_effect=RuntimeError('failure')):
                    with self.assertRaises(RuntimeError):
                        session.download({'id': 'fixture'}, 'en', ([ORIGINAL], None), Path(tmp))
                self.assertIs(original.params, params)

    def test_caption_format_preference_headers_and_inline_data_match_native_writer(self):
        with yt_dlp.YoutubeDL({'quiet': True}) as downloader, tempfile.TemporaryDirectory() as tmp:
            session = SubtitleSession(downloader)
            tracks = [{**ORIGINAL, 'ext': 'srt'}, {**ORIGINAL, 'data': 'WEBVTT\ninline\n'}]
            paths = session.download({'id': 'fixture', 'http_headers': {'X-Fixture': 'shared'}},
                'en', (tracks, None), Path(tmp))
            self.assertEqual(Path(paths[0]).read_text(), 'WEBVTT\ninline\n')
            with patch.object(downloader, 'dl') as download:
                session.download({'id': 'fixture', 'http_headers': {'X-Fixture': 'shared'}},
                    'ja', (None, [{**ORIGINAL, 'ext': 'srt'}]), Path(tmp))
                self.assertEqual(Path(download.call_args.args[0]).name, 'caption.ja.srt')
                self.assertEqual(download.call_args.args[1]['http_headers'], {'X-Fixture': 'shared'})

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
