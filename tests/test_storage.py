"""Disposable downloads must never delete or archive permanent media."""
from concurrent.futures import ThreadPoolExecutor
import functools
import http.server
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from Utils.download_storage import DownloadWorkspace
from tests import test_scale






class WorkspaceTests(unittest.TestCase):
    def test_invalid_task_id_and_symlink_cannot_select_a_deletion_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for identity in ('..', '../videos', 'videos', 'a' * 31):
                with self.assertRaises(ValueError):
                    DownloadWorkspace({'storage_mode': 'test'}, root, identity)
            target = root / 'permanent'
            target.mkdir()
            sentinel = target / 'keep.mp4'
            sentinel.write_bytes(b'keep')
            workspace = DownloadWorkspace({'storage_mode': 'test'}, root, 'a' * 32)
            workspace.root.mkdir()
            try:
                workspace.directory.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest('Creating symlinks requires OS privileges')
            with self.assertRaises(ValueError):
                workspace.cleanup()
            self.assertEqual(sentinel.read_bytes(), b'keep')
