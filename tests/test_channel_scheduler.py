"""Resource identity, fenced admission, balanced routing and shared backoff."""
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import format_datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from webapp.bergung import BergungError
from webapp.channel_scheduler import ChannelScheduler, retry_after
from webapp.channels import group_caps, registry
from webapp.database import tasks, download_permits
from webapp.downloads import DownloadLeaseLost
from webapp.settings import save_settings, validate_settings
from webapp.store import TaskStore
from webapp.throughput import PipelineBusy


def channel(identity, group='bergung', **kwargs):
    return dict(id=identity, adapter='bergung', upstream_group=group,
                resolver_group=group, quota_group=group, **kwargs)


class RegistryTests(unittest.TestCase):
    def test_mysql_decimal_resource_sum_is_normalized_to_integer(self):
        db = Mock()
        db.execute.return_value.scalar_one.return_value = Decimal(2)
        scheduler = ChannelScheduler(None, {'download_method':'bergung'})
        result = scheduler._active(db, 'upstream:test', time.time())
        self.assertIs(type(result), int)
        self.assertEqual(result, 2)
    def test_alias_is_conservative_and_explicit_groups_can_split(self):
        channels = registry({'download_method': 'bergung', 'download_channels': [{'id':'alias'}]})
        self.assertEqual([item.upstream_group for item in channels], ['unverified-media', 'unverified-media'])
        self.assertEqual(len(group_caps(channels)), 4)
        independent = registry({'download_method': 'bergung', 'download_channels': [channel('other', 'independent')]})
        self.assertEqual(len(group_caps(independent)), 8)

    def test_alias_caps_use_minimum_and_default_override_keeps_other_entrances(self):
        values = [channel('extra', transfer_limit=2), channel('bergung', transfer_limit=6)]
        channels = registry({'download_method': 'bergung', 'download_channels': values})
        self.assertEqual([item.id for item in channels], ['bergung', 'extra'])
        self.assertEqual(group_caps(channels)['upstream:bergung'], 2)

    def test_configuration_validation_and_yaml_roundtrip(self):
        config = {'download_method': 'bergung', 'scheduler_mode': 'balanced',
                  'download_channels': [channel('bergung')], 'shared_parser': True}
        validate_settings(config)
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            save_settings(path, config)
            saved = yaml.safe_load(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['download_channels'], config['download_channels'])
        for values in ([channel('bad id')], [channel('alias'), channel('alias')],
                       [channel('yt_dlp')], [channel('alias', site_url='http://example.com')],
                       [channel('alias', transfer_limit=True)], [channel('alias', cookie='secret')]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                registry({'download_method': 'bergung', 'download_channels': values})

    def test_retry_after_delta_date_and_invalid_values(self):
        self.assertEqual(retry_after('75', 100), 75)
        date = format_datetime(datetime.fromtimestamp(200, timezone.utc), usegmt=True)
        self.assertEqual(retry_after(date, 100), 100)
        self.assertIsNone(retry_after('nan'))
        self.assertIsNone(retry_after('invalid'))

    def test_legacy_browser_resolver_configuration_is_normalized(self):
        config = {'download_method': 'bergung', 'download_channels': [channel('alias', resolver_mode='browser')]}
        validate_settings(config)
        self.assertNotIn('resolver_mode', config['download_channels'][0])
        self.assertEqual(registry(config)[1].id, 'alias')
        with self.assertRaises(ValueError):
            registry({'download_method': 'bergung', 'download_channels': [channel('alias', resolver_mode='api')]})


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        url = 'sqlite:///' + (self.root / 'shared.db').as_posix()
        self.first = TaskStore(self.root / 'a', url)
        self.second = TaskStore(self.root / 'b', url)
        self.config = {'download_method': 'bergung', 'worker_count': 8,
                       'download_channels': [channel('bergung', transfer_limit=1), channel('alias'), channel('other', 'independent')]}

    def scheduler(self, store=None, config=None):
        store = store or self.first
        job = store.create('download', {'base': str(self.root), 'urls': ['https://youtu.be/00000000001']})
        with store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == job['id']).values(
                status='running', worker_id=store.owner, lease_until=time.time()+60))
        return ChannelScheduler(store, config or self.config, job['id'])

    def test_aliases_share_cap_while_independent_group_can_transfer(self):
        first, second = self.scheduler(), self.scheduler(self.second)
        a, alias, other = first.channels
        with first.permit(a, 'transfer', timeout=0):
            with self.assertRaises(PipelineBusy):
                with second.permit(alias, 'transfer', timeout=0):
                    self.fail('alias bypassed shared bandwidth allowance')
            with second.permit(other, 'transfer', timeout=0):
                pass
        with second.permit(alias, 'transfer', timeout=0):
            pass

    def test_all_scopes_are_atomic_and_failed_admission_leaks_no_permit(self):
        first, second = self.scheduler(), self.scheduler(self.second)
        a, alias, other = first.channels
        with first.permit(a, 'transfer', timeout=0):
            with self.assertRaises(PipelineBusy):
                with second.permit(alias, 'transfer', timeout=0):
                    pass
            with self.first.connect() as db:
                self.assertFalse(db.execute(select(download_permits).where(download_permits.c.task_id == second.task_id)).first())

    def test_waiting_transfer_reloads_runtime_limit_without_resolving_again(self):
        from dataclasses import replace
        first, second = self.scheduler(), self.scheduler(self.second)
        route = replace(first.channels[0], transfer_limit=8)
        first.caps['upstream:bergung'] = second.caps['upstream:bergung'] = 8
        first.caps['quota:bergung'] = second.caps['quota:bergung'] = 8
        limits = {'transfers': 8, 'groups': {'bergung': {'limit': 1}}}
        waits = []
        def waiting():
            waits.append(second.blocked_resource)
            limits['groups']['bergung']['limit'] = 2
        with patch('webapp.channel_scheduler.runtime_limits', return_value=limits):
            with first.permit(route, 'transfer', timeout=0):
                with second.permit(route, 'transfer', timeout=5, on_wait=waiting):
                    pass
        self.assertEqual(waits, ['local-upstream'])

    def test_expired_and_reassigned_task_cannot_keep_or_obtain_capacity(self):
        first, second = self.scheduler(), self.scheduler(self.second)
        with first.permit(first.channels[0], 'transfer', timeout=0):
            with self.first.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == first.task_id).values(lease_until=time.time()-1))
            with second.permit(second.channels[0], 'transfer', timeout=0):
                pass
            with self.assertRaises(DownloadLeaseLost):
                with first.permit(first.channels[0], 'resolve', timeout=0):
                    pass

    def test_balancing_selects_independent_group_without_doubling_alias_share(self):
        config = {**self.config, 'scheduler_mode': 'balanced'}
        first, second = self.scheduler(config=config), self.scheduler(self.second, config)
        with first.assignment(first.channels) as selected:
            self.assertEqual(selected.upstream_group, 'bergung')
            with second.assignment(second.channels) as next_channel:
                self.assertEqual(next_channel.upstream_group, 'independent')

    def test_rate_limit_cools_shared_resolver_and_quota_across_instances(self):
        first, second = self.scheduler(), self.scheduler(self.second)
        first.outcome(first.channels[0], success=False,
                      error=BergungError('rate limit', retryable=True, http_status=429, retry_after=30))
        with first.store.connect() as db:
            state = first._state(db, 'quota:' + first.channels[0].quota_group)
            self.assertEqual(state['cooldown_status'], 429)
            self.assertEqual(state['cooldown_kind'], 'resolve')
        with self.assertRaises(PipelineBusy):
            with second.permit(second.channels[1], 'resolve', timeout=0):
                pass
        with second.permit(second.channels[2], 'resolve', timeout=0):
            pass

    def test_video_failure_does_not_cool_other_entrances(self):
        first, second = self.scheduler(), self.scheduler(self.second)
        first.outcome(first.channels[0], success=False, error=BergungError('video unavailable'))
        with second.permit(second.channels[1], 'resolve', timeout=0):
            pass

    def test_parallel_processes_cannot_oversubscribe_the_same_group(self):
        parent, child = self.scheduler(), self.scheduler(self.second)
        code = '''import json,sys
from webapp.store import TaskStore
from webapp.channel_scheduler import ChannelScheduler
from webapp.throughput import PipelineBusy
s=TaskStore(sys.argv[1],sys.argv[2]);s.owner=sys.argv[3]
h=ChannelScheduler(s,json.loads(sys.argv[4]),sys.argv[5])
try:
 with h.permit(h.channels[0], 'transfer', timeout=0): print('admitted')
except PipelineBusy: print('busy')
'''
        with parent.permit(parent.channels[0], 'transfer', timeout=0):
            result = subprocess.run([sys.executable, '-c', code, str(self.second.directory), self.second.db.url,
                                     self.second.owner, json.dumps(self.config), child.task_id], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'busy')


