"""Disabled providers never return through retries, takeover or fallback."""
import unittest
from unittest.mock import patch

from tests import test_fleet, test_scale
from web_worker import execute


class ProviderSelectionTests(unittest.TestCase):
    setUp = test_scale.ScaleTests.setUp

    def save(self, methods):
        result = self.client.put('/api/config', json={
            'download_methods': methods, 'disk_min_free_gb': 0, 'disk_resume_free_gb': 0},
            headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)

    def test_reclaim_replaces_old_provider_and_progress_atomically(self):
        self.save(['yt1d', 'downmp3'])
        task = self.store.create('download', {'urls': ['https://youtu.be/npNnbj46_ik']})
        first = self.store.claim()
        self.store.update_progress(first['id'], {'stage': 'downloading', 'download_method': 'yt1d',
            'selected_download_methods': ['yt1d', 'downmp3'], 'percent': 65, 'downloaded_bytes': 100,
            'proxy_node': 'previous-node', 'fallback_used': True, 'fallback_reason': 'old failure'})
        self.store.release(first['id'], self.store.owner, 'fixture restart', delay=0)
        self.save(['downmp3'])
        # The API must already show the new method before the worker reports.
        again = self.store.claim()
        self.assertEqual(again['id'], task['id'])
        public = self.client.get('/api/tasks/' + again['id']).json
        self.assertEqual(public['progress']['download_method'], 'downmp3')
        self.assertEqual(public['progress']['selected_download_methods'], ['downmp3'])
        self.assertEqual(public['progress']['stage'], 'preparing_workspace')
        self.assertEqual(public['progress']['percent'], 0)
        self.assertEqual(public['progress']['downloaded_bytes'], 0)
        for key in ('proxy_node', 'fallback_used', 'fallback_reason'):
            self.assertNotIn(key, public['progress'])
        self.assertTrue(again['payload']['_restart_transfer'])

    def test_failure_only_switches_to_selected_provider(self):
        import yt_dlp
        self.save(['yt5s', 'downmp3'])
        self.store.create('download', {'config': {'download_methods': ['yt1d']},
            'base': str(self.root), 'urls': ['https://youtu.be/npNnbj46_ik']})
        task = self.store.claim()
        values = []
        with patch('webapp.yt1d.require_environment') as environment, \
                patch('webapp.yt1d.resolve') as yt1d, \
                patch('webapp.yt5s.require_browser_environment'), \
                patch('webapp.environment.require_downmp3_environment'), \
                patch('webapp.yt5s.resolve', side_effect=RuntimeError('fixture provider failure')) as yt5s, \
                patch('webapp.downmp3.resolve', return_value={'id': 'npNnbj46_ik'}) as downmp3, \
                patch.object(yt_dlp.YoutubeDL, 'process_ie_result'), patch('builtins.print'):
            result = execute('download', task['payload'], values.append,
                             store=self.store, task_id=task['id'])
        self.assertEqual(result['download_method'], 'downmp3')
        yt5s.assert_called_once()
        downmp3.assert_called_once()
        yt1d.assert_not_called()
        environment.assert_not_called()
        self.assertTrue(values)
        self.assertTrue(all(value['selected_download_methods'] == ['yt5s', 'downmp3'] for value in values))
        self.assertTrue(all(value['download_method'] in ('yt5s', 'downmp3') for value in values))


class InstanceProviderSelectionTests(unittest.TestCase):
    setUp = test_fleet.FleetTests.setUp
    setup_fleet = test_fleet.FleetTests.setup_fleet
    queue = test_fleet.FleetTests.queue
    activate = test_fleet.FleetTests.activate

    def test_takeover_uses_new_instance_methods_before_any_progress_report(self):
        self.queue(1)
        self.activate()
        self.a.fleet.save_policy({'download_methods': ['yt1d', 'downmp3']})
        first = self.a.claim()
        self.a.update_progress(first['id'], {**first['progress'], 'download_method': 'yt1d',
            'percent': 80, 'fallback_reason': 'old'})
        self.a.release(first['id'], self.a.owner, 'fixture takeover', delay=0)
        self.b.fleet.save_policy({'download_methods': ['downmp3']})
        again = self.b.claim()
        self.assertEqual(again['instance_id'], 'beta')
        self.assertEqual(again['payload']['config']['download_methods'], ['downmp3'])
        self.assertEqual(again['progress']['selected_download_methods'], ['downmp3'])
        self.assertEqual(again['progress']['download_method'], 'downmp3')
        self.assertEqual(again['progress']['percent'], 0)
        self.assertNotIn('fallback_reason', again['progress'])


if __name__ == '__main__':
    unittest.main()
