"""Rename publication and atomic cross-device fallback."""
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webapp.delivery.files import move_output


class PublicationTests(unittest.TestCase):
    def test_cross_device_publication_hides_partial_bytes_and_removes_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';target=root/'final'
            source.write_bytes(b'complete source');target.write_bytes(b'previous')
            replace=Path.replace
            def cross_device(path, destination):
                if path==source:
                    raise OSError(errno.EXDEV,'different devices')
                self.assertEqual(target.read_bytes(),b'previous')
                return replace(path,destination)
            with patch.object(Path,'replace',cross_device):
                move_output(source,target)
            self.assertEqual(target.read_bytes(),b'complete source')
            self.assertFalse(source.exists())
            self.assertEqual(list(root.iterdir()),[target])

    def test_failed_cross_device_copy_keeps_previous_output_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';target=root/'final'
            source.write_bytes(b'complete source');target.write_bytes(b'previous')
            replace=Path.replace
            def cross_device(path,destination):
                if path==source:
                    raise OSError(errno.EXDEV,'different devices')
                return replace(path,destination)
            def interrupted(source,destination):
                destination.write_bytes(b'incomplete')
                raise OSError(errno.ENOSPC,'disk full')
            with patch.object(Path,'replace',cross_device), \
                 patch('webapp.delivery.files.shutil.copyfile',side_effect=interrupted):
                with self.assertRaises(OSError):
                    move_output(source,target)
            self.assertEqual(target.read_bytes(),b'previous')
            self.assertEqual(source.read_bytes(),b'complete source')
            self.assertEqual(set(root.iterdir()),{source,target})

    def test_permission_failure_does_not_trigger_copy(self):
        with patch.object(Path,'replace',side_effect=PermissionError(errno.EACCES,'denied')), \
             patch('webapp.delivery.files.shutil.copyfile') as copy:
            with self.assertRaises(PermissionError):
                move_output(Path('source'),Path('target'))
            copy.assert_not_called()
