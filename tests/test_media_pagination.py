"""Catalog totals and cursor paging must include every matching material once."""
import unittest

from webapp.database import delivery_artifacts
from tests import test_media_cleanup as cleanup_tests


class MediaPaginationTests(unittest.TestCase):
    setUp = cleanup_tests.MediaCleanupTests.setUp
    write = cleanup_tests.MediaCleanupTests.write

    def seed(self, count, *, kind='subtitle', instance='local', title='Fixture'):
        with self.store.connect() as db:
            first = self.store.db.sequence(db, 'delivery_artifact', count)
            rows = [dict(id=f'{seq:064x}', seq=seq, job_id='j' * 32, task_id='t' * 32,
                video_id=f'video-{seq}', title=title, kind=kind, language='en', variant='fixture',
                path=str(self.root / f'{seq}.srt'), size=7, instance_id=instance, storage='local',
                available=1, test_mode=0, created_at='2026-11-14') for seq in range(first, first + count)]
            db.execute(delivery_artifacts.insert(), rows)
        return {row['id'] for row in rows}

    def test_all_pages_include_155_outputs_once_and_keep_true_total(self):
        expected = self.seed(155)
        found, cursor = [], None
        for size in (50, 50, 50, 5):
            response = self.client.get('/api/media/assets', query_string={'artifact_limit': 50,
                **({'artifact_cursor': cursor} if cursor else {})})
            self.assertEqual(response.status_code, 200, response.json)
            page = response.json['artifacts']
            self.assertEqual(page['total'], 155)
            self.assertEqual(response.json['counts']['all'], 155)
            self.assertEqual(len(page['items']), size)
            found.extend(item['id'] for item in page['items'])
            cursor = page['next_cursor']
        self.assertIsNone(cursor)
        self.assertEqual(set(found), expected)
        self.assertEqual(len(found), len(set(found)))

    def test_exhausted_local_stream_stays_empty_while_artifacts_continue(self):
        self.write('local.mp4')
        self.catalog.reindex(self.root)
        self.seed(55)
        first = self.client.get('/api/media/assets').json
        self.assertEqual(len(first['media']['items']), 1)
        second = self.client.get('/api/media/assets', query_string={
            'media_exhausted': 1, 'artifact_cursor': first['artifacts']['next_cursor']}).json
        self.assertEqual(second['media']['items'], [])
        self.assertEqual(second['media']['total'], 1)
        self.assertEqual(len(second['artifacts']['items']), 5)
        self.assertEqual(second['counts']['all'], 56)

    def test_types_filter_the_entire_library_and_counts_do_not_follow_page_size(self):
        self.seed(61)
        audio = self.seed(2, kind='audio_only')
        av = self.seed(1, kind='av')
        self.seed(4, instance='another-node')
        self.write('local.mp4')
        self.catalog.reindex(self.root)
        result = self.client.get('/api/media/assets?type=audio').json
        self.assertEqual({item['id'] for item in result['artifacts']['items']}, audio | av)
        self.assertEqual(result['media']['items'], [])
        self.assertEqual(result['counts'], {'all': 65, 'video': 2, 'audio': 3, 'subtitle': 61})
        subtitles = self.client.get('/api/media/assets?type=subtitle&artifact_limit=1').json
        self.assertEqual(len(subtitles['artifacts']['items']), 1)
        self.assertEqual(subtitles['artifacts']['total'], 61)
        self.assertEqual(subtitles['counts'], result['counts'])

    def test_search_counts_match_results_and_escape_wildcards_literally(self):
        self.seed(3, title='100%_done')
        self.seed(5, title='100 percent')
        self.write('100%_done.mp4')
        self.write('other.mp4')
        self.catalog.reindex(self.root)
        result = self.client.get('/api/media/assets', query_string={'q': '%_', 'artifact_limit': 1}).json
        self.assertEqual(result['media']['total'], 1)
        self.assertEqual(result['artifacts']['total'], 3)
        self.assertEqual(result['counts']['all'], 4)
        self.assertEqual(self.client.get('/api/media/assets?type=invalid').status_code, 400)

    def test_registered_delivery_video_is_not_counted_again_in_local_index(self):
        path = self.write('delivered.mp4')
        self.catalog.reindex(self.root)
        identity = next(iter(self.seed(1, kind='video_only')))
        with self.store.connect() as db:
            db.execute(delivery_artifacts.update().where(delivery_artifacts.c.id == identity).values(path=str(path)))
        result = self.client.get('/api/media/assets').json
        self.assertEqual(result['counts']['all'], 1)
        self.assertEqual(result['counts']['video'], 1)
        self.assertEqual(result['media']['items'], [])
        self.assertEqual(result['artifacts']['items'][0]['id'], identity)
