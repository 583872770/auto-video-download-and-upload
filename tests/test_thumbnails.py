"""Cover policies must be explicit; missing-image cache is bounded and local."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from webapp.delivery.thumbnails import candidates, MissingThumbnails


class ThumbnailPolicyTests(unittest.TestCase):
    def test_source_mode_never_uses_synthetic_youtube_urls_even_with_high_preference(self):
        info = {'extractor': 'youtube', 'thumbnail': 'https://image/source.jpg',
                'thumbnails': [{'url': 'https://image/source.jpg', 'preference': -5},
                               {'url': 'https://image/guessed.webp', 'preference': 0}]}
        self.assertEqual(candidates(info, 'source'), ['https://image/source.jpg'])
        self.assertEqual(candidates(info, 'best'), ['https://image/guessed.webp', 'https://image/source.jpg'])
        del info['thumbnail']
        self.assertEqual(candidates(info, 'source'), [])

    def test_cache_survives_retry_expires_and_keeps_tasks_isolated_without_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = MissingThumbnails(tmp, 'task')
            url = 'https://image/missing.jpg?secret=token'
            cache.record(url)
            self.assertTrue(MissingThumbnails(tmp, 'task').contains(url))
            self.assertFalse(MissingThumbnails(tmp, 'other').contains(url))
            self.assertNotIn('token', cache.path.read_text())
            with patch('webapp.delivery.thumbnails.time.time', return_value=10**12):
                self.assertFalse(cache.contains(url))
            with patch.object(MissingThumbnails, 'LIMIT', 2):
                for i in range(4):
                    cache.record(url + str(i))
                self.assertEqual(len(cache.read()), 2)

