"""Cloud delivery must not acknowledge files before verified OSS upload."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import oss2

from Utils.oss_storage import OSSStorage, OSSUploadError, archive_key, validate_oss
from tests import test_scale


CONFIG = dict(storage_mode='oss', oss_endpoint='https://oss-cn-hangzhou.aliyuncs.com',
              oss_bucket='video-fixture', oss_prefix='exports', oss_access_key_id='fixture-id',
              oss_access_key_secret='FIXTURE-SECRET', oss_security_token='')


class OSSStorageTests(unittest.TestCase):
    def test_validation_and_environment_credentials(self):
        with patch.dict('os.environ', {'OSS_ACCESS_KEY_ID': 'fixture', 'OSS_ACCESS_KEY_SECRET': 'secret'}):
            config = {**CONFIG, 'oss_endpoint': 'oss-cn-hangzhou-internal.aliyuncs.com',
                      'oss_access_key_id': '', 'oss_access_key_secret': ''}
            self.assertTrue(validate_oss(config)['oss_endpoint'].startswith('https://'))
        for key, value in [('oss_endpoint', 'http://oss-cn-hangzhou.aliyuncs.com'),
                           ('oss_endpoint', 'https://user:password@oss-cn-hangzhou.aliyuncs.com'),
                           ('oss_endpoint', 'https://example.test'), ('oss_bucket', '../bucket'),
                           ('oss_prefix', '../outside')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_oss({**CONFIG, key: value})

    def test_uploads_video_and_matching_sidecars_only_with_remote_verification(self):
        with tempfile.TemporaryDirectory() as directory, patch('oss2.Bucket') as factory, \
             patch('oss2.resumable_upload') as upload:
            root = Path(directory)
            for name in ('clip [a].mp4', 'clip [a].info.json', 'clip [a].jpg', 'clip [a].en.vtt',
                         'clip [a].mp4.part', 'other.mp4', 'other.jpg'):
                (root / name).write_bytes(b'fixture')
            factory.return_value.head_object.return_value.content_length = 7
            progress = []
            storage = OSSStorage(CONFIG)
            result = storage.upload_video(root / 'clip [a].mp4', root, progress.append)
            self.assertEqual(len(result), 4)
            self.assertEqual(upload.call_count, 4)
            self.assertEqual(factory.return_value.head_object.call_count, 4)
            self.assertTrue(all(row['key'].startswith('exports/clip [a].') for row in result))
            self.assertTrue(all(row['stage'] == 'uploading_oss' for row in progress))
            self.assertTrue((root / 'clip [a].mp4').is_file())

    def test_size_mismatch_retries_and_never_deletes_source(self):
        with tempfile.TemporaryDirectory() as directory, patch('oss2.Bucket') as factory, \
             patch('oss2.resumable_upload') as upload, patch('Utils.oss_storage.time.sleep'):
            path = Path(directory) / 'clip.mp4'
            path.write_bytes(b'fixture')
            factory.return_value.head_object.return_value.content_length = 1
            with self.assertRaises(OSSUploadError):
                OSSStorage(CONFIG).upload_video(path, path.parent)
            self.assertEqual(upload.call_count, 3)
            self.assertEqual(path.read_bytes(), b'fixture')

    def test_permission_failure_is_sanitized_and_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory, patch('oss2.Bucket'), \
             patch('oss2.resumable_upload', side_effect=oss2.exceptions.ServerError(
                 403, {}, b'<Error/>', {'Code': 'AccessDenied', 'Message': 'FIXTURE-SECRET'})) as upload:
            path = Path(directory) / 'clip.mp4'
            path.write_bytes(b'fixture')
            with self.assertRaises(OSSUploadError) as raised:
                OSSStorage(CONFIG).upload_video(path, path.parent)
            self.assertNotIn('FIXTURE-SECRET', str(raised.exception))
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(upload.call_count, 1)

    def test_cloud_failure_has_separate_scope_and_does_not_change_cookie_health(self):
        from webapp.download_policy import failure_evidence, recovery_policy
        from webapp.cookie_pool import CookiePool
        error = OSSUploadError('OSS fixture failure')
        failure = failure_evidence(error)
        self.assertEqual((failure.source, failure.phase, failure.kind), ('oss', 'upload', 'storage_failure'))
        self.assertTrue(failure.retryable)
        self.assertEqual(recovery_policy(failure, CONFIG).action, 'none')
        pool = object.__new__(CookiePool)
        pool.accounts = [('fixture', Path('fixture'), 'scope')]
        pool.profile_names = {'fixture': 'Fixture'}
        pool.config = CONFIG
        pool.router = SimpleNamespace(task_id=None, transaction_call=MagicMock())
        pool.outcome('scope', error)
        pool.router.transaction_call.assert_not_called()

    def test_archive_keys_distinguish_local_and_each_destination(self):
        key = 'youtube abcdefghijk'
        self.assertEqual(archive_key({'storage_mode': 'local'}, key), key)
        self.assertNotEqual(archive_key(CONFIG, key), key)
        self.assertNotEqual(archive_key(CONFIG, key), archive_key({**CONFIG, 'oss_prefix': 'other'}, key))

    def test_business_namespace_is_stable_across_attempt_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch('oss2.Bucket') as factory, \
             patch('oss2.resumable_upload'):
            factory.return_value.head_object.return_value.content_length = 5
            storage = OSSStorage({**CONFIG, '_oss_namespace': 'jobs/list/task'})
            keys = []
            for attempt in ('first', 'second'):
                root = Path(directory) / attempt
                root.mkdir()
                path = root / 'clip.mp4'
                path.write_bytes(b'video')
                keys.append(storage.upload_video(path, root)[0]['key'])
            self.assertEqual(keys, ['exports/jobs/list/task/clip.mp4'] * 2)

    def test_background_subtitles_sync_without_reuploading_video_or_cooling_youtube(self):
        import yt_dlp
        from webapp.subtitles import run_subtitles
        with tempfile.TemporaryDirectory() as directory, \
             patch('webapp.subtitles.ChannelScheduler') as scheduler, \
             patch('webapp.subtitles.DiskBudget') as budget, \
             patch.object(yt_dlp.YoutubeDL, 'download', return_value=0), \
             patch('oss2.Bucket') as factory, patch('oss2.resumable_upload') as upload:
            root = Path(directory)
            video = root / 'videos' / 'clip.mp4'
            video.parent.mkdir()
            video.write_bytes(b'video')
            video.with_suffix('.en.vtt').write_bytes(b'sub')
            scheduler.return_value.enabled = False
            scheduler.return_value.youtube_pacing = False
            budget.return_value.lease.return_value.__enter__.return_value.path = root / 'cache'
            factory.return_value.head_object.return_value.content_length = 3
            payload = {'base': str(root), 'config': CONFIG, 'video_file': str(video),
                       'urls': ['https://example.test/clip.mp4']}
            store = SimpleNamespace(directory=root / 'state')
            result = run_subtitles(store, payload, lambda _: None)
            self.assertEqual(result['oss_objects'], [{'key': 'exports/clip.en.vtt', 'size': 3}])
            self.assertEqual(upload.call_count, 1)
            upload.side_effect = oss2.exceptions.ServerError(403, {}, b'', {'Code': 'AccessDenied'})
            with self.assertRaises(OSSUploadError):
                run_subtitles(store, payload, lambda _: None)
            scheduler.return_value.outcome.assert_not_called()
            scheduler.return_value.transaction_call.assert_not_called()
            self.assertTrue(video.exists())

    def test_selected_instance_does_not_inherit_local_credentials(self):
        from webapp.fleet_routes import selected_configuration
        remote = {'storage_mode': 'oss', 'oss_access_key_secret': '',
                  'oss_access_key_secret_configured': True, 'oss_security_token_configured': False}
        with patch('webapp.fleet_routes.selected_id', return_value='remote'), \
             patch('webapp.fleet_routes.rpc', return_value={'config': remote}):
            result = selected_configuration(SimpleNamespace(id='local'), CONFIG)
        self.assertEqual(result['oss_access_key_id'], '')
        self.assertEqual(result['oss_access_key_secret'], '')
        self.assertEqual(result['oss_security_token'], '')
        self.assertTrue(result['oss_access_key_secret_configured'])

    def test_cli_uses_same_oss_delivery_worker(self):
        from webapp.downloader import main
        with tempfile.TemporaryDirectory() as directory, \
             patch('webapp.downloader.load_config', return_value=(CONFIG, Path(directory))), \
             patch('web_worker.execute', return_value={'storage_mode': 'oss'}) as execute:
            self.assertEqual(main(['https://example.test/clip.mp4']), 0)
            self.assertEqual(execute.call_args.args[0], 'download')
            self.assertEqual(execute.call_args.args[1]['config']['storage_mode'], 'oss')


class OSSWebTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_config_roundtrip_never_returns_saved_secrets_and_blank_preserves(self):
        response = self.client.put('/api/config', json=CONFIG, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        data = self.client.get('/api/config').json['config']
        self.assertEqual(data['storage_mode'], 'oss')
        self.assertEqual(data['oss_access_key_secret'], '')
        self.assertTrue(data['oss_access_key_secret_configured'])
        self.assertNotIn('FIXTURE-SECRET', self.client.get('/api/config').text)
        response = self.client.put('/api/config', json={'oss_access_key_secret': '', 'oss_prefix': 'new'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn('FIXTURE-SECRET', self.config.read_text(encoding='utf-8'))
        self.assertTrue(self.client.get('/api/config').json['config']['oss_access_key_secret_configured'])

    def test_upload_failure_keeps_files_unbilled_unarchived_then_retry_delivers(self):
        import yt_dlp
        from web_worker import execute
        from webapp.database import deliveries
        from sqlalchemy import func, select
        self.assertEqual(self.client.put('/api/config', json=CONFIG, headers=self.headers).status_code, 200)
        task = self.store.create('download', {'config': CONFIG, 'base': str(self.root),
                                             'urls': ['https://example.test/clip.mp4']})
        claimed = self.store.claim(self.store.owner)
        outputs = []
        def download(downloader, urls):
            info = {'title': 'clip', 'id': 'abcdefghijk', 'ext': 'mp4',
                    'url': urls[0], 'extractor': 'youtube', 'extractor_key': 'Youtube'}
            path = Path(downloader.prepare_filename(info))
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b'video-fixture')
            outputs.append(path)
            # Real process_info must recognize and reuse the local file, then
            # invoke completion hooks even though no transfer is necessary.
            downloader.process_ie_result(info)
            return 0
        with patch.object(yt_dlp.YoutubeDL, 'download', download), \
             patch('Utils.oss_storage.OSSStorage.upload_video', side_effect=OSSUploadError('fixture upload failed')):
            with self.assertRaises(OSSUploadError):
                execute('download', claimed['payload'], store=self.store, task_id=task['id'])
        self.assertTrue(outputs[0].is_file())
        self.assertFalse(self.store.archive_has(archive_key(CONFIG, 'youtube abcdefghijk')))
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(func.count()).select_from(deliveries)).scalar_one(), 0)
        with patch.object(yt_dlp.YoutubeDL, 'download', download), \
             patch('Utils.oss_storage.OSSStorage.upload_video', return_value=[{'key': 'exports/clip.mp4', 'size': 13}]) as upload:
            result = execute('download', claimed['payload'], store=self.store, task_id=task['id'])
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(result['storage_mode'], 'oss')
        self.assertEqual(result['file_count'], 1)
        self.assertTrue(outputs[0].is_file())
        self.assertTrue(self.store.archive_has(archive_key(CONFIG, 'youtube abcdefghijk')))
        self.assertFalse(self.store.archive_has('youtube abcdefghijk'))
        with self.store.connect() as db:
            self.assertEqual(db.execute(select(func.count()).select_from(deliveries)).scalar_one(), 1)
        self.store.finish(task['id'], result=result)
        self.assertNotIn('FIXTURE-SECRET', self.client.get('/api/tasks?view=history').text)

