"""Built frontend entry, authentication, bootstrap and source-only fallback."""
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests import test_console_auth
from webapp.frontend import assets


class FrontendTests(unittest.TestCase):
    setUp = test_console_auth.ConsoleAuthTests.setUp
    token = test_console_auth.ConsoleAuthTests.token
    setup_admin = test_console_auth.ConsoleAuthTests.setup_admin

    def test_settings_auth_and_safe_bootstrap(self):
        self.setup_admin()
        self.assertEqual(self.app.test_client().get('/settings').location, '/login')
        bundle = dict(script='frontend/main.js', styles=[], preload=[])
        with patch('webapp.frontend.assets', return_value=bundle):
            response = self.client.get('/settings')
        self.assertEqual(response.status_code, 200)
        bootstrap = json.loads(re.search(r'id="frontend-bootstrap"[^>]*>(.*?)</script>', response.text, re.S)[1])
        self.assertEqual(bootstrap['role'], 'admin')
        self.assertEqual(set(bootstrap), {'csrf','fleetEnabled','role','cobaltCatalog'})
        self.assertNotIn('/static/app.js', response.text)
        self.assertIn('/static/frontend/main.js', response.text)

    def test_unbuilt_source_uses_compatible_settings(self):
        with patch('webapp.frontend.assets', return_value=None):
            response = self.client.get('/settings')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/#settings')

    def test_manifest_resolves_shared_styles_and_preloads_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'frontend' / '.vite'
            self.assertIsNone(assets(directory))
            root.mkdir(parents=True)
            (root / 'manifest.json').write_text(json.dumps({
                'src/main.ts': {'file':'assets/main.js','imports':['_vue.js'],'css':['assets/main.css']},
                '_vue.js': {'file':'assets/vue.js','css':['assets/shared.css']},
            }), encoding='utf-8')
            self.assertEqual(assets(directory), dict(script='frontend/assets/main.js',
                styles=['frontend/assets/shared.css','frontend/assets/main.css'], preload=['frontend/assets/vue.js']))


if __name__ == '__main__':
    unittest.main()
