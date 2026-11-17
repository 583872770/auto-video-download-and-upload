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
        self.assertEqual(set(bootstrap), {'csrf','fleetEnabled','role'})
        self.assertNotIn('/static/app.js', response.text)
        self.assertIn('/static/frontend/main.js', response.text)


    def test_business_pages_share_authenticated_shell_without_legacy_scripts(self):
        self.setup_admin()
        bundle = dict(script='frontend/main.js', styles=[], preload=[])
        for path in ('/', '/jobs', '/jobs/' + 'a' * 32, '/workbench', '/media', '/logs',
                     '/accounts', '/instances', '/statistics', '/proxy', '/deployment'):
            with self.subTest(path=path):
                self.assertEqual(self.app.test_client().get(path).location, '/login')
                with patch('webapp.frontend.assets', return_value=bundle):
                    response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn('id="frontend-app"', response.text)
                self.assertNotIn('/static/jobs.js', response.text)
                self.assertNotIn('/static/delivery_files.js', response.text)
                self.assertNotIn('/static/app.js', response.text)

    def test_login_and_recovery_use_the_bundle_without_business_database(self):
        from werkzeug.test import Client
        from werkzeug.wrappers import Response
        from webapp.deployment import DeploymentGateway
        with TemporaryDirectory() as directory:
            gateway = DeploymentGateway(Path(directory) / 'config.yaml', Path(directory) / 'state', load_environment=False)
            client = Client(gateway, Response)
            bundle = dict(script='frontend/main.js', styles=[], preload=[])
            with patch('webapp.frontend.assets', return_value=bundle):
                response = client.get('/setup')
                login = self.client.get('/login')
            self.assertIn('/setup-static/frontend/main.js', response.text)
            self.assertIn('id="frontend-app"', login.text)
            self.assertNotIn(gateway.access_code, response.text)
            self.assertNotIn('/static/console-auth.js', login.text)
            self.assertEqual(client.get('/api/deployment').status_code, 403)
            for key in ('mysql_host', 'mysql_password', 'redis_url'):
                self.assertNotIn(f'name="{key}"', response.text)


    def test_manifest_resolves_shared_styles_and_preloads_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'frontend' / '.vite'
            from werkzeug.exceptions import ServiceUnavailable
            with self.assertRaises(ServiceUnavailable):
                assets(directory)
            root.mkdir(parents=True)
            (root / 'manifest.json').write_text(json.dumps({
                'src/main.ts': {'file':'assets/main.js','imports':['_vue.js'],'css':['assets/main.css']},
                '_vue.js': {'file':'assets/vue.js','css':['assets/shared.css']},
            }), encoding='utf-8')
            self.assertEqual(assets(directory), dict(script='frontend/assets/main.js',
                styles=['frontend/assets/shared.css','frontend/assets/main.css'], preload=['frontend/assets/vue.js']))


if __name__ == '__main__':
    unittest.main()