class AutoSchedulerTests(unittest.TestCase):
    setUp = AdmissionTests.setUp
    scheduler = AdmissionTests.scheduler

    def setUp(self):
        AdmissionTests.setUp(self)
        limits = patch('webapp.channel_scheduler.runtime_limits', return_value={
            'transfers':16, 'workers':20, 'browsers':3, 'groups':{}})
        limits.start()
        self.addCleanup(limits.stop)
        self.config = {'download_methods':['bergung'], 'scheduler_mode':'auto', 'worker_count':16,
            'download_connection_limit':4, 'download_channels':[
                channel('bergung', 'shared', transfer_limit=32, api_url='https://first.example.com/'),
                channel('alias', 'shared', transfer_limit=32, api_url='https://second.example.com/')]}

    def warm(self, scheduler, rates=None):
        for index, item in enumerate(scheduler.channels):
            rate = rates[index] if rates else 8 * 1024**2
            scheduler.outcome(item, success=True, size=64*1024**2, elapsed=4+64*1024**2/rate,
                              resolve_elapsed=4, transfer_elapsed=64*1024**2/rate)

    def state(self, scheduler, channel):
        with scheduler.transaction() as (db, stamp):
            return scheduler._state(db, 'channel:' + channel.id)

    def test_routes_across_providers_without_using_order(self):
        config = {**self.config, 'download_methods':['bergung','downmp3'], 'download_channels':[]}
        scheduler = self.scheduler(config=config)
        self.warm(scheduler, [1024**2, 8*1024**2])
        with scheduler.assignment(scheduler.channels) as chosen:
            self.assertEqual(chosen.adapter, 'downmp3')

    def test_only_one_unproven_entrance_can_occupy_preparation(self):
        first, second = self.scheduler(), self.scheduler(self.second)
        with first.assignment(first.channels) as initial:
            with self.assertRaises(PipelineBusy):
                with second.assignment(second.channels):
                    self.fail('all browsers occupied by unproven routes')
        with second.assignment(second.channels) as next_route:
            self.assertNotEqual(initial.id, next_route.id)

    def test_warm_work_can_continue_while_one_cold_route_is_probed(self):
        first, second = self.scheduler(), self.scheduler(self.second)
        first.outcome(first.channels[0], success=True, size=64*1024**2, elapsed=10, transfer_elapsed=8)
        with first.assignment(first.channels) as probe:
            self.assertEqual(probe.id, 'alias')
            with second.assignment(second.channels) as warm:
                self.assertEqual(warm.id, 'bergung')

    def test_ready_first_file_frees_probe_but_retains_entrance_ownership(self):
        first, second, third = self.scheduler(), self.scheduler(self.second), self.scheduler()
        with first.assignment(first.channels) as route:
            first.prepared()
            with second.assignment(second.channels) as next_route:
                self.assertNotEqual(route.id, next_route.id)
                second.prepared()
                with self.assertRaises(PipelineBusy):
                    with third.assignment(third.channels):
                        self.fail('an unmeasured entrance received a duplicate first task')

    def test_same_api_alias_cannot_multiply_route_or_resolver_capacity(self):
        entries = [channel('bergung', 'one', task_limit=1, resolver_limit=1, api_url='https://API.example.com/'),
                   channel('alias', 'two', task_limit=1, resolver_limit=1, api_url='https://api.example.com:443/')]
        config = {**self.config, 'download_channels':entries}
        first, second = self.scheduler(config=config), self.scheduler(self.second, config)
        self.warm(first)
        with first.assignment(first.channels):
            with self.assertRaises(PipelineBusy):
                with second.assignment(second.channels):
                    self.fail('same backend gained another task allocation')
        with first.permit(first.channels[0], 'resolve', timeout=0):
            with self.assertRaises(PipelineBusy):
                with second.permit(second.channels[1], 'resolve', timeout=0):
                    self.fail('same API gained another parser allocation')

    def test_same_api_throttle_propagates_even_with_different_configured_groups(self):
        entries = [channel('bergung', 'one', api_url='https://same.example.com/'),
                   channel('alias', 'two', api_url='https://same.example.com/'),
                   channel('other', 'three', api_url='https://other.example.com/')]
        config = {**self.config, 'download_channels':entries}
        first, second = self.scheduler(config=config), self.scheduler(self.second, config)
        first.outcome(first.channels[0], success=False,
            error=BergungError('limited', http_status=429, retry_after=30))
        with self.assertRaises(PipelineBusy):
            with second.permit(second.channels[1], 'resolve', timeout=0):
                pass
        with second.permit(second.channels[2], 'resolve', timeout=0):
            pass

    def test_video_and_local_failures_do_not_poison_channel_health(self):
        scheduler = self.scheduler()
        self.warm(scheduler)
        before = self.state(scheduler, scheduler.channels[0])
        for error in (BergungError('private', failure_scope='video'), DownloadLeaseLost('expired'),
                      OSError(28, 'no space'), PipelineBusy('capacity'), PoolTimeoutError('pool busy')):
            scheduler.outcome(scheduler.channels[0], success=False, error=error)
            self.assertEqual(self.state(scheduler, scheduler.channels[0]), before)

    def test_wrapped_transient_failure_cools_only_its_route(self):
        scheduler = self.scheduler()
        error = RuntimeError('downloader wrapper')
        error.__cause__ = BergungError('upstream unavailable', retryable=True)
        scheduler.outcome(scheduler.channels[0], success=False, error=error)
        with scheduler.assignment(scheduler.channels) as chosen:
            self.assertEqual(chosen.id, 'alias')
        self.assertGreater(self.state(scheduler, scheduler.channels[0])['cooldown_until'], time.time())

    def test_reference_file_scoring_does_not_prefer_short_clips(self):
        scheduler = self.scheduler()
        for item, size in zip(scheduler.channels, (64*1024**2, 1024**2)):
            scheduler.outcome(item, success=True, size=size, elapsed=4+size/1024**2,
                              resolve_elapsed=4, transfer_elapsed=size/1024**2)
        with scheduler.assignment(scheduler.channels) as chosen:
            self.assertEqual(chosen.id, 'bergung')

    def test_connection_budget_cannot_be_exceeded_by_fragment_permits(self):
        first, second = self.scheduler(), self.scheduler(self.second)
        with first.permit(first.channels[0], 'transfer', streams=3, timeout=0):
            with self.assertRaises(PipelineBusy):
                with second.permit(second.channels[1], 'transfer', streams=2, timeout=0):
                    pass
        with second.permit(second.channels[1], 'transfer', streams=2, timeout=0):
            pass

    def test_auto_setting_roundtrip_includes_catalog_and_resource_control(self):
        from webapp.cobalt_catalog import verified_channels
        config = {'download_methods':['bergung'], 'scheduler_mode':'auto', 'throughput_mode':'manual',
                  'download_channels':verified_channels(), 'auto_channel_attempts':3}
        validate_settings(config)
        self.assertEqual(config['throughput_mode'], 'auto')
        self.assertTrue(all(item['verification'] == 'auto' for item in config['download_channels']))
