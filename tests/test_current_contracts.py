"""Current interfaces have explicit settings and no implicit legacy conversions."""
import unittest

from tests import test_scale, test_workbench
from webapp.delivery.contracts import normalize
from webapp.service_role import normalize_role


class CurrentContractTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def test_old_configuration_fields_are_rejected_without_changing_saved_file(self):
        before = self.config.read_bytes()
        for fields in ({'download_method': 'yt5s_fallback'}, {'subtitle_mode': 'required'},
                       {'subtitle_languages': ['en']}, {'builtin_connection_limit': 1}):
            response = self.client.put('/api/config', json=fields, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(self.config.read_bytes(), before)

    def test_missing_frontend_returns_actionable_503(self):
        from unittest.mock import patch
        from werkzeug.exceptions import ServiceUnavailable
        with patch('webapp.frontend.assets', side_effect=ServiceUnavailable('npm run build')):
            response = self.client.get('/settings')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('/static/app.js', response.text)

    def test_subtitle_language_does_not_select_a_hidden_legacy_mode(self):
        self.assertEqual(normalize({'subtitles': {'languages': ['en', 'fr']}})['subtitles']['mode'], 'original')
        with self.assertRaises(ValueError):
            normalize({'subtitles': {'mode': 'selected', 'languages': ['en']}})

    def test_only_current_service_role_is_supported(self):
        self.assertEqual(normalize_role(), 'hybrid')
        for role in ('worker', 'manager', 'all'):
            with self.assertRaises(ValueError):
                normalize_role(role)


class CurrentSchedulingTests(unittest.TestCase):
    setUp = test_workbench.WorkbenchFleetTests.setUp
    create = test_workbench.WorkbenchFleetTests.create
    control = test_workbench.WorkbenchFleetTests.control
    setup_fleet = test_workbench.WorkbenchFleetTests.setup_fleet

    def test_unselected_instance_cannot_inherit_another_instances_job(self):
        job = self.create(urls='npNnbj46_ik\n2Gu7q-KUXCk')
        self.control('alpha', 'start', job)
        with self.manager.connect() as db:
            self.b.fleet.activate(db)
        self.assertIsNone(self.b.claim('beta-owner'))
        self.assertEqual(self.a.claim('alpha-owner')['batch_id'], job)
