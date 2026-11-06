"""Upload tuning must keep verification and honest progress timestamps."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from Utils.oss_storage import OSSStorage, validate_oss

CONFIG = dict(oss_endpoint='https://oss-cn-hangzhou.aliyuncs.com', oss_bucket='fixture-video',
              oss_access_key_id='fixture', oss_access_key_secret='fixture', oss_prefix='videos')


class OSSTuningTests(unittest.TestCase):
    def test_limits_validate_without_raising_default_upload_concurrency(self):
        value = validate_oss(dict(CONFIG))
        self.assertEqual([value[k] for k in ('oss_upload_threads', 'oss_part_size_mb', 'oss_upload_limit')], [2, 8, 2])
        for key in ('oss_upload_threads', 'oss_part_size_mb', 'oss_upload_limit'):
            for invalid in (True, 0, 'bad', 99):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    validate_oss({**CONFIG, key: invalid})

    def test_configurable_upload_verifies_size_and_only_renews_on_new_bytes(self):
        with tempfile.TemporaryDirectory() as directory, patch('oss2.Bucket') as bucket, \
             patch('oss2.resumable_upload') as upload, patch('Utils.oss_storage.time.time', side_effect=[100, 200]):
            path = Path(directory)/'video.mp4'
            path.write_bytes(b'x'*100)
            reports = []
            def transfer(*args, **kw):
                callback = kw['progress_callback']
                callback(50, 100)
                callback(100, 100)
                callback(100, 100)
            upload.side_effect = transfer
            bucket.return_value.head_object.return_value.content_length = 100
            storage = OSSStorage({**CONFIG, 'oss_upload_threads': 3, 'oss_part_size_mb': 16})
            storage.upload_video(path, path.parent, reports.append)
            self.assertEqual(upload.call_args.kwargs['num_threads'], 3)
            self.assertEqual(upload.call_args.kwargs['part_size'], 16*1024**2)
            self.assertTrue(bucket.call_args.kwargs['enable_crc'])
            self.assertEqual(bucket.return_value.head_object.call_count, 1)
            self.assertIsNone(reports[0]['oss_last_bytes_at'])
            self.assertEqual([row['oss_last_bytes_at'] for row in reports[-3:]], [200]*3)
            self.assertTrue(path.exists())
