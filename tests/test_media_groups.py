"""Group pagination must never split a video's languages or hide sibling outputs."""
import json
from datetime import datetime
import unittest

from webapp.database import delivery_artifacts, media
from tests import test_media_cleanup as cleanup_tests


class MediaGroupTests(unittest.TestCase):
    setUp = cleanup_tests.MediaCleanupTests.setUp
    write = cleanup_tests.MediaCleanupTests.write

    def artifact(self, video='abcdefghijk', title='A title', kind='subtitle', language='en', **changes):
        with self.store.connect() as db:
            seq = self.store.db.sequence(db, 'delivery_artifact')
            row = dict(id=f'{seq:064x}', seq=seq, contract_version=1, job_id='j' * 32, task_id='t' * 32,
                video_id=video, title=title, kind=kind, language=language, subtitle_source='manual',
                variant='fixture', path=str(self.root / f'{seq}.srt'), size=7, instance_id='local',
                storage='local', available=1, test_mode=0, created_at='2026-11-15T12:00:00+08:00')
            row.update(changes)
            db.execute(delivery_artifacts.insert().values(**row))
        return row['id']

    def groups(self, **params):
        response = self.client.get('/api/media/groups', query_string=params)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def files(self, key='video:abcdefghijk', **params):
        response = self.client.get('/api/media/group-files', query_string={'key': key, **params})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_all_types_and_languages_share_one_source_group_and_same_titles_stay_distinct(self):
        for kind in ('av', 'video_only', 'audio_only', 'subtitle', 'thumbnail', 'metadata'):
            self.artifact(kind=kind)
        self.artifact(language='fr', subtitle_source='original_auto')
        self.artifact(video='otherVideo1')
        self.write('import.mp4')
        self.write('import.info.json', json.dumps({'id': 'abcdefghijk', 'title': 'A title'}).encode())
        self.catalog.reindex(self.root)
        result = self.groups()
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['file_count'], 9)
        group = next(item for item in result['items'] if item['video_id'] == 'abcdefghijk')
        self.assertEqual(group['kind_counts'], dict(av=1, video_only=1, audio_only=1, subtitle=2, thumbnail=1, metadata=1, source_video=1))
        self.assertEqual(group['file_count'], 8)
        self.assertEqual(group['size'], 56)
        files = self.files()['items']
        self.assertEqual(len(files), 8)
        subtitles = [item['artifact'] for item in files if item['kind'] == 'subtitle']
        self.assertEqual({(item['language'], item['subtitle_source']) for item in subtitles},
                         {('en', 'manual'), ('fr', 'original_auto')})
        self.assertEqual(sum('local' in item for item in files), 1)
        for item in subtitles:
            self.assertNotIn('path', item)
            self.assertNotIn('object_key', item)

    def test_group_and_file_pagination_keep_all_155_siblings_once(self):
        expected = {self.artifact(language=f'lang-{i}') for i in range(155)}
        for index in range(27):
            self.artifact(video=f'video-{index}', title=f'Other {index:02}')
        first = self.groups(limit=12)
        second = self.groups(limit=12, cursor=first['next_cursor'])
        third = self.groups(limit=12, cursor=second['next_cursor'])
        groups = first['items'] + second['items'] + third['items']
        self.assertEqual(len(groups), 28)
        self.assertEqual(len({item['group_key'] for item in groups}), 28)
        self.assertIsNone(third['next_cursor'])
        self.assertEqual(first['total'], third['total'])
        self.assertEqual(first['file_count'], 182)
        self.assertEqual(groups[0]['file_count'], 155)
        found, cursor = [], None
        for size in (50, 50, 50, 5):
            result = self.files(**({'cursor': cursor} if cursor else {}))
            self.assertEqual(result['total'], 155)
            self.assertEqual(len(result['items']), size)
            found.extend(item['artifact']['id'] for item in result['items'])
            cursor = result['next_cursor']
        self.assertIsNone(cursor)
        self.assertEqual(set(found), expected)
        self.assertEqual(len(found), len(set(found)))

    def test_type_and_search_select_whole_groups_instead_of_hiding_other_files(self):
        self.artifact(title='100%_caption', language='fr')
        self.artifact(title='Another title', kind='av')
        self.artifact(title='Another title', kind='audio_only')
        self.artifact(video='noMatch0001', title='100 percent')
        result = self.groups(q='%_', type='audio_only')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['file_count'], 3)
        self.assertEqual(result['counts'], dict(all=1, av=1, video_only=0, audio_only=1, subtitle=1, thumbnail=0, metadata=0, source_video=0))
        self.assertEqual(result['items'][0]['kind_counts'], dict(av=1, audio_only=1, subtitle=1))
        self.assertEqual(len(self.files()['items']), 3)
        # A finished AV file is its own actual output type, not a pure audio file.
        self.artifact(video='avOnly00001', title='AV only', kind='av')
        self.assertEqual(self.groups(type='audio_only')['total'], 1)

    def test_latest_download_orders_mixed_groups_and_files_with_stable_pagination(self):
        early = '2026-11-15T12:00:00+08:00'
        later = '2026-11-15T05:00:00+00:00'
        latest = '2026-11-15T14:00:00+08:00'
        old = self.artifact(video='old', title='A first alphabetically', created_at=early)
        self.artifact(video='tie-b', title='B', created_at=later)
        self.artifact(video='tie-a', title='Z', created_at='2026-11-15T13:00:00+08:00')
        newest = self.artifact(video='old', title='A first alphabetically', kind='audio_only', created_at=latest)
        self.write('import.mp4')
        self.write('import.info.json', json.dumps({'id': 'import', 'title': 'Imported'}).encode())
        self.catalog.reindex(self.root)
        imported_at = datetime.fromisoformat('2026-11-15T13:30:00+08:00').timestamp() + .5
        with self.store.connect() as db:
            db.execute(media.update().values(downloaded_at=imported_at))

        found, cursor = [], None
        for _ in range(4):
            result = self.groups(limit=1, **({'cursor': cursor} if cursor else {}))
            found.extend(result['items'])
            cursor = result['next_cursor']
        self.assertEqual([row['video_id'] for row in found], ['old', 'import', 'tie-a', 'tie-b'])
        self.assertEqual([row['downloaded_at'] for row in found],
                         [datetime.fromisoformat(latest).timestamp(), imported_at,
                          datetime.fromisoformat(later).timestamp(), datetime.fromisoformat(later).timestamp()])
        self.assertIsNone(cursor)
        first = self.files(key='video:old', limit=1)
        second = self.files(key='video:old', limit=1, cursor=first['next_cursor'])
        self.assertEqual([first['items'][0]['artifact']['id'], second['items'][0]['artifact']['id']], [newest, old])
        self.assertIsNone(second['next_cursor'])

    def test_unavailable_test_and_foreign_files_are_excluded_and_registered_local_is_not_duplicated(self):
        path = self.write('delivered.mp4')
        self.catalog.reindex(self.root)
        expected = self.artifact(kind='video_only', path=str(path))
        self.artifact(available=0)
        self.artifact(test_mode=1)
        self.artifact(instance_id='other-node')
        self.assertEqual(self.groups()['file_count'], 1)
        self.assertEqual(self.groups()['items'][0]['kind_counts'], dict(video_only=1))
        self.assertEqual(self.files()['items'][0]['artifact']['id'], expected)
        self.assertEqual(self.files(key='video:missing')['items'], [])

    def test_local_files_without_video_id_remain_independent_and_downloadable(self):
        self.write('a/Same title.mp4')
        self.write('b/Same title.mp4')
        self.catalog.reindex(self.root)
        result = self.groups()
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['counts']['source_video'], 2)
        for group in result['items']:
            file = self.files(key=group['group_key'])['items'][0]
            self.assertEqual(file['kind'], 'source_video')
            with self.client.get('/media/videos/' + file['local']['name']) as response:
                self.assertEqual(response.status_code, 200)

    def test_empty_library_and_invalid_inputs(self):
        result = self.groups()
        self.assertEqual((result['total'], result['file_count'], result['items']), (0, 0, []))
        self.assertIsNone(result['next_cursor'])
        for route in ('/api/media/groups?type=invalid', '/api/media/groups?limit=0',
                      '/api/media/groups?cursor=invalid', '/api/media/group-files?key=invalid',
                      '/api/media/group-files?key=video:123&cursor=invalid'):
            self.assertEqual(self.client.get(route).status_code, 400, route)


if __name__ == '__main__':
    unittest.main()
