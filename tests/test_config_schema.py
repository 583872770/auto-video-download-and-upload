"""Pin configuration contracts before consolidating their production definitions."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

import yaml

from webapp.config.schema import (SETTINGS, RUNTIME_FIELDS, PIPELINE_KEY_FIELDS, TEXT_FIELDS,
                                  Setting, default_settings)
from Utils.oss_storage import OSS_DEFAULTS, OSS_FIELDS, OSS_INTEGER_FIELDS
from webapp.disk_budget import DISK_DEFAULTS, DISK_FIELDS
from webapp.config.schema import TASK_FIELDS
from webapp.fleet import LOCAL_FIELDS, POLICY_FIELDS, RESOURCE_LIMITS
from webapp.config.schema import DEFAULTS, DOWNLOAD_FIELDS, INTEGER_FIELDS
from webapp.config.settings import save_settings, validate_settings
from webapp.throughput import policy_key
from webapp.config.schema import PACING_FIELDS


CONTRACT = json.loads((Path(__file__).parent / 'fixtures/settings_contract.json').read_text(encoding='utf-8'))


class ConfigurationContractTests(unittest.TestCase):
    def test_registry_is_immutable_and_defaults_are_not_shared(self):
        first, second = default_settings(), default_settings()
        self.assertEqual(first, CONTRACT['defaults'])
        self.assertEqual(second, CONTRACT['defaults'])
        for key in ('youtube_cookie_pool',):
            first[key].append('changed')
            self.assertEqual(second[key], [])
            self.assertEqual(default_settings()[key], [])
            self.assertEqual(DEFAULTS[key], [])
        with self.assertRaises(TypeError):
            SETTINGS['unknown'] = Setting()
        with self.assertRaises(FrozenInstanceError):
            SETTINGS['download_task_limit'].default = 99
        for arguments in ({'default': []}, {'default': True, 'limits': (1, 64)},
                          {'default': 0, 'limits': (1, 64)}, {'scope': 'global'},
                          {'resource_override': True}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                Setting(**arguments)


    def test_import_order_has_no_dependency_cycle(self):
        orders = [('Utils.oss_storage', 'webapp.config.settings', 'webapp.fleet'),
                  ('webapp.disk_budget', 'webapp.request_scheduler', 'webapp.config.settings'),
                  ('webapp.throughput', 'webapp.store', 'app')]
        for modules in orders:
            with self.subTest(modules=modules):
                result = subprocess.run([sys.executable, '-c', ';'.join('import ' + module for module in modules)],
                                        cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_defaults_ranges_and_field_memberships(self):
        projections = dict(defaults=DEFAULTS, download_fields=sorted(DOWNLOAD_FIELDS),
                           integer_fields=INTEGER_FIELDS, policy_fields=sorted(POLICY_FIELDS),
                           local_fields=sorted(LOCAL_FIELDS), resource_limits=RESOURCE_LIMITS,
                           disk_defaults=DISK_DEFAULTS, oss_defaults=OSS_DEFAULTS,
                           oss_integer_fields=OSS_INTEGER_FIELDS, pacing_fields=sorted(PACING_FIELDS))
        for name, value in projections.items():
            with self.subTest(projection=name):
                self.assertEqual(json.loads(json.dumps(value)), CONTRACT[name])
        for key, value in DEFAULTS.items():
            with self.subTest(default_type=key):
                self.assertIs(type(value), type(CONTRACT['defaults'][key]))
        self.assertEqual(list(DEFAULTS), list(CONTRACT['defaults']), 'keep persisted YAML ordering')
        self.assertEqual(DISK_FIELDS, set(DISK_DEFAULTS))
        self.assertEqual(OSS_FIELDS, set(OSS_DEFAULTS))
        self.assertFalse(LOCAL_FIELDS & POLICY_FIELDS)
        self.assertEqual(LOCAL_FIELDS | POLICY_FIELDS | TASK_FIELDS, set(DEFAULTS))

    def test_integer_validation_preserves_boundaries_coercion_and_rejection(self):
        for key, (low, high) in CONTRACT['integer_fields'].items():
            for value in (low, high, str(low), str(high)):
                with self.subTest(key=key, valid=value):
                    config = {key: value}
                    # These domain constraints are independent of scalar ranges.
                    if key == 'disk_min_free_gb':
                        config['disk_resume_free_gb'] = high
                    if key == 'disk_resume_free_gb':
                        config['disk_min_free_gb'] = low
                    if key == 'disk_upload_limit_mb':
                        config['disk_import_budget_mb'] = high
                    if key == 'disk_import_budget_mb':
                        config['disk_upload_limit_mb'] = low
                    self.assertEqual(validate_settings(config)[key], int(value))
                    self.assertIs(type(config[key]), int)
            for value in (low - 1, high + 1, True, 1.5, None, 'invalid'):
                with self.subTest(key=key, invalid=value):
                    with self.assertRaises(ValueError):
                        validate_settings({key: value})

    def test_save_reload_keeps_unicode_and_only_known_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.yaml'
            supplied = {'path': '素材/视频', 'download_task_limit': 3, 'youtube_cookie_pool': ['a' * 32],
                        'builtin_connection_limit': 1, 'unknown': 'ignored'}
            save_settings(path, supplied)
            saved = yaml.safe_load(path.read_text(encoding='utf-8'))
            expected = {k: supplied.get(k, v) for k, v in CONTRACT['defaults'].items() if k not in TASK_FIELDS}
            self.assertEqual(saved, expected)
            self.assertEqual(list(saved), [k for k in CONTRACT['defaults'] if k not in TASK_FIELDS])
            self.assertEqual(validate_settings(saved), saved)
            self.assertEqual(list(Path(temporary).iterdir()), [path], 'no leftover temporary config')


    def test_text_and_switch_validation_keeps_existing_types(self):
        for key in TEXT_FIELDS:
            for value in (None, 1, [], {}, 'bad\x00text', 'x' * 4097):
                with self.subTest(key=key, value=type(value).__name__), self.assertRaises(ValueError):
                    validate_settings({key: value})
        for key in ('shard_downloads', 'youtube_sleep_preset'):
            for value in (True, False, 'true', 'false'):
                with self.subTest(key=key, value=value):
                    self.assertIs(validate_settings({key: value})[key], value in (True, 'true'))
            for value in ('yes', 1, None):
                with self.subTest(key=key, invalid=value), self.assertRaises(ValueError):
                    validate_settings({key: value})

    def test_runtime_persistence_retains_the_existing_whitelist(self):
        from webapp.store import TaskStore
        with tempfile.TemporaryDirectory() as temporary:
            store = TaskStore(Path(temporary))
            self.addCleanup(store.db.engine.dispose)
            config = {**deepcopy(CONTRACT['defaults']), 'unknown': 'not persisted',
                      'builtin_connection_limit': 1}
            store.set_runtime(config)
            self.assertEqual(store.runtime(), {key: config[key] for key in CONTRACT['runtime_fields']})
            store.set_runtime({'download_task_limit': 4, 'path': 'not-runtime'})
            self.assertEqual(store.runtime(), {'download_task_limit': 4}, 'partial runtime must not fill defaults')


if __name__ == '__main__':
    unittest.main()
