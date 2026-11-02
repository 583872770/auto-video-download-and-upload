"""Browser discovery for Docker's system Chromium and local installations."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from webapp.browser import browser_options


class BrowserDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.chromium = SimpleNamespace(executable_path=str(self.root / 'missing-bundled-browser'))
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        lookup = patch('webapp.browser.shutil.which', return_value=None)
        self.which = lookup.start()
        self.addCleanup(lookup.stop)

    def executable(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return str(path)

    def test_docker_desktop_browser_is_used_without_bundled_installation(self):
        system = self.executable('usr/bin/chromium')
        with patch.dict(os.environ, {'YT5S_BROWSER': system}):
            self.assertEqual(browser_options(self.chromium), {'executable_path': system})
        self.which.assert_not_called()

    def test_explicit_configuration_overrides_desktop_environment(self):
        explicit = self.executable('custom/chrome')
        with patch.dict(os.environ, {'YT5S_BROWSER': str(self.root / 'invalid-env-path')}):
            self.assertEqual(browser_options(self.chromium, explicit), {'executable_path': explicit})

    def test_system_chromium_is_discovered_on_path(self):
        system = self.executable('usr/bin/chromium')
        self.which.side_effect = lambda name: system if name == 'chromium' else None
        self.assertEqual(browser_options(self.chromium), {'executable_path': system})

    def test_bundled_browser_keeps_default_launch_options(self):
        self.chromium.executable_path = self.executable('bundled/chrome')
        self.assertEqual(browser_options(self.chromium), {})
        self.which.assert_not_called()

    def test_windows_edge_fallback_remains_available(self):
        edge = self.executable('Microsoft/Edge/Application/msedge.exe')
        with patch.dict(os.environ, {'PROGRAMFILES': str(self.root)}):
            self.assertEqual(browser_options(self.chromium), {'executable_path': edge})

    def test_missing_browsers_have_actionable_messages_without_paths(self):
        with self.assertRaisesRegex(RuntimeError, 'up -d --build'):
            browser_options(self.chromium)
        with self.assertRaisesRegex(RuntimeError, 'browser_executable') as failure:
            browser_options(self.chromium, str(self.root / 'private-path'))
        self.assertNotIn('private-path', str(failure.exception))
